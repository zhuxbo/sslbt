"""部署器测试"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from lib.deployer import Deployer, DeployError
from lib.config import ConfigManager

# 有效的未来到期时间，供 parse_cert_info mock 使用（部署成功必须能记录到期时间）
_FUTURE_EXPIRY = datetime(2035, 1, 1, tzinfo=timezone.utc)


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
            'not_after': _FUTURE_EXPIRY,
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
            'not_after': _FUTURE_EXPIRY,
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
            'not_after': _FUTURE_EXPIRY,
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
            'not_after': _FUTURE_EXPIRY,
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
        # 到期时间应被回填（避免 cron 因空 expires_at 永不接手）
        assert cert['metadata']['cert_expires_at'] != ''


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
        panelSite.panelSite.reset()
        monkeypatch.setattr(panelSite.panelSite, 'SetSSL',
                            lambda self, params: {'status': True, 'msg': '设置成功'})

    def test_web_config_error_fails(self, deployer, monkeypatch):
        import public
        monkeypatch.setattr(public, 'checkWebConfig',
                            lambda: 'nginx: [emerg] invalid parameter')
        with pytest.raises(DeployError, match='配置'):
            deployer._set_ssl('test.example.com', 'cert', 'key')

    def test_web_config_error_phase_is_reload(self, deployer, monkeypatch):
        """写入后配置校验失败归类为 reload 阶段（pre-flight 通过，写入后才检出）"""
        import public
        calls = {'n': 0}

        def staged_check():
            calls['n'] += 1
            return True if calls['n'] == 1 else 'nginx: [emerg] boom'

        monkeypatch.setattr(public, 'checkWebConfig', staged_check)
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


class TestPreflightAndRollback:
    """SetSSL 写入前置检查与验证失败回滚（B3）"""

    @pytest.fixture
    def deployer(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        return Deployer(config, MagicMock(), MagicMock())

    @pytest.fixture(autouse=True)
    def _clean_ssl(self):
        import panelSite
        panelSite.panelSite.reset()
        yield
        panelSite.panelSite.reset()

    def test_preflight_rejects_broken_config(self, deployer, monkeypatch):
        """既有配置损坏时 pre-flight 快速失败，不调用 SetSSL"""
        import panelSite
        import public
        calls = {'setssl': 0}

        def counting_setssl(self, params):
            calls['setssl'] += 1
            return {'status': True}

        monkeypatch.setattr(panelSite.panelSite, 'SetSSL', counting_setssl)
        monkeypatch.setattr(public, 'checkWebConfig', lambda: 'nginx: [emerg] pre-existing error')
        with pytest.raises(DeployError) as exc:
            deployer._set_ssl('test.example.com', 'cert', 'key')
        assert exc.value.phase == 'preflight'
        assert calls['setssl'] == 0  # 既有配置损坏，不应写入新证书

    def test_setssl_ok_reload_fail_rolls_back(self, deployer, monkeypatch):
        """SetSSL 成功但 reload 失败 → 回滚到原证书（用 mock 模拟）"""
        import panelSite
        import public
        panelSite.panelSite._ssl_data['test.example.com'] = {'key': 'OLD-KEY', 'cert': 'OLD-CERT'}

        reload_calls = {'n': 0}

        def staged_reload():
            reload_calls['n'] += 1
            # 新证书写入后第一次 reload 失败触发回滚，回滚后恢复正常
            return ('', 'Job for nginx.service failed') if reload_calls['n'] == 1 else ('', '')

        monkeypatch.setattr(public, 'serviceReload', staged_reload)
        with pytest.raises(DeployError, match='已回滚'):
            deployer._set_ssl('test.example.com', 'NEW-CERT', 'NEW-KEY')
        # 原证书已恢复
        assert panelSite.panelSite._ssl_data['test.example.com'] == {'key': 'OLD-KEY', 'cert': 'OLD-CERT'}

    def test_rollback_no_prev_cert(self, deployer, monkeypatch):
        """SetSSL 成功但 reload 失败且站点无原证书 → 提示无法回滚"""
        import public
        monkeypatch.setattr(public, 'serviceReload', lambda: ('', 'Job for nginx.service failed'))
        with pytest.raises(DeployError, match='无原证书可回滚'):
            deployer._set_ssl('newsite.example.com', 'NEW-CERT', 'NEW-KEY')

    def test_rollback_also_fails_needs_manual(self, deployer, monkeypatch):
        """回滚后 reload 仍失败 → 提示人工检查"""
        import panelSite
        import public
        panelSite.panelSite._ssl_data['test.example.com'] = {'key': 'OLD-KEY', 'cert': 'OLD-CERT'}
        monkeypatch.setattr(public, 'serviceReload', lambda: ('', 'Job for nginx.service failed'))
        with pytest.raises(DeployError, match='请人工检查'):
            deployer._set_ssl('test.example.com', 'NEW-CERT', 'NEW-KEY')

    def test_clean_deploy_writes_new_cert(self, deployer):
        """配置正常时正常写入，不触发回滚，站点持有新证书"""
        import panelSite
        result = deployer._set_ssl('freshsite.example.com', 'NEW-CERT', 'NEW-KEY')
        assert result['status'] is True
        assert panelSite.panelSite._ssl_data['freshsite.example.com'] == {'key': 'NEW-KEY', 'cert': 'NEW-CERT'}


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
        mock_cert_utils.parse_cert_info.return_value = {'not_after': _FUTURE_EXPIRY}
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
        mock_cert_utils.parse_cert_info.return_value = {'not_after': _FUTURE_EXPIRY}
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


class TestMetadataFailure:
    """metadata 解析/写入失败必须回调 failure，不得误报成功（B1）"""

    @staticmethod
    def _make(tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        api = MagicMock()
        api.callback.return_value = {'code': 1}
        return Deployer(config, api, MagicMock()), api, config

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_deploy_multi_parse_failure_callbacks_failure(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """证书解析失败（parse 返回 None）→ 回调 failure 并抛 DeployError，metadata 不落盘"""
        deployer, api, config = self._make(tmp_data_dir)
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = None
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1'])

        with pytest.raises(DeployError, match='部署未完成'):
            deployer.deploy_multi(['s1'], 'cert', 'key', order_id=12345)

        kwargs = api.callback.call_args.kwargs
        assert kwargs['status'] == 'failure'
        assert '到期时间' in kwargs.get('message', '')
        # cert_expires_at 未被回填，下次 cron 会因空值重新接手
        cert = config.get_cert(12345)
        assert cert['metadata']['cert_expires_at'] == ''

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_deploy_multi_no_not_after_callbacks_failure(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """证书解析结果缺 not_after → 回调 failure 并抛 DeployError"""
        deployer, api, config = self._make(tmp_data_dir)
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = {'serial': 'ABC'}
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1'])

        with pytest.raises(DeployError, match='部署未完成'):
            deployer.deploy_multi(['s1'], 'cert', 'key', order_id=12345)

        assert api.callback.call_args.kwargs['status'] == 'failure'

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_deploy_multi_metadata_write_failure_callbacks_failure(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """metadata 写入抛异常 → 回调 failure 并抛 DeployError"""
        deployer, api, config = self._make(tmp_data_dir)
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = {'not_after': _FUTURE_EXPIRY, 'serial': 'X'}
        mock_set_ssl.return_value = {'status': True}
        # 用抛异常的 config 替换
        bad_config = MagicMock()
        bad_config.update_metadata.side_effect = OSError('disk full')
        deployer._config = bad_config

        with pytest.raises(DeployError, match='部署未完成'):
            deployer.deploy_multi(['s1'], 'cert', 'key', order_id=12345)

        kwargs = api.callback.call_args.kwargs
        assert kwargs['status'] == 'failure'
        assert 'metadata' in kwargs.get('message', '')

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_deploy_single_parse_failure_callbacks_failure(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """单站点 deploy：证书解析失败 → 回调 failure 并抛 DeployError"""
        deployer, api, config = self._make(tmp_data_dir)
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = None
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_name='s1')

        with pytest.raises(DeployError, match='部署未完成'):
            deployer.deploy('s1', 'cert', 'key', order_id=12345)

        assert api.callback.call_args.kwargs['status'] == 'failure'

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_deploy_single_success_still_callbacks_success(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """单站点 deploy：解析与写入均成功 → 回调 success 并返回成功"""
        deployer, api, config = self._make(tmp_data_dir)
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = {'not_after': _FUTURE_EXPIRY, 'serial': 'X'}
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_name='s1')

        result = deployer.deploy('s1', 'cert', 'key', order_id=12345)
        assert result['status'] is True
        assert api.callback.call_args.kwargs['status'] == 'success'


class TestDeletedSiteSelfHeal:
    """站点删除后的续签自愈（B7）"""

    @staticmethod
    def _make(tmp_data_dir, site_list):
        """site_list: get_sites() 的返回值（模拟真实语义：站点 dict 列表）"""
        config = ConfigManager(tmp_data_dir)
        api = MagicMock()
        api.callback.return_value = {'code': 1}
        site_mgr = MagicMock()
        site_mgr.get_sites.return_value = site_list
        deployer = Deployer(config, api, MagicMock(), site_mgr)
        return deployer, api, config

    @staticmethod
    def _mock_cert_ok(mock_cert_utils):
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = {'not_after': _FUTURE_EXPIRY, 'serial': 'X'}

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_partial_deleted_site_pruned(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """部分站点已删：存活站点部署，已删站点解除绑定，回调 failure 带原因"""
        deployer, api, config = self._make(tmp_data_dir, [{'name': 'live.com', 'path': '/w'}])
        self._mock_cert_ok(mock_cert_utils)
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['live.com', 'deleted.com'])

        results = deployer.deploy_multi(['live.com', 'deleted.com'], 'cert', 'key', order_id=12345)

        live_r = next(r for r in results if r['site_name'] == 'live.com')
        del_r = next(r for r in results if r['site_name'] == 'deleted.com')
        assert live_r['status'] is True
        assert del_r['status'] is False
        assert '已删除' in del_r['message']
        assert del_r.get('site_removed') is True
        # 只部署存活站点，且一次 deploy_multi 只查一次站点清单
        mock_set_ssl.assert_called_once_with('live.com', 'cert', 'key')
        assert deployer._site_manager.get_sites.call_count == 1
        # config 已解除已删站点绑定
        assert config.get_cert(12345)['site_name'] == ['live.com']
        # 回调 failure 带原因
        kwargs = api.callback.call_args.kwargs
        assert kwargs['status'] == 'failure'
        assert 'deleted.com' in kwargs['message']

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_self_heal_no_repeat_failure(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """自愈后再次部署不再包含已删站点，回调 success"""
        deployer, api, config = self._make(tmp_data_dir, [{'name': 'live.com', 'path': '/w'}])
        self._mock_cert_ok(mock_cert_utils)
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['live.com', 'deleted.com'])

        # 首次：含已删站点 → failure + 解除绑定
        deployer.deploy_multi(['live.com', 'deleted.com'], 'cert', 'key', order_id=12345)
        assert api.callback.call_args.kwargs['status'] == 'failure'

        # 第二次：用配置中剩余站点（已解除绑定）→ success
        remaining = config.get_cert(12345)['site_name']
        deployer.deploy_multi(remaining, 'cert', 'key', order_id=12345)
        assert api.callback.call_args.kwargs['status'] == 'success'

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_all_bound_sites_deleted_but_panel_has_sites(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """绑定的站点全部已删（面板仍有其他站点）：全部解除绑定，回调 failure"""
        deployer, api, config = self._make(tmp_data_dir, [{'name': 'other.com', 'path': '/w'}])
        self._mock_cert_ok(mock_cert_utils)
        config.add_cert(12345, 'test', ['a.com'], site_names=['x.com', 'y.com'])

        results = deployer.deploy_multi(['x.com', 'y.com'], 'cert', 'key', order_id=12345)

        mock_set_ssl.assert_not_called()
        assert all(r['status'] is False for r in results)
        assert config.get_cert(12345)['site_name'] == []
        assert api.callback.call_args.kwargs['status'] == 'failure'

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_no_site_manager_no_detection(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """未注入 site_manager 时不做删除检测（保持原行为）"""
        config = ConfigManager(tmp_data_dir)
        api = MagicMock()
        api.callback.return_value = {'code': 1}
        deployer = Deployer(config, api, MagicMock())  # 无 site_manager
        self._mock_cert_ok(mock_cert_utils)
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1.com'])

        results = deployer.deploy_multi(['s1.com'], 'cert', 'key', order_id=12345)
        assert results[0]['status'] is True
        mock_set_ssl.assert_called_once()


class TestSiteQueryFailureNoUnbind:
    """站点清单查询失败绝不解绑（P0 复现场景：DB 缺失/锁定/表结构漂移都曾被判为「零站点」）"""

    @staticmethod
    def _make_with_real_mgr(tmp_data_dir):
        from lib.site_manager import SiteManager
        config = ConfigManager(tmp_data_dir)
        api = MagicMock()
        api.callback.return_value = {'code': 1}
        site_mgr = SiteManager(logger=MagicMock())  # 真实 SiteManager，不用 mock 掩盖失败模式
        deployer = Deployer(config, api, MagicMock(), site_mgr)
        return deployer, api, config

    @staticmethod
    def _mock_cert_ok(mock_cert_utils):
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = {'not_after': _FUTURE_EXPIRY, 'serial': 'X'}

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_db_missing_no_unbind(self, mock_set_ssl, mock_cert_utils, tmp_data_dir, tmp_path):
        """DB 文件缺失：不解绑、site_name 保留、保守继续部署全部站点"""
        from lib.site_manager import SiteManager
        deployer, api, config = self._make_with_real_mgr(tmp_data_dir)
        self._mock_cert_ok(mock_cert_utils)
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1.com', 's2.com'])

        with patch.object(SiteManager, '_get_db_path',
                          return_value=str(tmp_path / 'no-such-site.db')):
            results = deployer.deploy_multi(['s1.com', 's2.com'], 'cert', 'key', order_id=12345)

        # 全部站点保守视为存在并部署，绝不解绑
        assert mock_set_ssl.call_count == 2
        assert all(r['status'] is True for r in results)
        assert not any(r.get('site_removed') for r in results)
        assert config.get_cert(12345)['site_name'] == ['s1.com', 's2.com']
        assert api.callback.call_args.kwargs['status'] == 'success'

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_db_schema_drift_no_unbind(self, mock_set_ssl, mock_cert_utils, tmp_data_dir, tmp_path):
        """表结构漂移（sqlite3.OperationalError: no such table）：同样不解绑"""
        import sqlite3
        from lib.site_manager import SiteManager
        db_path = str(tmp_path / 'drifted.db')
        conn = sqlite3.connect(db_path)
        conn.execute('CREATE TABLE unrelated (id INTEGER)')  # 没有 sites/domain 表
        conn.commit()
        conn.close()

        deployer, api, config = self._make_with_real_mgr(tmp_data_dir)
        self._mock_cert_ok(mock_cert_utils)
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1.com'])

        with patch.object(SiteManager, '_get_db_path', return_value=db_path):
            results = deployer.deploy_multi(['s1.com'], 'cert', 'key', order_id=12345)

        mock_set_ssl.assert_called_once()
        assert results[0]['status'] is True
        assert config.get_cert(12345)['site_name'] == ['s1.com']

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_db_locked_no_unbind(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """DB 锁定（database is locked）：mock get_sites 抛查询失败语义，不解绑"""
        from lib.site_manager import SiteQueryError
        config = ConfigManager(tmp_data_dir)
        api = MagicMock()
        api.callback.return_value = {'code': 1}
        site_mgr = MagicMock()
        site_mgr.get_sites.side_effect = SiteQueryError('获取站点列表失败: database is locked')
        deployer = Deployer(config, api, MagicMock(), site_mgr)
        self._mock_cert_ok(mock_cert_utils)
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1.com', 's2.com'])

        results = deployer.deploy_multi(['s1.com', 's2.com'], 'cert', 'key', order_id=12345)

        assert mock_set_ssl.call_count == 2
        assert all(r['status'] is True for r in results)
        assert config.get_cert(12345)['site_name'] == ['s1.com', 's2.com']

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_empty_site_list_no_unbind(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """清单查询成功但为空（面板零站点/迁移中间态）：单次探测不清空绑定"""
        config = ConfigManager(tmp_data_dir)
        api = MagicMock()
        api.callback.return_value = {'code': 1}
        site_mgr = MagicMock()
        site_mgr.get_sites.return_value = []
        deployer = Deployer(config, api, MagicMock(), site_mgr)
        self._mock_cert_ok(mock_cert_utils)
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1.com'])

        results = deployer.deploy_multi(['s1.com'], 'cert', 'key', order_id=12345)

        # 跳过删除判定：保守部署、不解绑
        mock_set_ssl.assert_called_once()
        assert results[0]['status'] is True
        assert config.get_cert(12345)['site_name'] == ['s1.com']


class TestDeployError:
    def test_error_attributes(self):
        err = DeployError('test error', phase='validate', retryable=True)
        assert str(err) == 'test error'
        assert err.phase == 'validate'
        assert err.retryable is True
