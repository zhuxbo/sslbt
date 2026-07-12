"""部署器测试"""

import pytest
from unittest.mock import MagicMock, patch

from lib.deployer import Deployer, DeployError
from lib.config import ConfigManager


class TestDeployer:
    @pytest.fixture
    def deployer(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        api = MagicMock()
        api.callback.return_value = {'code': 1}
        logger = MagicMock()
        return Deployer(config, api, logger)

    def test_deploy_invalid_cert(self, deployer):
        with pytest.raises(DeployError, match='证书验证失败'):
            deployer.deploy('test-site', 'bad cert', 'bad key')

    def test_deploy_invalid_key(self, deployer):
        cert = "-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----"
        with pytest.raises(DeployError, match='私钥验证失败'):
            deployer.deploy('test-site', cert, 'bad key')

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_deploy_success(self, mock_set_ssl, mock_cert_utils, deployer, tmp_data_dir):
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = {
            'common_name': 'example.com',
            'serial': 'ABC123',
        }
        mock_set_ssl.return_value = {'status': True}

        config = ConfigManager(tmp_data_dir)
        config.add_cert(12345, 'test', ['example.com'], site_name='example.com')

        deployer._config = config
        result = deployer.deploy(
            site_name='example.com',
            fullchain_pem='cert-pem',
            key_pem='key-pem',
            order_id=12345,
            domains=['example.com'],
        )
        assert result['status'] is True
        mock_set_ssl.assert_called_once()

    @patch('lib.deployer.cert_utils')
    def test_deploy_key_mismatch(self, mock_cert_utils, deployer):
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = False

        with pytest.raises(DeployError, match='不匹配'):
            deployer.deploy('test-site', 'cert', 'key')

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_deploy_multi_site(self, mock_set_ssl, mock_cert_utils, deployer, tmp_data_dir):
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = {
            'common_name': 'a.com',
            'serial': 'ABC123',
        }
        mock_set_ssl.return_value = {'status': True}

        config = ConfigManager(tmp_data_dir)
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1.a.com', 's2.a.com'])

        deployer._config = config
        results = deployer.deploy_multi(
            site_names=['s1.a.com', 's2.a.com'],
            fullchain_pem='cert-pem',
            key_pem='key-pem',
            order_id=12345,
            domains=['a.com'],
        )
        assert len(results) == 2
        assert all(r['status'] is True for r in results)
        assert mock_set_ssl.call_count == 2

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_deploy_multi_partial_failure(self, mock_set_ssl, mock_cert_utils, deployer, tmp_data_dir):
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = {
            'common_name': 'a.com',
            'serial': 'ABC123',
        }
        mock_set_ssl.side_effect = [{'status': True}, Exception('部署超时')]

        config = ConfigManager(tmp_data_dir)
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1.a.com', 's2.a.com'])

        deployer._config = config
        results = deployer.deploy_multi(
            site_names=['s1.a.com', 's2.a.com'],
            fullchain_pem='cert-pem',
            key_pem='key-pem',
            order_id=12345,
            domains=['a.com'],
        )
        assert results[0]['status'] is True
        assert results[1]['status'] is False


    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_deploy_multi_all_fail_no_metadata_update(self, mock_set_ssl, mock_cert_utils, deployer, tmp_data_dir):
        """全部站点失败时不更新 metadata（保留重试状态）"""
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_set_ssl.side_effect = Exception('部署超时')

        config = ConfigManager(tmp_data_dir)
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1.a.com', 's2.a.com'])
        # 设置初始 metadata（模拟 Local 模式的重试状态）
        config.update_metadata(12345, {
            'last_issue_state': 'processing',
            'issue_retry_count': 3,
        })

        deployer._config = config
        results = deployer.deploy_multi(
            site_names=['s1.a.com', 's2.a.com'],
            fullchain_pem='cert-pem',
            key_pem='key-pem',
            order_id=12345,
            domains=['a.com'],
        )
        assert all(r['status'] is False for r in results)
        # metadata 不应被更新（重试状态保留）
        cert = config.get_cert(12345)
        assert cert['metadata']['last_issue_state'] == 'processing'
        assert cert['metadata']['issue_retry_count'] == 3

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_deploy_multi_partial_success_updates_metadata(self, mock_set_ssl, mock_cert_utils, deployer, tmp_data_dir):
        """部分成功时正常更新 metadata"""
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = {
            'common_name': 'a.com',
            'serial': 'DEF456',
        }
        mock_set_ssl.side_effect = [{'status': True}, Exception('超时')]

        config = ConfigManager(tmp_data_dir)
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1.a.com', 's2.a.com'])
        config.update_metadata(12345, {
            'last_issue_state': 'processing',
            'issue_retry_count': 3,
        })

        deployer._config = config
        results = deployer.deploy_multi(
            site_names=['s1.a.com', 's2.a.com'],
            fullchain_pem='cert-pem',
            key_pem='key-pem',
            order_id=12345,
            domains=['a.com'],
        )
        assert results[0]['status'] is True
        assert results[1]['status'] is False
        # metadata 应被更新（部分成功）
        cert = config.get_cert(12345)
        assert cert['metadata']['last_issue_state'] == ''
        assert cert['metadata']['issue_retry_count'] == 0
        assert cert['metadata']['last_deploy_at'] != ''


class TestSetSSLResultWhitelist:
    """SetSSL 结果白名单判定（P1-13）：仅 dict 且 status is True 视为成功，其余形态一律判失败"""

    @pytest.fixture
    def deployer(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        return Deployer(config, MagicMock(), MagicMock())

    @staticmethod
    def _patch_result(monkeypatch, result):
        import panelSite
        monkeypatch.setattr(panelSite.panelSite, 'SetSSL', lambda self, params: result)

    def test_none_result_fails(self, deployer, monkeypatch):
        self._patch_result(monkeypatch, None)
        with pytest.raises(DeployError):
            deployer._set_ssl('test.example.com', 'cert', 'key')

    def test_string_result_fails(self, deployer, monkeypatch):
        self._patch_result(monkeypatch, 'unexpected error text')
        with pytest.raises(DeployError):
            deployer._set_ssl('test.example.com', 'cert', 'key')

    def test_missing_status_key_fails(self, deployer, monkeypatch):
        self._patch_result(monkeypatch, {'msg': '设置成功'})
        with pytest.raises(DeployError):
            deployer._set_ssl('test.example.com', 'cert', 'key')

    def test_truthy_non_bool_status_fails(self, deployer, monkeypatch):
        # 宝塔 returnMsg 恒为 bool，非 bool 真值属异常形态，按白名单判失败
        self._patch_result(monkeypatch, {'status': 1, 'msg': '设置成功'})
        with pytest.raises(DeployError):
            deployer._set_ssl('test.example.com', 'cert', 'key')

    def test_status_false_fails_with_msg(self, deployer, monkeypatch):
        self._patch_result(monkeypatch, {'status': False, 'msg': '指定站点不存在'})
        with pytest.raises(DeployError, match='指定站点不存在'):
            deployer._set_ssl('test.example.com', 'cert', 'key')

    def test_status_true_succeeds(self, deployer, monkeypatch):
        self._patch_result(monkeypatch, {'status': True, 'msg': '设置成功'})
        result = deployer._set_ssl('test.example.com', 'cert', 'key')
        assert result['status'] is True


class TestReloadJudgment:
    """reload 结果纳入部署成败判定（P1-13）"""

    @pytest.fixture
    def deployer(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        return Deployer(config, MagicMock(), MagicMock())

    @pytest.fixture(autouse=True)
    def _set_ssl_ok(self, monkeypatch):
        import panelSite
        monkeypatch.setattr(panelSite.panelSite, 'SetSSL',
                            lambda self, params: {'status': True, 'msg': '设置成功'})

    def test_web_config_error_fails(self, deployer, monkeypatch):
        import public
        monkeypatch.setattr(public, 'checkWebConfig',
                            lambda: 'nginx: [emerg] invalid parameter')
        with pytest.raises(DeployError, match='配置'):
            deployer._set_ssl('test.example.com', 'cert', 'key')

    def test_web_config_error_phase_is_reload(self, deployer, monkeypatch):
        import public
        monkeypatch.setattr(public, 'checkWebConfig', lambda: 'nginx: [emerg] boom')
        with pytest.raises(DeployError) as exc_info:
            deployer._set_ssl('test.example.com', 'cert', 'key')
        assert exc_info.value.phase == 'reload'

    def test_reload_stderr_error_fails(self, deployer, monkeypatch):
        import public
        monkeypatch.setattr(public, 'serviceReload',
                            lambda: ('', 'Job for nginx.service failed'))
        with pytest.raises(DeployError, match='重载'):
            deployer._set_ssl('test.example.com', 'cert', 'key')

    def test_reload_warning_passes(self, deployer, monkeypatch):
        # stderr 中的 warn 不应误判为失败
        import public
        monkeypatch.setattr(public, 'serviceReload',
                            lambda: ('', 'nginx: [warn] conflicting server name "a.com"'))
        result = deployer._set_ssl('test.example.com', 'cert', 'key')
        assert result['status'] is True

    def test_reload_clean_passes(self, deployer):
        result = deployer._set_ssl('test.example.com', 'cert', 'key')
        assert result['status'] is True


class TestFailureCallback:
    """失败必须回调 failure 并携带原因（P1-13）"""

    @staticmethod
    def _make(tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        api = MagicMock()
        api.callback.return_value = {'code': 1}
        return Deployer(config, api, MagicMock()), api, config

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_deploy_multi_failure_callback_with_message(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        deployer, api, config = self._make(tmp_data_dir)
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = {}
        mock_set_ssl.side_effect = [{'status': True}, DeployError('nginx 配置校验失败')]
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1', 's2'])

        deployer.deploy_multi(['s1', 's2'], 'cert', 'key', order_id=12345)

        kwargs = api.callback.call_args.kwargs
        assert kwargs['status'] == 'failure'
        assert 's2' in kwargs.get('message', '')
        assert 'nginx 配置校验失败' in kwargs.get('message', '')

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_deploy_multi_success_callback_no_message(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        deployer, api, config = self._make(tmp_data_dir)
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = {}
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1'])

        deployer.deploy_multi(['s1'], 'cert', 'key', order_id=12345)

        kwargs = api.callback.call_args.kwargs
        assert kwargs['status'] == 'success'
        assert not kwargs.get('message')

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_deploy_single_failure_sends_failure_callback(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        deployer, api, config = self._make(tmp_data_dir)
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_set_ssl.side_effect = DeployError('SetSSL 返回异常形态: None')
        config.add_cert(12345, 'test', ['a.com'], site_name='s1')

        with pytest.raises(DeployError):
            deployer.deploy('s1', 'cert', 'key', order_id=12345)

        kwargs = api.callback.call_args.kwargs
        assert kwargs['status'] == 'failure'
        assert 'SetSSL 返回异常形态' in kwargs.get('message', '')


class TestDeployError:
    def test_error_attributes(self):
        err = DeployError('test error', phase='validate', retryable=True)
        assert str(err) == 'test error'
        assert err.phase == 'validate'
        assert err.retryable is True
