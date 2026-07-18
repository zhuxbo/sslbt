"""续签引擎。对标 sslctl pkg/certops/renew.go 状态机"""

import os
import json
import time
import random
import fcntl
import tempfile
from datetime import datetime, timezone

from . import cert_utils
from .api_client import APIError

# 常量，对标 sslctl
RENEW_DEFAULT_DAYS = 14
MAX_RENEW_BEFORE_DAYS = 30  # 续签应在到期前 30 天内，超限视为服务端异常值（spec 2.9）
MAX_ISSUE_RETRY_COUNT = 10
RENEW_SLEEP_MIN = 5
RENEW_SLEEP_MAX = 120
SPREAD_TOTAL_MAX = 600  # 分散延迟总量上限（秒）
MAX_RENEW_BATCH = 100   # 单次续签证书数量上限


def _expiry_unknown(meta):
    """判断 metadata 中的到期时间是否未知（为空或不可解析）"""
    expires_at = meta.get('cert_expires_at', '')
    if not expires_at:
        return True
    try:
        datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
        return False
    except (ValueError, AttributeError):
        return True


def needs_renewal(cert_entry, renew_before_days):
    """判断证书是否需要进入续签/查询流程（续签决策以服务端为主，spec §3.4）。

    本函数仅做"是否放行进入续签流程"的前置判定，不发起任何网络请求；到期时间
    未知时的实际 API 查询回填由各模式处理器完成（local 见 RenewEngine._renew_local
    的回填分支，pull 见 RenewEngine._renew_pull 的查询-部署）。

    - 已明确过期（剩余天数 < 0）：停止，等待人工处理（spec §3.2）
    - local 模式已提交 CSR（last_issue_state == 'processing'）：放行进入查询流程跟进签发
    - cert_expires_at 为空/不可解析：到期时间未知，放行交由处理器查询回填后再判定
      （覆盖首次部署遇 processing、metadata 写失败、带外换证等中间态，避免 cron 永不接手）
    - 到期时间已知且未到期：仅当剩余天数 ≤ renew_before_days 才续签
    """
    meta = cert_entry.get('metadata', {})
    expires_at = meta.get('cert_expires_at', '')

    days_remaining = None
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
            days_remaining = (exp_dt - datetime.now(timezone.utc)).days
        except (ValueError, AttributeError):
            days_remaining = None

    # 已明确过期，停止续签，等待人工处理
    if days_remaining is not None and days_remaining < 0:
        return False

    # 已提交 CSR 待签发：无论到期时间是否已知都需进入查询流程
    if meta.get('last_issue_state', '') == 'processing':
        return True

    # 到期时间未知（空/不可解析）：视为需处理，进入 API 查询回填 metadata
    if days_remaining is None:
        return True

    return days_remaining <= renew_before_days


