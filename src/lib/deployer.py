"""证书部署模块。通过宝塔 panelSite.SetSSL() 部署证书。"""

import os
from datetime import datetime, timezone, timedelta

from . import cert_utils

# 宝塔站点证书文件目录（与 site_manager._check_ssl 的路径约定一致）
_BT_CERT_DIRS = (
    '/www/server/panel/vhost/cert/%s',
    '/www/server/panel/vhost/ssl/%s',
)

# 绑定站点在面板清单中连续缺失达此阈值才确认删除并解绑；
# 未达阈值仅记为"疑似删除"（本轮按失败上报但不解绑），缩小误清绑定的破坏半径
SITE_MISSING_CONFIRM_THRESHOLD = 2

# 相邻两次缺失观测计入计数的最小间隔（小时）：不足间隔的再次缺失不递增，
# 确认删除需要跨时间段的两轮观测，而非短时间内的两次探测（如数分钟内两次手动运行）
SITE_MISSING_MIN_INTERVAL_HOURS = 12


class _BtParams(dict):
    """兼容宝塔 API 的参数对象，支持属性和字典两种访问方式"""
    def __init__(self, **kw):
        super().__init__(**kw)

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

    def __setattr__(self, key, value):
        self[key] = value


class DeployError(Exception):
    """部署错误"""
    def __init__(self, message, phase='deploy', retryable=False):
        super().__init__(message)
        self.phase = phase
        self.retryable = retryable


# serviceReload 返回 ExecShell 的 (stdout, stderr)，stderr 含以下特征视为重载失败
_RELOAD_ERROR_MARKERS = ('emerg', 'alert', 'crit', 'fatal', 'error', 'failed', 'not running')


def _extract_reload_error(result):
    """从 serviceReload 返回值提取错误信息；stderr 无错误特征或形态无法识别时返回 ''"""
    if not isinstance(result, (tuple, list)) or len(result) < 2:
        return ''
    stderr = str(result[1] or '').strip()
    if not stderr:
        return ''
    lowered = stderr.lower()
    for marker in _RELOAD_ERROR_MARKERS:
        if marker in lowered:
            return stderr[:300]
    return ''


