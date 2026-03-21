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

        # 通过宝塔 API 部署
        try:
            self._set_ssl(site_name, fullchain_pem, key_pem)
        except Exception as e:
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
            try:
                self._config.update_metadata(order_id, meta)
            except Exception as e:
                if self._logger:
                    self._logger.warn("更新证书配置失败: %s", str(e))

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
                    self._logger.warn("站点部署失败: site=%s, error=%s", site_name, str(e))

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
            try:
                self._config.update_metadata(order_id, meta)
            except Exception as e:
                if self._logger:
                    self._logger.warn("更新证书配置失败: %s", str(e))

        # 发送部署回调（任一站点失败即 failure）
        cb_api = api_client or self._api
        if order_id and cb_api:
            all_success = all(r['status'] for r in results)
            self._send_callback(
                order_id=order_id,
                status='success' if all_success else 'failure',
                deployed_at=now,
                api_client=cb_api,
            )

        return results

    def _set_ssl(self, site_name, cert_pem, key_pem):
        """调用宝塔 panelSite.SetSSL()"""
        import panelSite

        params = _BtParams(
            type='1',
            siteName=site_name,
            key=key_pem,
            csr=cert_pem,  # 宝塔的 csr 参数实际是证书
        )

        site_obj = panelSite.panelSite()
        result = site_obj.SetSSL(params)

        if isinstance(result, dict) and result.get('status') is False:
            raise DeployError(result.get('msg', '部署失败'), phase='deploy', retryable=True)

        return result

    def _send_callback(self, order_id, status, deployed_at, api_client=None):
        """发送部署结果回调（非关键路径，失败仅记录日志）"""
        try:
            api_client.callback(
                order_id=order_id,
                status=status,
                deployed_at=deployed_at,
            )
        except Exception as e:
            if self._logger:
                self._logger.warn("部署回调失败（非关键）: %s", str(e))
