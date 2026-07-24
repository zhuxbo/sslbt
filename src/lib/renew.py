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
from .deployer import DeployError
from .config import (
    MAX_ISSUE_RETRY_COUNT, MAX_DEPLOY_ATTEMPT_COUNT,
    ISSUE_STATE_PROCESSING, ISSUE_STATE_CAPPED, ISSUE_STATE_EXPIRED,
    TERMINAL_ISSUE_STATES, CAP_STAGE_ISSUE, CAP_STAGE_DEPLOY,
    RENEW_MODE_LOCAL, derive_or_validate_renew_policy,
)

# 常量，对标 sslctl
RENEW_DEFAULT_DAYS = 14
MAX_RENEW_BEFORE_DAYS = 30  # 续签应在到期前 30 天内，超限视为服务端异常值（spec 2.9）
# MAX_ISSUE_RETRY_COUNT / MAX_DEPLOY_ATTEMPT_COUNT 由 config 统一定义（各自 >= 10 触顶）
SAFETY_MARGIN_HOURS = 24    # 自动动作安全余量：剩余有效期 < 此值不启动新动作（spec §3.2/§11）
RENEW_SLEEP_MIN = 5
RENEW_SLEEP_MAX = 120
SPREAD_TOTAL_MAX = 600  # 分散延迟总量上限（秒）
MAX_RENEW_BATCH = 100   # 单次续签证书数量上限

# 服务端"已在处理"类状态统一归一为 processing（只查询、不重复提交、不增计数、不重生 CSR）：
# 提交响应只会是 pending / processing；查询在 processing → active 之间可能出现短暂中间态 approving
_PROCESSING_ALIASES = ('processing', 'pending', 'approving')


def _normalize_issue_status(status):
    """归一服务端状态：pending / approving / 已在处理 → processing（spec §2.6/§3.5）"""
    return ISSUE_STATE_PROCESSING if status in _PROCESSING_ALIASES else status


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


def _remaining_hours(meta):
    """返回证书剩余有效期（小时）；到期时间未知（空/不可解析）返回 None"""
    expires_at = meta.get('cert_expires_at', '')
    if not expires_at:
        return None
    try:
        exp_dt = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None
    return (exp_dt - datetime.now(timezone.utc)).total_seconds() / 3600.0