class Deployer:
    """证书部署器"""

    def __init__(self, config_manager, api_client=None, logger=None, site_manager=None):
        self._config = config_manager
        self._api = api_client
        self._logger = logger
        self._site_manager = site_manager

    def deploy_multi(self, site_names, fullchain_pem, key_pem, order_id=None,
                     domains=None, api_client=None, send_callback=True):
        """部署证书到多个站点，逐一执行，部分失败不中断

        send_callback：手动 deploy/setup 路径为 True（底层自行发回调，语义不变）；自动续签
        编排层传 False——底层只返回结构化结果、不自行发回调，由编排层在结果落盘后统一发送
        （deploy-spec §2.8/§5.1）。

        Returns: [{'site_name': str, 'status': bool, 'message': str}]
        """
        if self._logger:
            self._logger.info("开始多站点部署: sites=%s, order_id=%s", site_names, order_id)

        # 验证证书和私钥（只验证一次）
        ok, err = cert_utils.validate_cert_pem(fullchain_pem)
        if not ok:
            raise DeployError("证书验证失败: %s" % err, phase='validate')

        ok, err = cert_utils.validate_key_pem(key_pem)
        if not ok:
            raise DeployError("私钥验证失败: %s" % err, phase='validate')

        if not cert_utils.verify_cert_key_match(fullchain_pem, key_pem):
            raise DeployError("证书和私钥不匹配", phase='validate')

        # 检测面板中缺失的绑定站点并保守自愈：单次快照缺失仅记为"疑似删除"，
        # 跨最小间隔的连续两轮缺失才确认解绑，避免迁移/重装等中途的不完整快照误清绑定
        live_sites, missing_sites = self._detect_deleted_sites(site_names, order_id)
        suspected_sites, confirmed_sites = self._track_missing_sites(
            order_id, site_names, missing_sites)

        # 逐站点部署（仅存活站点）
        results = []
        for site_name in live_sites:
            try:
                self._set_ssl(site_name, fullchain_pem, key_pem)
                results.append({'site_name': site_name, 'status': True, 'message': '部署成功'})
                if self._logger:
                    self._logger.info("站点部署成功: site=%s", site_name)
            except Exception as e:
                results.append({'site_name': site_name, 'status': False, 'message': str(e)})
                if self._logger:
                    self._logger.warning("站点部署失败: site=%s, error=%s", site_name, str(e))

        success_count = sum(1 for r in results if r.get('status'))
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        # 仅在至少一个站点成功时更新 metadata（全部失败保留重试状态）
        # 解析或写入失败视为部署未完成：本地 cert_expires_at 缺失会导致 cron 永不接手
        meta_error = None
        if order_id and success_count > 0:
            cert_info = cert_utils.parse_cert_info(
                fullchain_pem, logger=self._logger)
            if not cert_info or not cert_info.get('not_after'):
                meta_error = '证书解析失败，无法记录到期时间'
            else:
                # 部署成功清零签发与部署状态（deploy-spec §3.8）
                meta = {
                    'last_deploy_at': now,
                    'cert_expires_at': cert_info['not_after'].strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'cert_serial': cert_info.get('serial', ''),
                    'last_issue_state': '',
                    'issue_retry_count': 0,
                    'deploy_attempt_count': 0,
                    'deploy_started': False,
                    'csr_submitted_at': '',
                    'last_csr_hash': '',
                }
                try:
                    self._config.update_metadata(order_id, meta)
                except Exception as e:
                    meta_error = 'metadata 更新失败: %s' % str(e)

        # 已确认删除（连续两轮缺失）的站点：从证书绑定中移除并持久化（自愈），计入结果供回调
        prune_succeeded = True
        if confirmed_sites and order_id:
            prune_succeeded = self._prune_deleted_sites(order_id, confirmed_sites)
        for sn in confirmed_sites:
            if prune_succeeded:
                results.append({'site_name': sn, 'status': False,
                                'message': '站点连续两轮缺失，已确认删除并解除绑定',
                                'site_removed': True})
            else:
                results.append({'site_name': sn, 'status': False,
                                'message': '站点连续两轮缺失，但解除绑定持久化失败',
                                'site_remove_failed': True})
        # 疑似删除（首轮缺失）：本轮按部署失败上报，但不解绑，等待下一轮二次确认
        for sn in suspected_sites:
            results.append({'site_name': sn, 'status': False,
                            'message': '站点疑似已删除，待下一轮确认（本轮暂不解绑）',
                            'site_missing': True})

        # 发送部署回调（任一站点失败或 metadata 未落盘即 failure，附各失败原因）
        # send_callback=False 时底层不发（自动续签编排层在结果落盘后统一发送）
        cb_api = api_client or self._api
        if send_callback and order_id and cb_api:
            all_success = all(r['status'] for r in results)
            fail_parts = [
                '%s: %s' % (r['site_name'], r['message'])
                for r in results if not r['status']
            ]
            if meta_error:
                fail_parts.append(meta_error)
            self._send_callback(
                order_id=order_id,
                status='success' if (all_success and not meta_error) else 'failure',
                deployed_at=now,
                api_client=cb_api,
                message='; '.join(fail_parts),
            )

        # metadata 解析/写入失败：即使站点已写入证书也视为部署未完成，抛错促使下次重试
        if meta_error:
            if self._logger:
                self._logger.error("部署未完成（%s）: sites=%s", meta_error, site_names)
            raise DeployError('部署未完成: %s' % meta_error, phase='metadata', retryable=True)

        return results

    def _detect_deleted_sites(self, site_names, order_id):
        """检测面板清单中缺失的绑定站点，返回 (live_sites, missing_sites)

        安全约束（防止误清绑定导致证书静默过期）：
        - 一次 deploy_multi 只查一次站点清单并复用，不逐站点扫库
        - 清单查询失败（SiteQueryError 等）：放弃本轮缺失判定，全部保守视为存在
        - 清单为空或形态异常：同样放弃判定——「面板零站点」多为 DB 迁移/重装等
          异常中间态，单次探测不足以支撑清空全部绑定的破坏性操作
        - 仅当清单查询成功且非空时，才把「不在清单中」判定为缺失（疑似删除，
          是否解绑由 _track_missing_sites 的连续两轮确认决定）
        """
        if self._site_manager is None:
            return list(site_names), []

        site_list = None
        try:
            site_list = self._site_manager.get_sites()
        except Exception as e:
            if self._logger:
                self._logger.warning("站点清单查询失败，跳过缺失检测（保守视为全部存在）: %s", str(e))
            return list(site_names), []

        existing = set()
        if isinstance(site_list, list):
            for s in site_list:
                if isinstance(s, dict) and s.get('name'):
                    existing.add(s['name'])
        if not existing:
            if self._logger:
                self._logger.warning("站点清单为空或形态异常，跳过缺失检测: order_id=%s", order_id)
            return list(site_names), []

        live_sites = [sn for sn in site_names if sn in existing]
        missing_sites = [sn for sn in site_names if sn not in existing]
        if missing_sites and self._logger:
            self._logger.warning("检测到面板缺失的绑定站点（待二次确认）: order_id=%s, sites=%s",
                                 order_id, ','.join(missing_sites))
        return live_sites, missing_sites

    def _load_missing_counts(self, order_id):
        """读取证书 metadata 中持久化的站点缺失跟踪（健壮解析，异常返回空）

        形态: {site: {'count': int, 'last_at': iso8601}}；非法条目直接丢弃
        （丢弃仅使确认周期重新起算，方向保守，不会导致误解绑）。
        """
        if not order_id:
            return {}
        try:
            cert = self._config.get_cert(order_id)
        except Exception:
            return {}
        if not isinstance(cert, dict):
            return {}
        meta = cert.get('metadata', {})
        if not isinstance(meta, dict):
            return {}
        raw = meta.get('site_missing_counts', {})
        if not isinstance(raw, dict):
            return {}
        counts = {}
        for k, v in raw.items():
            if not isinstance(v, dict):
                continue
            try:
                c = int(v.get('count', 0))
            except (TypeError, ValueError):
                continue
            if c > 0:
                counts[k] = {'count': c, 'last_at': str(v.get('last_at') or '')}
        return counts

    def _track_missing_sites(self, order_id, site_names, missing_sites):
        """更新站点缺失跟踪，返回 (suspected_sites, confirmed_sites)

        保守自愈——确认删除需要跨时间段的两轮观测，而非短时间内的两次探测：
        - 首次缺失记为疑似（count=1 并记录计入时间，本轮不解绑）
        - 距上次计入不足 SITE_MISSING_MIN_INTERVAL_HOURS 的再次缺失不递增计数
          （保持疑似态与失败上报），达到间隔才计为新一轮观测
        - 连续计入达 SITE_MISSING_CONFIRM_THRESHOLD 轮才确认删除
        - 存活/恢复/不再绑定的站点跟踪清零；无 order_id 无法持久化时全部视为疑似
        """
        counts = self._load_missing_counts(order_id)
        if not missing_sites and not counts:
            return [], []
        if not order_id:
            # 无法持久化缺失跟踪：保守全部视为疑似，绝不解绑
            return list(missing_sites), []

        now = datetime.now(timezone.utc)
        now_str = now.strftime('%Y-%m-%dT%H:%M:%SZ')
        min_interval = timedelta(hours=SITE_MISSING_MIN_INTERVAL_HOURS)
        missing_set = set(missing_sites)
        new_counts = {}
        suspected = []
        confirmed = []
        for sn in site_names:
            if sn not in missing_set:
                continue  # 存活站点：不写入 new_counts，等价跟踪清零
            entry = counts.get(sn)
            if entry is None:
                count, last_at = 1, now_str
            else:
                count, last_at = entry['count'], entry['last_at']
                prev = self._parse_iso_ts(last_at)
                if prev is None:
                    # 时间戳缺失/损坏：修复为当前时间，本轮不递增（保守方向）
                    last_at = now_str
                elif now - prev >= min_interval:
                    count = min(count + 1, SITE_MISSING_CONFIRM_THRESHOLD)
                    last_at = now_str
                # 间隔不足：计数与时间戳保持不变（锚定上次计入时刻，不滑动窗口）
            new_counts[sn] = {'count': count, 'last_at': last_at}
            if count >= SITE_MISSING_CONFIRM_THRESHOLD:
                confirmed.append(sn)
            else:
                suspected.append(sn)

        # 跟踪有变化才持久化（恢复/不再绑定的站点随 new_counts 重建而自动清理）
        if new_counts != counts:
            try:
                self._config.update_metadata(order_id, {'site_missing_counts': new_counts})
            except Exception as e:
                if self._logger:
                    self._logger.warning("更新站点缺失计数失败: order_id=%s, error=%s",
                                         order_id, str(e))

        if self._logger:
            if suspected:
                self._logger.warning("站点缺失疑似删除，待跨间隔二次确认: order_id=%s, sites=%s",
                                     order_id, ','.join(suspected))
            if confirmed:
                self._logger.warning("站点连续缺失已确认删除: order_id=%s, sites=%s",
                                     order_id, ','.join(confirmed))
        return suspected, confirmed

    @staticmethod
    def _parse_iso_ts(value):
        """解析 ISO8601 时间戳（Z 后缀），失败或缺少时区信息返回 None

        naive 串（无 Z/偏移，如人工篡改的 '2026-07-16T10:00:00'）解析虽成功，
        但与 aware now 相减会抛 TypeError，统一按损坏处理交由调用方修复分支。
        """
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except (ValueError, AttributeError):
            return None
        if parsed.tzinfo is None:
            return None
        return parsed

    def _prune_deleted_sites(self, order_id, deleted_sites):
        """将已删除的站点从证书绑定中移除并持久化，返回是否成功"""
        try:
            cert = self._config.get_cert(order_id)
            if not cert:
                return False
            current = cert.get('site_name', [])
            if isinstance(current, str):
                current = [current] if current else []
            remaining = [s for s in current if s not in deleted_sites]
            if remaining != current:
                self._config.update_cert(order_id, {'site_name': remaining})
                if self._logger:
                    self._logger.warning("已解除已删除站点的证书绑定: order_id=%s, sites=%s",
                                         order_id, ','.join(deleted_sites))
            return True
        except Exception as e:
            if self._logger:
                self._logger.warning("解除已删除站点绑定失败: order_id=%s, error=%s",
                                     order_id, str(e))
            return False

    def _set_ssl(self, site_name, cert_pem, key_pem):
        """调用宝塔 panelSite.SetSSL()，写入前置检查 + 白名单判定 + 写入后校验/回滚

        流程：
        1. Pre-flight：写入前校验既有 Web 配置，本就损坏则快速失败不写入
        2. 写入前捕获站点当前证书/私钥，用于回滚
        3. SetSSL 写入（仅 dict 且 status is True 视为成功，异常形态一律判失败）
        4. 写入后校验配置并重载，失败则恢复原证书后按 reload 阶段抛错
        """
        import panelSite

        # 1. Pre-flight：既有配置本就损坏时快速失败，避免误判为本次部署失败
        preflight_err = self._check_web_config()
        if preflight_err:
            raise DeployError(
                'Web 服务既有配置校验失败（非本次部署导致，请先修复配置）: %s' % preflight_err,
                phase='preflight', retryable=False)

        site_obj = panelSite.panelSite()

        # 2. 捕获站点当前证书/私钥，用于写入验证失败时回滚
        prev = self._capture_current_ssl(site_obj, site_name)

        # 3. SetSSL 写入
        params = _BtParams(
            type='1',
            siteName=site_name,
            key=key_pem,
            csr=cert_pem,  # 宝塔的 csr 参数实际是证书
        )
        # SetSSL 抛异常或返回非成功状态时，都可能已部分写入证书，尝试回滚到原证书
        try:
            result = site_obj.SetSSL(params)
        except Exception as e:
            self._rollback_after_setssl_failure(site_obj, site_name, prev)
            raise DeployError('SetSSL 写入异常: %s' % str(e), phase='deploy', retryable=True)
        if not (isinstance(result, dict) and result.get('status') is True):
            msg = ''
            if isinstance(result, dict):
                msg = str(result.get('msg') or '')
            if not msg:
                msg = 'SetSSL 返回异常形态: %r' % (result,)
            self._rollback_after_setssl_failure(site_obj, site_name, prev)
            raise DeployError(msg, phase='deploy', retryable=True)

        # 4. 写入后校验并重载，失败则回滚原证书
        verify_err = self._verify_web_service()
        if verify_err:
            rollback_note = self._rollback_ssl(site_obj, site_name, prev)
            raise DeployError('%s；%s' % (verify_err, rollback_note),
                              phase='reload', retryable=True)

        return result

    @staticmethod
    def _check_web_config():
        """运行宝塔 checkWebConfig，返回错误信息字符串或 None（配置正常/不可用）

        nginx 配置检查天然全局，任何站点的坏配置都会导致其返回错误。
        """
        import public
        check = getattr(public, 'checkWebConfig', None)
        if not callable(check):
            return None
        result = check()
        if result is not True:
            return str(result)
        return None

    def _verify_web_service(self):
        """SetSSL 后校验 Web 配置并重载，返回错误信息或 None（不再直接抛错）

        宝塔 SetSSL 内部虽会调用 serviceReload()，但丢弃其结果；此处显式复核：
        checkWebConfig 校验配置有效性，serviceReload 幂等重载并检查错误输出。
        """
        import public

        err = self._check_web_config()
        if err:
            return 'Web 服务配置校验失败: %s' % err

        reload_fn = getattr(public, 'serviceReload', None)
        if callable(reload_fn):
            rerr = _extract_reload_error(reload_fn())
            if rerr:
                return 'Web 服务重载失败: %s' % rerr
        return None

    @classmethod
    def _capture_current_ssl(cls, site_obj, site_name):
        """读取站点当前证书/私钥，用于回滚。无有效证书或读取失败时返回 None

        主路径 GetSSL：按 'csr' 键=证书、'key' 键=私钥解析（与 SetSSL 参数命名
        一致；宝塔各版本返回形态未逐一确证，属已知假设）。拿不到时回退读宝塔
        证书目录文件（fullchain.pem/privkey.pem）。
        """
        try:
            result = site_obj.GetSSL(_BtParams(siteName=site_name))
        except Exception:
            result = None
        if isinstance(result, dict):
            key = result.get('key', '')
            cert = result.get('csr', '')  # 宝塔 GetSSL 的 csr 字段是证书
            if key and cert:
                return {'key': key, 'cert': cert}
        return cls._read_site_cert_files(site_name)

    @staticmethod
    def _read_site_cert_files(site_name):
        """回退：从宝塔证书目录读取站点当前证书/私钥文件

        site_name 直接拼入证书目录路径，读取前校验其作为路径组件的安全性
        （拒绝穿越/绝对路径/分隔符），并在打开前对目标文件做符号链接检查，
        避免经构造站点名或预置符号链接读到证书目录外的任意文件。
        """
        if cert_utils.validate_site_name_component(site_name):
            return None
        for dir_tpl in _BT_CERT_DIRS:
            cert_dir = dir_tpl % site_name
            cert_path = os.path.join(cert_dir, 'fullchain.pem')
            key_path = os.path.join(cert_dir, 'privkey.pem')
            try:
                if not (os.path.isfile(cert_path) and os.path.isfile(key_path)):
                    continue
                if os.path.islink(cert_path) or os.path.islink(key_path):
                    continue  # 拒绝符号链接，避免读到目录外文件
                with open(cert_path, 'r') as f:
                    cert = f.read()
                with open(key_path, 'r') as f:
                    key = f.read()
                if cert and key:
                    return {'key': key, 'cert': cert}
            except OSError:
                continue
        return None

    def _rollback_after_setssl_failure(self, site_obj, site_name, prev):
        """SetSSL 写入失败（返回非成功状态或抛异常）后尝试回滚到原证书

        prev 为空（站点此前无有效证书）时为 no-op；回滚自身的异常仅记日志，
        不掩盖 SetSSL 的原始错误（原始错误由调用方抛出）。
        """
        if not prev:
            return
        try:
            note = self._rollback_ssl(site_obj, site_name, prev)
            if self._logger:
                self._logger.warning("SetSSL 写入失败，已尝试回滚原证书: site=%s, %s", site_name, note)
        except Exception as e:
            if self._logger:
                self._logger.error("SetSSL 写入失败后回滚异常: site=%s, error=%s", site_name, str(e))

    def _rollback_ssl(self, site_obj, site_name, prev):
        """写入验证失败后恢复站点原证书，返回状态说明（供错误信息与回调）"""
        if not prev:
            return '站点此前未配置有效 SSL，无原证书可回滚，请人工检查'
        try:
            params = _BtParams(type='1', siteName=site_name,
                               key=prev['key'], csr=prev['cert'])
            result = site_obj.SetSSL(params)
        except Exception as e:
            if self._logger:
                self._logger.error("回滚 SetSSL 异常: site=%s, error=%s", site_name, str(e))
            return '回滚异常（%s），站点证书可能处于异常状态，请人工检查' % str(e)

        if not (isinstance(result, dict) and result.get('status') is True):
            if self._logger:
                self._logger.error("回滚 SetSSL 失败: site=%s, result=%r", site_name, result)
            return '回滚失败，站点证书可能处于异常状态，请人工检查'

        verify_err = self._verify_web_service()
        if verify_err:
            if self._logger:
                self._logger.error("回滚后校验/重载失败: site=%s, %s", site_name, verify_err)
            return '已恢复原证书但重载仍失败（%s），请人工检查' % verify_err

        if self._logger:
            self._logger.warning("写入验证失败，已回滚到原证书: site=%s", site_name)
        return '已回滚到原证书'

    def _send_callback(self, order_id, status, deployed_at, api_client=None, message=''):
        """发送部署结果回调（非关键路径，失败仅记录日志）"""
        try:
            api_client.callback(
                order_id=order_id,
                status=status,
                deployed_at=deployed_at,
                message=message,
            )
        except Exception as e:
            if self._logger:
                self._logger.warning("部署回调失败（非关键）: %s", str(e))
