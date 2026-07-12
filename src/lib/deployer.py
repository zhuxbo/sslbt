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

        # 解析证书信息
        cert_info = cert_utils.parse_cert_info(fullchain_pem)
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

        # 更新配置中的 metadata
        if order_id:
            meta = {
                'last_deploy_at': now,
            }
            if cert_info:
                if cert_info.get('not_after'):
                    meta['cert_expires_at'] = cert_info['not_after'].strftime('%Y-%m-%dT%H:%M:%SZ')
                if cert_info.get('serial'):
                    meta['cert_serial'] = cert_info['serial']
            meta['last_issue_state'] = ''
            meta['issue_retry_count'] = 0
            meta['csr_submitted_at'] = ''
            meta['last_csr_hash'] = ''
            try:
                self._config.update_metadata(order_id, meta)
            except Exception as e:
                if self._logger:
                    self._logger.warning("更新证书配置失败: %s", str(e))

        # 发送部署回调
        cb_api = api_client or self._api
        if order_id and cb_api:
            self._send_callback(
                order_id=order_id,
                status='success',
                deployed_at=now,
                api_client=cb_api,
            )

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
        if order_id and success_count > 0:
            cert_info = cert_utils.parse_cert_info(fullchain_pem)
            meta = {
                'last_deploy_at': now,
            }
            if cert_info:
                if cert_info.get('not_after'):
                    meta['cert_expires_at'] = cert_info['not_after'].strftime('%Y-%m-%dT%H:%M:%SZ')
                if cert_info.get('serial'):
                    meta['cert_serial'] = cert_info['serial']
            meta['last_issue_state'] = ''
            meta['issue_retry_count'] = 0
            meta['csr_submitted_at'] = ''
            meta['last_csr_hash'] = ''
            try:
                self._config.update_metadata(order_id, meta)
            except Exception as e:
                if self._logger:
                    self._logger.warning("更新证书配置失败: %s", str(e))

        # 发送部署回调（任一站点失败即 failure，附各站点失败原因）
        cb_api = api_client or self._api
        if order_id and cb_api:
            all_success = all(r['status'] for r in results)
            fail_msgs = '; '.join(
                '%s: %s' % (r['site_name'], r['message'])
                for r in results if not r['status']
            )
            self._send_callback(
                order_id=order_id,
                status='success' if all_success else 'failure',
                deployed_at=now,
                api_client=cb_api,
                message=fail_msgs,
            )

        return results

    def _set_ssl(self, site_name, cert_pem, key_pem):
        """调用宝塔 panelSite.SetSSL()，白名单判定结果并校验服务重载

        仅「dict 且 status is True」（宝塔 returnMsg 的固定成功形态）视为成功，
        非 dict、缺 status 键等异常形态一律判失败，避免失败被误报为成功。
        """
        import panelSite

        params = _BtParams(
            type='1',
            siteName=site_name,
            key=key_pem,
            csr=cert_pem,  # 宝塔的 csr 参数实际是证书
        )

        site_obj = panelSite.panelSite()
        result = site_obj.SetSSL(params)

        if not (isinstance(result, dict) and result.get('status') is True):
            msg = ''
            if isinstance(result, dict):
                msg = str(result.get('msg') or '')
            if not msg:
                msg = 'SetSSL 返回异常形态: %r' % (result,)
            raise DeployError(msg, phase='deploy', retryable=True)

        self._verify_web_service()
        return result

    def _verify_web_service(self):
        """SetSSL 后校验 Web 配置并重载，失败视为部署失败

        宝塔 SetSSL 内部虽会调用 serviceReload()，但丢弃其结果；此处显式复核：
        checkWebConfig 校验配置有效性，serviceReload 幂等重载并检查错误输出。
        """
        import public

        check = getattr(public, 'checkWebConfig', None)
        if callable(check):
            check_result = check()
            if check_result is not True:
                raise DeployError('Web 服务配置校验失败: %s' % check_result,
                                  phase='reload', retryable=True)

        reload_fn = getattr(public, 'serviceReload', None)
        if callable(reload_fn):
            err = _extract_reload_error(reload_fn())
            if err:
                raise DeployError('Web 服务重载失败: %s' % err,
                                  phase='reload', retryable=True)

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