class RenewEngine:
    """续签引擎"""

    def __init__(self, config_manager, api_factory, deployer, logger=None, file_verifier=None):
        self._config = config_manager
        self._api_factory = api_factory
        self._deployer = deployer
        self._logger = logger
        self._file_verifier = file_verifier
        self._data_dir = config_manager._data_dir

    def check_and_renew_all(self, spread=False):
        """检查并续签所有证书

        Args:
            spread: 是否在续签间加随机延迟，避免集中请求 API（cron 调用时为 True）
        """
        # 并发保护：非阻塞获取进程锁
        lock_path = os.path.join(self._data_dir, 'renew.lock')
        lock_fd = open(lock_path, 'w')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_fd.close()
            if self._logger:
                self._logger.info("另一个续签进程正在运行，跳过本次检查")
            return []

        try:
            return self._do_renew_all(spread)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    def _do_renew_all(self, spread=False):
        """实际续签逻辑（已持有进程锁）"""
        certs = self._config.get_certs()

        # 阶段 1: 收集需续签的证书和对应 API 客户端
        pending_list = []
        for cert in certs:
            if not cert.get('enabled', True):
                continue
            order_id = cert.get('order_id')
            if not order_id:
                continue
            renew_mode = self._config.get_renew_mode(cert)
            # local 模式：重试超限则跳过（spec 3.2）
            if renew_mode == 'local':
                meta = cert.get('metadata', {})
                if meta.get('issue_retry_count', 0) > MAX_ISSUE_RETRY_COUNT:
                    if self._logger:
                        self._logger.warning("证书 order_id=%s CSR 重试超限，跳过", order_id)
                    continue
            renew_days = self._config.get_renew_before_days(cert)
            if not needs_renewal(cert, renew_days):
                continue
            api = self._api_factory(cert)
            if not api:
                if self._logger:
                    self._logger.warning("证书 order_id=%s 缺少 API 配置，跳过续签", order_id)
                continue
            pending_list.append((cert, api, renew_mode))

        # 阶段 2: 截断并计算延迟
        if len(pending_list) > MAX_RENEW_BATCH:
            if self._logger:
                self._logger.warning(
                    "需续签证书 %d 个，超过单次上限 %d，截断处理",
                    len(pending_list), MAX_RENEW_BATCH)
            pending_list = pending_list[:MAX_RENEW_BATCH]
        sleep_min, sleep_max = self._calc_spread_delay(len(pending_list))

        # 阶段 3: 逐个续签
        results = []
        for cert, api, renew_mode in pending_list:
            order_id = cert['order_id']

            if spread and results:
                delay = random.randint(sleep_min, sleep_max)
                if self._logger:
                    self._logger.info("等待 %d 秒后处理下一个证书", delay)
                time.sleep(delay)

            if self._logger:
                self._logger.info("证书需要续签: order_id=%s, mode=%s", order_id, renew_mode)

            try:
                if renew_mode == 'local':
                    result = self._renew_local(cert, api)
                else:
                    result = self._renew_pull(cert, api)
                results.append({
                    'order_id': order_id,
                    'status': 'success' if result else 'pending',
                    'message': '续签完成' if result else '等待签发',
                })
            except Exception as e:
                if self._logger:
                    self._logger.error("续签失败: order_id=%s, error=%s", order_id, str(e))
                results.append({
                    'order_id': order_id,
                    'status': 'failure',
                    'message': str(e),
                })

        # 汇总日志
        if self._logger:
            total = len(results)
            if total:
                success = sum(1 for r in results if r['status'] == 'success')
                pending = sum(1 for r in results if r['status'] == 'pending')
                failed = sum(1 for r in results if r['status'] == 'failure')
                self._logger.info("续签检查完成: %d 个证书, 成功=%d, 等待=%d, 失败=%d",
                                  total, success, pending, failed)
            else:
                self._logger.info("续签检查完成: 无需续签")

        self._write_renew_status(results)
        return results

    def _write_renew_status(self, results):
        """写入最近一次续签运行的轻量状态（供面板展示），失败不影响续签

        复用数据目录，原子写 + 0600 权限，与 config/session 落盘约定一致。
        """
        status = {
            'last_run': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'total': len(results),
            'success': sum(1 for r in results if r.get('status') == 'success'),
            'pending': sum(1 for r in results if r.get('status') == 'pending'),
            'failure': sum(1 for r in results if r.get('status') == 'failure'),
        }
        path = os.path.join(self._data_dir, 'renew_status.json')
        data = json.dumps(status, ensure_ascii=False).encode('utf-8')
        # 随机名临时文件（mkstemp 以 O_EXCL|O_CREAT 创建，不跟随符号链接）+ 原子替换，
        # 避免固定名 .tmp 被预置为符号链接时经 O_TRUNC 覆盖任意目标文件
        try:
            fd, tmp = tempfile.mkstemp(dir=self._data_dir, prefix='.renew_status-', suffix='.tmp')
        except OSError as e:
            if self._logger:
                self._logger.warning("写入续签状态文件失败: %s", str(e))
            return
        try:
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            if self._logger:
                self._logger.warning("写入续签状态文件失败: %s", str(e))

    @staticmethod
    def _calc_spread_delay(count):
        """根据需续签证书数量动态计算延迟区间

        保证总延迟不超过 SPREAD_TOTAL_MAX（默认 600 秒），
        证书少时用较大间隔（30-120s），证书多时自动缩短。
        """
        if count <= 1:
            return RENEW_SLEEP_MIN, RENEW_SLEEP_MAX
        gaps = count - 1
        avg_max = SPREAD_TOTAL_MAX // gaps
        sleep_max = max(RENEW_SLEEP_MIN, min(avg_max, RENEW_SLEEP_MAX))
        sleep_min = max(RENEW_SLEEP_MIN, sleep_max // 3)
        return sleep_min, sleep_max

    def _check_deploy_results(self, results, order_id):
        """检查 deploy_multi 结果：全部失败视为部署失败，部分失败记录警告但仍视为成功

        站点缺失（site_removed 已确认删除并解绑 / site_missing 首轮疑似删除）时按
        失败上报，与部署回调的 failure 语义一致；缺失站点恢复或解绑后的后续轮次
        不再出现该站点，恢复 success。
        """
        if not results:
            raise RuntimeError("部署结果为空: order_id=%s" % order_id)
        success_count = sum(1 for r in results if r.get('status'))
        fail_count = len(results) - success_count
        if success_count == 0:
            failed_msgs = '; '.join(r.get('message', '') for r in results if not r.get('status'))
            raise RuntimeError("所有站点部署失败: %s" % failed_msgs)
        removed = [r['site_name'] for r in results if r.get('site_removed')]
        if removed:
            raise RuntimeError("站点已删除，已解除绑定: %s" % ','.join(removed))
        remove_failed = [r['site_name'] for r in results if r.get('site_remove_failed')]
        if remove_failed:
            raise RuntimeError("站点已删除，但解除绑定持久化失败: %s" % ','.join(remove_failed))
        # 疑似删除（首轮缺失，尚未解绑）：与回调 failure 一致按失败上报，等待二次确认
        suspected = [r['site_name'] for r in results if r.get('site_missing')]
        if suspected:
            raise RuntimeError("站点疑似已删除，待二次确认: %s" % ','.join(suspected))
        if fail_count > 0 and self._logger:
            failed = [r['site_name'] for r in results if not r.get('status')]
            self._logger.warning(
                "部分站点部署失败: order_id=%s, failed_sites=%s",
                order_id, ','.join(failed),
            )
        return True

    def _update_renew_before_days(self, api):
        """从 api.last_renew_before_days 更新全局配置"""
        try:
            days = int(getattr(api, 'last_renew_before_days', 0) or 0)
        except (TypeError, ValueError):
            return
        if days > MAX_RENEW_BEFORE_DAYS:
            if self._logger:
                self._logger.warning(
                    "服务端返回的 renew_before_days=%s 超过上限 %s（续签应在到期前 30 天内），保留本地配置",
                    days, MAX_RENEW_BEFORE_DAYS)
            return
        if days > 0:
            cfg = self._config.get_config()
            cfg['schedule']['renew_before_days'] = days
            self._config.save_config(cfg)

    def _send_failure_callback(self, api, order_id, message):
        """发送 failure 部署回调（非关键路径，失败仅记日志）"""
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        try:
            api.callback(order_id=order_id, status='failure', deployed_at=now, message=message)
        except Exception as e:
            if self._logger:
                self._logger.warning("部署回调失败（非关键）: %s", str(e))

    def _renew_pull(self, cert_entry, api):
        """Pull 模式续签：查询订单 → 证书就绪则部署"""
        order_id = cert_entry['order_id']
        if self._logger:
            self._logger.info("Pull 模式续签: order_id=%s", order_id)

        cert_data = api.query_order(order_id)
        self._update_renew_before_days(api)
        order_id = self._check_order_update(cert_entry, cert_data)

        status = cert_data.get('status', '')
        certificate = cert_data.get('certificate', '')
        ca_certificate = cert_data.get('ca_certificate', '')
        private_key = cert_data.get('private_key', '')

        if status != 'active' or not certificate:
            if self._logger:
                self._logger.info("证书未就绪: status=%s", status)
            return False

        if not ca_certificate:
            if self._logger:
                self._logger.warning("缺少中间证书，等待下次检查")
            return False

        # 构建完整证书链并部署
        fullchain = cert_utils.build_fullchain(certificate, ca_certificate)
        site_names = cert_entry.get('site_name', [])
        if isinstance(site_names, str):
            site_names = [site_names] if site_names else []
        domains = cert_entry.get('domains', [])

        if not site_names:
            if self._logger:
                self._logger.warning("未绑定站点，跳过部署")
            return False

        results = self._deployer.deploy_multi(
            site_names=site_names,
            fullchain_pem=fullchain,
            key_pem=private_key,
            order_id=order_id,
            domains=domains,
            api_client=api,
        )
        return self._check_deploy_results(results, order_id)

    def _renew_local(self, cert_entry, api):
        """Local 模式续签：生成 CSR → 提交 → 等待签发 → 部署"""
        order_id = cert_entry['order_id']
        meta = cert_entry.get('metadata', {})

        if self._logger:
            self._logger.info("Local 模式续签: order_id=%s", order_id)

        # 检查重试次数，超过上限等待人工处理
        retry_count = meta.get('issue_retry_count', 0)
        if retry_count > MAX_ISSUE_RETRY_COUNT:
            raise RuntimeError("CSR 提交重试次数已达上限 (%d)" % MAX_ISSUE_RETRY_COUNT)

        last_state = meta.get('last_issue_state', '')

        if last_state == 'processing':
            return self._handle_processing(cert_entry, api)

        # 到期时间未知且无在途 CSR：先查询 API 回填元数据再按正常逻辑判定，
        # 避免对"部署成功但元数据丢失/带外换证"的证书盲目重新提交 CSR（对齐 sslctl）
        if _expiry_unknown(meta):
            return self._refresh_and_maybe_renew_local(cert_entry, api)

        return self._submit_new_csr(cert_entry, api)

    def _refresh_and_maybe_renew_local(self, cert_entry, api):
        """Local 模式到期时间未知：查询 API 回填元数据后再按正常续签逻辑判定

        - 查询失败：向上抛出（本轮按失败处理），不盲目提交 CSR
        - 服务端未返回证书内容 / 证书解析失败：本轮跳过（返回 False），下轮再试
        - 回填成功后：仍需续签（临期/已过期）→ 提交新 CSR；否则本轮不续签
        """
        order_id = cert_entry['order_id']
        cert_data = api.query_order(order_id)
        self._update_renew_before_days(api)
        order_id = self._check_order_update(cert_entry, cert_data)

        certificate = cert_data.get('certificate', '')
        if not certificate:
            if self._logger:
                self._logger.warning(
                    "证书到期时间未知且服务端未返回证书内容，本轮跳过: order_id=%s, status=%s",
                    order_id, cert_data.get('status', ''))
            return False

        cert_info = cert_utils.parse_cert_info(certificate)
        if not cert_info or not cert_info.get('not_after'):
            if self._logger:
                self._logger.warning("回填到期时间失败（证书解析错误），本轮跳过: order_id=%s", order_id)
            return False

        expires_at = cert_info['not_after'].strftime('%Y-%m-%dT%H:%M:%SZ')
        cert_serial = cert_info.get('serial', '')
        self._config.update_metadata(order_id, {
            'cert_expires_at': expires_at,
            'cert_serial': cert_serial,
        })
        meta = cert_entry.setdefault('metadata', {})
        meta['cert_expires_at'] = expires_at
        meta['cert_serial'] = cert_serial
        if self._logger:
            self._logger.info("证书到期时间已从服务端回填: order_id=%s, expires_at=%s",
                              order_id, expires_at)

        # 回填后按正常逻辑判定：剩余期限充足则本轮不续签
        renew_days = self._config.get_renew_before_days(cert_entry)
        if not needs_renewal(cert_entry, renew_days):
            if self._logger:
                self._logger.info("回填后剩余期限充足，本轮不续签: order_id=%s", order_id)
            return False

        return self._submit_new_csr(cert_entry, api)

    def _check_order_update(self, cert_entry, cert_data):
        """检查 API 返回的 order_id 是否变化（续费），变化则更新配置和 pending key 路径"""
        old_id = cert_entry['order_id']
        new_id = cert_data.get('order_id')
        if not new_id or int(new_id) == int(old_id):
            return old_id
        new_id = int(new_id)
        if self._logger:
            self._logger.info("订单续费，ID 更新: %s → %s", old_id, new_id)
        try:
            self._config.update_order_id(old_id, new_id)
        except ValueError as e:
            if self._logger:
                self._logger.warning("更新订单 ID 失败: %s", str(e))
            return old_id
        # 重命名 pending key 目录
        old_name = cert_entry.get('cert_name', 'order-%s' % old_id)
        new_name = 'order-%d' % new_id
        old_dir = os.path.join(self._data_dir, 'pending-keys', old_name)
        new_dir = os.path.join(self._data_dir, 'pending-keys', new_name)
        try:
            if os.path.isdir(old_dir) and not os.path.exists(new_dir):
                os.rename(old_dir, new_dir)
        except OSError as e:
            if self._logger:
                self._logger.warning("重命名 pending key 目录失败: %s", str(e))
        # 更新内存中的 cert_entry
        cert_entry['order_id'] = new_id
        cert_entry['cert_name'] = new_name
        return new_id

    def _handle_processing(self, cert_entry, api):
        """处理已提交 CSR 的 processing 状态"""
        order_id = cert_entry['order_id']
        meta = cert_entry.get('metadata', {})

        # 查询订单状态
        cert_data = api.query_order(order_id)
        self._update_renew_before_days(api)
        order_id = self._check_order_update(cert_entry, cert_data)
        status = cert_data.get('status', '')

        if status == 'processing':
            # 检查是否有新的验证文件需要放置
            self._try_place_verify_file(cert_entry, cert_data)
            if self._logger:
                self._logger.info("证书仍在处理中，继续等待")
            return False

        if status != 'active':
            if self._logger:
                self._logger.info("证书状态异常: %s，清除状态", status)
            self._cleanup_verify_files(meta)
            self._config.update_metadata(order_id, {
                'last_issue_state': '',
                'csr_submitted_at': '',
                'pending_file_verify': '',
                'pending_verify_paths': [],
            })
            return False

        # 证书已签发，清理验证文件
        self._cleanup_verify_files(meta)

        # 读取 pending key 并部署
        certificate = cert_data.get('certificate', '')
        ca_certificate = cert_data.get('ca_certificate', '')

        if not certificate or not ca_certificate:
            if self._logger:
                self._logger.warning("证书内容不完整")
            return False

        pending_key = self._read_pending_key(cert_entry)
        if not pending_key:
            if self._logger:
                self._logger.error("pending key 不存在")
            self._config.update_metadata(order_id, {
                'last_issue_state': '',
                'csr_submitted_at': '',
            })
            return False

        fullchain = cert_utils.build_fullchain(certificate, ca_certificate)
        site_names = cert_entry.get('site_name', [])
        if isinstance(site_names, str):
            site_names = [site_names] if site_names else []
        domains = cert_entry.get('domains', [])

        if not site_names:
            # 无绑定站点可部署（如站点删除确认解绑后）：不再静默跳过留下
            # processing 孤儿（每日空查询+私钥永驻）——清理 pending 私钥、清空
            # 签发状态使状态收敛，发 failure 回调并按失败上报；
            # 用户重新绑定站点后走正常重签流程
            if self._logger:
                self._logger.error("无绑定站点可部署，清除签发状态收敛: order_id=%s", order_id)
            self._cleanup_pending_key(cert_entry)
            self._config.update_metadata(order_id, {
                'last_issue_state': '',
                'csr_submitted_at': '',
                'pending_file_verify': '',
                'pending_verify_paths': [],
            })
            self._send_failure_callback(api, order_id, '无绑定站点可部署')
            raise RuntimeError("无绑定站点可部署，已清除签发状态，请重新绑定站点")

        results = self._deployer.deploy_multi(
            site_names=site_names,
            fullchain_pem=fullchain,
            key_pem=pending_key,
            order_id=order_id,
            domains=domains,
            api_client=api,
        )

        # 清理判据 = 私钥是否已被消费（任一站点部署成功即已写入站点），与
        # _check_deploy_results 是否抛错解耦：部分成功+站点缺失/删除时仍抛错上报，
        # 但私钥必须清理不泄漏；全失败（success=0）保留 pending key 供下轮重试（spec §3.8）
        if any(r.get('status') for r in results):
            self._cleanup_pending_key(cert_entry)
        return self._check_deploy_results(results, order_id)

    def _submit_new_csr(self, cert_entry, api):
        """生成并提交新的 CSR"""
        order_id = cert_entry['order_id']
        domains = cert_entry.get('domains', [])

        if not domains:
            raise RuntimeError("未配置域名")

        # 清理可能残留的 pending key（崩溃恢复场景）
        self._cleanup_pending_key(cert_entry)

        # 生成 CSR 和私钥
        csr_pem, key_pem, csr_hash = cert_utils.generate_csr(domains)

        # 保存 pending key
        self._save_pending_key(cert_entry, key_pem)

        # 递增重试计数（先持久化）
        meta = cert_entry.get('metadata', {})
        retry_count = meta.get('issue_retry_count', 0) + 1
        self._config.update_metadata(order_id, {
            'issue_retry_count': retry_count,
        })

        # 提交 CSR
        validation_method = cert_entry.get('validation_method', '')
        if validation_method:
            from .config import validate_validation_method
            err_msg = validate_validation_method(domains, validation_method)
            if err_msg:
                self._cleanup_pending_key(cert_entry)
                raise RuntimeError(err_msg)
        try:
            cert_data = api.submit_csr(order_id, csr_pem, domains,
                                       validation_method=validation_method)
        except APIError:
            self._cleanup_pending_key(cert_entry)
            raise

        self._update_renew_before_days(api)
        order_id = self._check_order_update(cert_entry, cert_data)
        status = cert_data.get('status', 'processing')
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        meta_update = {
            'csr_submitted_at': now,
            'last_csr_hash': csr_hash,
            'last_issue_state': status,
        }

        # 处理文件验证
        if status == 'processing':
            file_info = cert_data.get('file')
            if file_info and self._file_verifier:
                site_names = cert_entry.get('site_name', [])
                if isinstance(site_names, str):
                    site_names = [site_names] if site_names else []
                placed = self._file_verifier.place_file(file_info, site_names)
                meta_update['pending_file_verify'] = file_info
                meta_update['pending_verify_paths'] = placed

        self._config.update_metadata(order_id, meta_update)

        if self._logger:
            self._logger.info("CSR 已提交，等待签发: status=%s", status)
        return False

    def _cleanup_verify_files(self, meta):
        """清理 metadata 中记录的验证文件"""
        if not self._file_verifier:
            return
        paths = meta.get('pending_verify_paths', [])
        if paths:
            self._file_verifier.cleanup_files(paths)

    def _try_place_verify_file(self, cert_entry, cert_data):
        """检查 API 返回是否有新的验证文件需要放置"""
        if not self._file_verifier:
            return
        file_info = cert_data.get('file')
        if not file_info:
            return
        meta = cert_entry.get('metadata', {})
        old_file = meta.get('pending_file_verify', '')
        # 验证文件未变化则跳过
        if old_file and old_file == file_info:
            return
        # 清理旧文件，放置新文件
        self._cleanup_verify_files(meta)
        site_names = cert_entry.get('site_name', [])
        if isinstance(site_names, str):
            site_names = [site_names] if site_names else []
        placed = self._file_verifier.place_file(file_info, site_names)
        self._config.update_metadata(cert_entry['order_id'], {
            'pending_file_verify': file_info,
            'pending_verify_paths': placed,
        })

    def _pending_key_path(self, cert_entry):
        cert_name = cert_entry.get('cert_name', 'order-%s' % cert_entry.get('order_id'))
        return os.path.join(self._data_dir, 'pending-keys', cert_name, 'pending-key.pem')

    def _save_pending_key(self, cert_entry, key_pem):
        path = self._pending_key_path(cert_entry)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, key_pem.encode('utf-8'))
        finally:
            os.close(fd)

    def _read_pending_key(self, cert_entry):
        path = self._pending_key_path(cert_entry)
        if not os.path.isfile(path):
            return None
        if os.path.islink(path):
            return None  # 拒绝符号链接
        with open(path, 'r') as f:
            return f.read()

    def _cleanup_pending_key(self, cert_entry):
        path = self._pending_key_path(cert_entry)
        try:
            if os.path.isfile(path):
                os.remove(path)
            # 清理空目录
            dir_path = os.path.dirname(path)
            if os.path.isdir(dir_path) and not os.listdir(dir_path):
                os.rmdir(dir_path)
        except OSError as e:
            if self._logger:
                self._logger.error("清理 pending key 失败: %s, error=%s", os.path.basename(path), str(e))
