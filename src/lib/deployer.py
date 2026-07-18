"""证书部署模块。通过宝塔 panelSite.SetSSL() 部署证书。"""

from datetime import datetime, timezone

from . import cert_utils


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

    def __init__(self, config_manager, api_client=None, logger=None):
        self._config = config_manager
        self._api = api_client
        self._logger = logger

    def deploy(self, site_name, fullchain_pem, key_pem, order_id=None, domains=None,
               api_client=None):
        """部署证书到指定站点

        Args:
            site_name: 宝塔站点名称
            fullchain_pem: 完整证书链（叶子证书 + 中间证书）
            key_pem: 私钥 PEM
            order_id: 订单 ID（用于回调和更新配置）
            domains: 域名列表

        Returns:
            dict: {status, message}
        """
        if self._logger:
            self._logger.info("开始部署证书: site=%s, order_id=%s", site_name, order_id)

        # 验证证书和私钥
        ok, err = cert_utils.validate_cert_pem(fullchain_pem)
        if not ok:
            raise DeployError("证书验证失败: %s" % err, phase='validate')

        ok, err = cert_utils.validate_key_pem(key_pem)
        if not ok:
            raise DeployError("私钥验证失败: %s" % err, phase='validate')

        if not cert_utils.verify_cert_key_match(fullchain_pem, key_pem):
            raise DeployError("证书和私钥不匹配", phase='validate')

        # 通过宝塔 API 部署，失败时回调 failure（附原因）后抛出
        try:
            self._set_ssl(site_name, fullchain_pem, key_pem)
        except Exception as e:
            now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            cb_api = api_client or self._api
            if order_id and cb_api:
                self._send_callback(
                    order_id=order_id,
                    status='failure',
                    deployed_at=now,
                    api_client=cb_api,
                    message='%s: %s' % (site_name, str(e)),
                )
            raise DeployError("部署失败: %s" % str(e), phase='deploy', retryable=True)

        # 解析证书信息并更新 metadata；解析或写入失败视为部署未完成
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        meta_error = None
        if order_id:
            cert_info = cert_utils.parse_cert_info(fullchain_pem)
            if not cert_info or not cert_info.get('not_after'):
                meta_error = '证书解析失败，无法记录到期时间'
            else:
                meta = {
                    'last_deploy_at': now,
                    'cert_expires_at': cert_info['not_after'].strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'cert_serial': cert_info.get('serial', ''),
                    'last_issue_state': '',
                    'issue_retry_count': 0,
                    'csr_submitted_at': '',
                    'last_csr_hash': '',
                }
                try:
                    self._config.update_metadata(order_id, meta)
                except Exception as e:
                    meta_error = 'metadata 更新失败: %s' % str(e)

        # 发送部署回调：metadata 未落盘时回调 failure（本地状态不一致，需下次重试）
        cb_api = api_client or self._api
        if order_id and cb_api:
            self._send_callback(
                order_id=order_id,
                status='failure' if meta_error else 'success',
                deployed_at=now,
                api_client=cb_api,
                message=('%s: %s' % (site_name, meta_error)) if meta_error else '',
            )

        # metadata 解析/写入失败：站点虽已写入证书，但本地状态未落盘，视为部署未完成
        if meta_error:
            if self._logger:
                self._logger.error("部署未完成（%s）: site=%s", meta_error, site_name)
            raise DeployError('部署未完成: %s' % meta_error, phase='metadata', retryable=True)

        if self._logger:
            self._logger.info("证书部署成功: site=%s", site_name)

        return {'status': True, 'message': '部署成功'}

    def deploy_multi(self, site_names, fullchain_pem, key_pem, order_id=None,
                     domains=None, api_client=None):
        """部署证书到多个站点，逐一执行，部分失败不中断

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

        # 逐站点部署
        results = []
        for site_name in site_names:
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
            cert_info = cert_utils.parse_cert_info(fullchain_pem)
            if not cert_info or not cert_info.get('not_after'):
                meta_error = '证书解析失败，无法记录到期时间'
            else:
                meta = {
                    'last_deploy_at': now,
                    'cert_expires_at': cert_info['not_after'].strftime('%Y-%m-%dT%H:%M:%SZ'),
                    'cert_serial': cert_info.get('serial', ''),
                    'last_issue_state': '',
                    'issue_retry_count': 0,
                    'csr_submitted_at': '',
                    'last_csr_hash': '',
                }
                try:
                    self._config.update_metadata(order_id, meta)
                except Exception as e:
                    meta_error = 'metadata 更新失败: %s' % str(e)

        # 发送部署回调（任一站点失败或 metadata 未落盘即 failure，附各失败原因）
        cb_api = api_client or self._api
        if order_id and cb_api:
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
        result = site_obj.SetSSL(params)
        if not (isinstance(result, dict) and result.get('status') is True):
            msg = ''
            if isinstance(result, dict):
                msg = str(result.get('msg') or '')
            if not msg:
                msg = 'SetSSL 返回异常形态: %r' % (result,)
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

    @staticmethod
    def _capture_current_ssl(site_obj, site_name):
        """读取站点当前证书/私钥，用于回滚。无有效证书或读取失败时返回 None"""
        try:
            result = site_obj.GetSSL(_BtParams(siteName=site_name))
        except Exception:
            return None
        if not isinstance(result, dict):
            return None
        key = result.get('key', '')
        cert = result.get('csr', '')  # 宝塔 GetSSL 的 csr 字段是证书
        if key and cert:
            return {'key': key, 'cert': cert}
        return None

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