def _deploy_callback_decision(results):
    """从底层部署结果推导编排层回调的 (status, message)。

    全部站点成功 → ('success', '')；任一明确失败 → ('failure', 各失败站点原因摘要)。
    与 deploy_multi 内部回调判定同构（metadata 落盘失败经 DeployError 单独路径处理）。
    """
    if all(r.get('status') for r in results):
        return 'success', ''
    fail_parts = ['%s: %s' % (r.get('site_name', ''), r.get('message', ''))
                  for r in results if not r.get('status')]
    return 'failure', '; '.join(fail_parts)


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
            meta = cert.get('metadata', {})
            state = meta.get('last_issue_state', '')

            # 触顶 / 过期 / policy 阻断为终态：静默跳过，不启动新动作、不发回调、不计数
            if state in TERMINAL_ISSUE_STATES:
                continue

            renew_mode = self._config.get_renew_mode(cert)

            # 触顶检查（计数分离）：签发（仅 local）与部署各自 >= 10 → 进入 CAPPED，不发回调
            # 签发触顶只拦截"新的 CSR 提交"；已进入 processing（CSR 已被接受）的证书继续轮询签发，
            # 不因签发计数被误判触顶而停止部署
            if renew_mode == RENEW_MODE_LOCAL and state != ISSUE_STATE_PROCESSING \
                    and meta.get('issue_retry_count', 0) >= MAX_ISSUE_RETRY_COUNT:
                self._enter_capped(cert, CAP_STAGE_ISSUE)
                continue
            if meta.get('deploy_attempt_count', 0) >= MAX_DEPLOY_ATTEMPT_COUNT:
                self._enter_capped(cert, CAP_STAGE_DEPLOY)
                continue

            # 到期准入：已过期 → 转 EXPIRED 静默；剩余 < 安全余量 → 本轮不启动新动作
            hours = _remaining_hours(meta)
            if hours is not None:
                if hours <= 0:
                    self._enter_expired(cert)
                    continue
                if hours < SAFETY_MARGIN_HOURS:
                    if self._logger:
                        self._logger.info(
                            "剩余有效期不足安全余量（%.1fh < %dh），本轮不启动新动作: order_id=%s",
                            hours, SAFETY_MARGIN_HOURS, order_id)
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
                if renew_mode == RENEW_MODE_LOCAL:
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

    # ==================== 触顶 / 过期 状态转移（静默，不发回调） ====================

    def _persist_meta(self, order_id, updates):
        """尽力持久化终态 metadata：失败仅记日志，不中断整批续签

        仅供可由下一轮前置条件重新推导的 CAPPED/EXPIRED 状态使用。部署意图与明确
        结果属于外部动作硬门禁，必须直接调用 update_metadata 并传播写入失败。
        """
        try:
            self._config.update_metadata(order_id, updates)
        except Exception as e:
            if self._logger:
                self._logger.warning("持久化 metadata 失败（非关键）: order_id=%s, error=%s", order_id, str(e))

    def _enter_capped(self, cert_entry, stage):
        """触顶：置 CAPPED 并记录阶段（issue/deploy），静默等待人工处理，不发回调"""
        order_id = cert_entry['order_id']
        self._persist_meta(order_id, {
            'last_issue_state': ISSUE_STATE_CAPPED,
            'cap_stage': stage,
        })
        meta = cert_entry.setdefault('metadata', {})
        meta['last_issue_state'] = ISSUE_STATE_CAPPED
        meta['cap_stage'] = stage
        if self._logger:
            self._logger.warning("证书触顶静默（阶段=%s），等待人工处理: order_id=%s", stage, order_id)

    def _enter_expired(self, cert_entry):
        """过期：置 EXPIRED 静默终止，仅留本地日志与人工入口，不发回调"""
        order_id = cert_entry['order_id']
        self._persist_meta(order_id, {'last_issue_state': ISSUE_STATE_EXPIRED})
        cert_entry.setdefault('metadata', {})['last_issue_state'] = ISSUE_STATE_EXPIRED
        if self._logger:
            self._logger.warning("证书已过期，静默终止，等待人工处理: order_id=%s", order_id)

    # ==================== 部署编排（计数 + 回调收敛到编排层） ====================

    def _begin_deploy_attempt(self, order_id, cert_entry):
        """递增部署计数并标记 started（持久化一个新的部署意图）。

        崩溃恢复重放同一意图不自增：若上轮已 started 未结束，复用同一尝试。
        返回本次尝试后的部署计数。
        """
        meta = cert_entry.setdefault('metadata', {})
        if meta.get('deploy_started'):
            return meta.get('deploy_attempt_count', 0)
        count = meta.get('deploy_attempt_count', 0) + 1
        self._config.update_metadata(order_id, {
            'deploy_attempt_count': count,
            'deploy_started': True,
        })
        meta['deploy_attempt_count'] = count
        meta['deploy_started'] = True
        return count

    def _conclude_deploy_attempt(self, order_id, cert_entry, counts_cleared):
        """结束一次部署尝试：清除 started 标记。

        counts_cleared=True（至少一个站点成功，deploy_multi 已清零计数与 started）时仅同步内存；
        否则（全失败/落盘前失败）保留部署计数，仅清 started 供下一轮作为新意图重新递增。
        """
        meta = cert_entry.setdefault('metadata', {})
        if counts_cleared:
            meta['deploy_attempt_count'] = 0
            meta['deploy_started'] = False
            return
        if meta.get('deploy_started'):
            self._config.update_metadata(order_id, {'deploy_started': False})
            meta['deploy_started'] = False

    def _deploy_and_report(self, cert_entry, api, fullchain, key_pem, site_names, domains, order_id):
        """编排层统一部署：递增部署意图 → 底层部署（抑制回调）→ 结果落盘后统一发部署结果回调。

        底层 deploy_multi 只返回结构化结果、不自行回调；每次成功与每次明确失败各尽力上报一次；
        第 10 次（最后一次）失败在 message 标注"已达重试上限"。返回底层结果列表。
        """
        count = self._begin_deploy_attempt(order_id, cert_entry)
        at_cap = count >= MAX_DEPLOY_ATTEMPT_COUNT
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        try:
            results = self._deployer.deploy_multi(
                site_names=site_names, fullchain_pem=fullchain, key_pem=key_pem,
                order_id=order_id, domains=domains, api_client=api, send_callback=False)
        except DeployError as e:
            # 底层在结果落盘前失败（校验 / metadata 写失败）：编排层补发失败回调后再抛出
            self._conclude_deploy_attempt(order_id, cert_entry, counts_cleared=False)
            self._send_deploy_callback(api, order_id, 'failure', now, str(e), at_cap)
            raise
        counts_cleared = any(r.get('status') for r in results)
        status, message = _deploy_callback_decision(results)
        self._conclude_deploy_attempt(order_id, cert_entry, counts_cleared)
        self._send_deploy_callback(api, order_id, status, now, message, at_cap)
        return results

    def _send_deploy_callback(self, api, order_id, status, deployed_at, message, at_cap):
        """编排层统一发送部署结果回调（非关键路径，失败仅记日志）。

        第 10 次（最后一次）部署失败在 message 标注"已达重试上限"（自由文本，零协议变化）。
        """
        if status == 'failure' and at_cap:
            message = ('%s；已达重试上限' % message) if message else '已达重试上限'
        try:
            api.callback(order_id=order_id, status=status, deployed_at=deployed_at, message=message)
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

        results = self._deploy_and_report(
            cert_entry, api, fullchain, private_key, site_names, domains, order_id)
        return self._check_deploy_results(results, order_id)

    def _renew_local(self, cert_entry, api):
        """Local 模式续签：生成 CSR → 提交 → 等待签发 → 部署"""
        order_id = cert_entry['order_id']
        meta = cert_entry.get('metadata', {})

        if self._logger:
            self._logger.info("Local 模式续签: order_id=%s", order_id)

        last_state = meta.get('last_issue_state', '')

        if last_state == ISSUE_STATE_PROCESSING:
            return self._handle_processing(cert_entry, api)

        # 存在在途 CSR（上轮 POST 结果不确定）：以同一 CSR 恢复，不重新生成、不增计数
        if self._has_pending_csr(cert_entry):
            return self._recover_pending_submit(cert_entry, api)

        # 到期时间未知且无在途 CSR：先查询 API 回填元数据再按正常逻辑判定，
        # 避免对"部署成功但元数据丢失/带外换证"的证书盲目重新提交 CSR（对齐 sslctl）
        if _expiry_unknown(meta):
            return self._refresh_and_maybe_renew_local(cert_entry, api)

        # 签发触顶防御（正常由前置过滤拦截并置 CAPPED；直接调用时在此静默停止，绝不第 11 次）
        if meta.get('issue_retry_count', 0) >= MAX_ISSUE_RETRY_COUNT:
            self._enter_capped(cert_entry, CAP_STAGE_ISSUE)
            return False

        return self._submit_new_csr(cert_entry, api)

    def _recover_pending_submit(self, cert_entry, api):
        """上轮 CSR 提交结果不确定：查询订单，据服务端实际状态恢复，绝不重复 POST。

        服务端状态机保证：提交成功响应只会是 pending/processing（均表示 CSR 已收到）；
        服务端未接收提交时返回错误信息（走明确业务拒绝路径清理 pending），不会以状态表达。
        因此恢复只需查询：
        - pending/processing/approving/active → 归一 processing 走查询路径
          （只 GET、不增计数、不重生 CSR）；active 则读 pending key 部署
        - 其他状态为 active 之后的订单终态 → 持久化后停止，等待人工处理
        """
        order_id = cert_entry['order_id']
        cert_data = api.query_order(order_id)
        self._update_renew_before_days(api)
        order_id = self._check_order_update(cert_entry, cert_data)
        status = _normalize_issue_status(cert_data.get('status', ''))

        if status in (ISSUE_STATE_PROCESSING, 'active'):
            # 已在处理/已签发：归一 processing，交由 processing 路径查询/部署
            self._config.update_metadata(order_id, {'last_issue_state': ISSUE_STATE_PROCESSING})
            cert_entry.setdefault('metadata', {})['last_issue_state'] = ISSUE_STATE_PROCESSING
            if self._logger:
                self._logger.info("在途 CSR 恢复：服务端已在处理，归一 processing: order_id=%s", order_id)
            return self._handle_processing(cert_entry, api)

        # 订单终态：持久化实际状态后停止，等待人工处理；仅状态变化时记 error，避免每轮重复刷日志
        meta = cert_entry.setdefault('metadata', {})
        if meta.get('last_issue_state', '') != status:
            self._config.update_metadata(order_id, {'last_issue_state': status})
            meta['last_issue_state'] = status
            if self._logger:
                self._logger.error("在途 CSR 查询到订单终态，停止自动提交等待人工处理: order_id=%s, status=%s",
                                   order_id, status)
        elif self._logger:
            self._logger.info("订单仍处于终态 %s，等待人工处理: order_id=%s", status, order_id)
        return False

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

        cert_info = cert_utils.parse_cert_info(
            certificate, logger=self._logger)
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

        # 查询订单状态（只 GET，不重复 POST，不增计数）
        cert_data = api.query_order(order_id)
        self._update_renew_before_days(api)
        order_id = self._check_order_update(cert_entry, cert_data)
        # pending / 已在处理 统一归一为 processing 继续等待（spec §2.6/§3.5）
        status = _normalize_issue_status(cert_data.get('status', ''))

        if status == ISSUE_STATE_PROCESSING:
            # 检查是否有新的验证文件需要放置
            self._try_place_verify_file(cert_entry, cert_data)
            if self._logger:
                self._logger.info("证书仍在处理中，继续等待")
            return False

        if status != 'active':
            # 仅在状态发生变化时记 error 并落盘，避免每轮 cron 重复刷日志/重复写文件
            if meta.get('last_issue_state', '') != status:
                if self._logger:
                    self._logger.error("证书状态异常: %s，持久化实际状态并等待人工处理", status)
                self._cleanup_verify_files(meta)
                self._config.update_metadata(order_id, {
                    'last_issue_state': status,
                    'pending_file_verify': '',
                    'pending_verify_paths': [],
                })
                meta['last_issue_state'] = status
                meta['pending_file_verify'] = ''
                meta['pending_verify_paths'] = []
            elif self._logger:
                self._logger.info("订单仍处于异常状态 %s，等待人工处理: order_id=%s", status, order_id)
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
            self._cleanup_pending_csr(cert_entry)
            self._config.update_metadata(order_id, {
                'last_issue_state': '',
                'csr_submitted_at': '',
                'pending_file_verify': '',
                'pending_verify_paths': [],
            })
            self._send_failure_callback(api, order_id, '无绑定站点可部署')
            raise RuntimeError("无绑定站点可部署，已清除签发状态，请重新绑定站点")

        results = self._deploy_and_report(
            cert_entry, api, fullchain, pending_key, site_names, domains, order_id)

        # 清理判据 = 私钥是否已被消费（任一站点部署成功即已写入站点），与
        # _check_deploy_results 是否抛错解耦：部分成功+站点缺失/删除时仍抛错上报，
        # 但私钥必须清理不泄漏；全失败（success=0）保留 pending key 供下轮重试（spec §3.8）
        if any(r.get('status') for r in results):
            self._cleanup_pending_key(cert_entry)
            self._cleanup_pending_csr(cert_entry)
        return self._check_deploy_results(results, order_id)

    def _submit_new_csr(self, cert_entry, api):
        """生成并提交新的 CSR（一次新的签发逻辑尝试，递增计数）"""
        order_id = cert_entry['order_id']
        domains = cert_entry.get('domains', [])

        if not domains:
            raise RuntimeError("未配置域名")

        # 清理可能残留的 pending（无在途 CSR 才走到这里）
        self._cleanup_pending_key(cert_entry)
        self._cleanup_pending_csr(cert_entry)

        # 校验/派生验证方式（SAN 含 IP 强制 file）；不兼容立即拒绝，不生成/不提交/不增计数
        _, validation_method, err = derive_or_validate_renew_policy(
            domains, cert_entry.get('renew_mode', ''), cert_entry.get('validation_method', ''))
        if err:
            raise RuntimeError(err)

        # 生成 CSR 和私钥
        csr_pem, key_pem, csr_hash = cert_utils.generate_csr(domains)

        # 网络请求前：原子持久化 pending key + CSR + CSR 哈希，并递增 issue_retry_count
        # （计数 = 持久化一个新的逻辑尝试意图；下轮恢复重放同一 CSR 不再递增）
        self._save_pending_key(cert_entry, key_pem)
        self._save_pending_csr(cert_entry, csr_pem)
        meta = cert_entry.setdefault('metadata', {})
        retry_count = meta.get('issue_retry_count', 0) + 1
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        self._config.update_metadata(order_id, {
            'issue_retry_count': retry_count,
            'last_csr_hash': csr_hash,
            'csr_submitted_at': now,
        })
        meta['issue_retry_count'] = retry_count
        meta['last_csr_hash'] = csr_hash

        return self._do_submit_csr(cert_entry, api, csr_pem, validation_method)

    def _do_submit_csr(self, cert_entry, api, csr_pem, validation_method):
        """执行 CSR 提交并处理响应。

        - 传输不确定（超时/断连/解析失败）：保留 pending key + CSR 作为在途标记，
          下轮查询订单状态恢复（不重复 POST），返回 False
        - 明确业务拒绝（含服务端未接收提交）：确认未创建新证书，清理 pending key + CSR，抛出
        - 成功：pending / 已在处理 归一 processing，放置验证文件，标记 processing
        """
        order_id = cert_entry['order_id']
        domains = cert_entry.get('domains', [])
        try:
            cert_data = api.submit_csr(order_id, csr_pem, domains,
                                       validation_method=validation_method)
        except APIError as e:
            if getattr(e, 'transport', False):
                # 不确定结果：保留 pending key + CSR，下轮以同一 CSR 恢复，不重生不增计数
                if self._logger:
                    self._logger.warning("CSR 提交结果不确定（%s），保留 pending 待下轮恢复: order_id=%s",
                                         str(e), order_id)
                return False
            # 明确业务拒绝：确认未创建新证书，清理 pending
            self._cleanup_pending_key(cert_entry)
            self._cleanup_pending_csr(cert_entry)
            raise

        self._update_renew_before_days(api)
        order_id = self._check_order_update(cert_entry, cert_data)
        # pending / 已在处理 统一归一 processing（只查询、不重复提交、不增计数、不重生 CSR）
        status = _normalize_issue_status(cert_data.get('status', 'processing'))

        meta_update = {'last_issue_state': status}
        if status == ISSUE_STATE_PROCESSING:
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

    # ==================== 在途 CSR 持久化（response-loss 恢复：作为在途标记，恢复走查询） ====================

    def _pending_csr_path(self, cert_entry):
        cert_name = cert_entry.get('cert_name', 'order-%s' % cert_entry.get('order_id'))
        return os.path.join(self._data_dir, 'pending-keys', cert_name, 'pending-csr.pem')

    def _has_pending_csr(self, cert_entry):
        """在途 CSR 存在（pending key + pending CSR 均在且非符号链接）= 上轮提交结果不确定，需恢复"""
        key_path = self._pending_key_path(cert_entry)
        csr_path = self._pending_csr_path(cert_entry)
        return (os.path.isfile(key_path) and not os.path.islink(key_path)
                and os.path.isfile(csr_path) and not os.path.islink(csr_path))

    def _save_pending_csr(self, cert_entry, csr_pem):
        path = self._pending_csr_path(cert_entry)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 先删残留再以 O_EXCL 创建，避免固定名被预置符号链接经 O_TRUNC 覆盖任意目标
        try:
            if os.path.lexists(path):
                os.remove(path)
        except OSError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, csr_pem.encode('utf-8'))
        finally:
            os.close(fd)

    def _cleanup_pending_csr(self, cert_entry):
        path = self._pending_csr_path(cert_entry)
        try:
            if os.path.lexists(path):
                os.remove(path)
            dir_path = os.path.dirname(path)
            if os.path.isdir(dir_path) and not os.listdir(dir_path):
                os.rmdir(dir_path)
        except OSError as e:
            if self._logger:
                self._logger.error("清理 pending CSR 失败: %s, error=%s", os.path.basename(path), str(e))
