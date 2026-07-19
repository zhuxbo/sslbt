"""部署器测试"""

import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from lib.deployer import Deployer, DeployError
from lib.config import ConfigManager

# 有效的未来到期时间，供 parse_cert_info mock 使用（部署成功必须能记录到期时间）
_FUTURE_EXPIRY = datetime(2035, 1, 1, tzinfo=timezone.utc)


def _backdate_missing(config, order_id, hours=13):
    """回拨站点缺失跟踪的上次计入时间戳，模拟距上次缺失已超过最小确认间隔"""
    cert = config.get_cert(order_id)
    counts = cert['metadata'].get('site_missing_counts', {})
    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
    for sn in counts:
        if isinstance(counts[sn], dict):
            counts[sn]['last_at'] = old
    config.update_metadata(order_id, {'site_missing_counts': counts})


class TestDeployer:
    @pytest.fixture
    def deployer(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        api = MagicMock()
        api.callback.return_value = {'code': 1}
        logger = MagicMock()
        return Deployer(config, api, logger)

    def test_deploy_multi_invalid_cert(self, deployer):
        with pytest.raises(DeployError, match='证书验证失败'):
            deployer.deploy_multi(['test-site'], 'bad cert', 'bad key')

    def test_deploy_multi_invalid_key(self, deployer):
        cert = "-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----"
        with pytest.raises(DeployError, match='私钥验证失败'):
            deployer.deploy_multi(['test-site'], cert, 'bad key')

    @patch('lib.deployer.cert_utils')
    def test_deploy_multi_key_mismatch(self, mock_cert_utils, deployer):
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = False

        with pytest.raises(DeployError, match='不匹配'):
            deployer.deploy_multi(['test-site'], 'cert', 'key')

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

    def test_capture_falls_back_to_cert_files(self, deployer, tmp_path, monkeypatch):
        """GetSSL 拿不到有效证书时回退读宝塔证书目录文件"""
        import lib.deployer as deployer_mod
        cert_dir = tmp_path / 'vhost-cert' / 'site.example.com'
        cert_dir.mkdir(parents=True)
        (cert_dir / 'fullchain.pem').write_text('FILE-CERT')
        (cert_dir / 'privkey.pem').write_text('FILE-KEY')
        monkeypatch.setattr(deployer_mod, '_BT_CERT_DIRS',
                            (str(tmp_path / 'vhost-cert' / '%s'),))

        site_obj = MagicMock()
        site_obj.GetSSL.return_value = {'status': False}  # 无有效证书
        prev = deployer._capture_current_ssl(site_obj, 'site.example.com')
        assert prev == {'key': 'FILE-KEY', 'cert': 'FILE-CERT'}

    def test_capture_prefers_getssl(self, deployer):
        """GetSSL 返回有效证书时优先使用，不读文件"""
        site_obj = MagicMock()
        site_obj.GetSSL.return_value = {'status': True, 'key': 'K', 'csr': 'C'}
        prev = deployer._capture_current_ssl(site_obj, 'any.example.com')
        assert prev == {'key': 'K', 'cert': 'C'}

    def test_capture_none_when_no_source(self, deployer):
        """GetSSL 与文件回退均无结果时返回 None"""
        site_obj = MagicMock()
        site_obj.GetSSL.side_effect = Exception('boom')
        prev = deployer._capture_current_ssl(site_obj, 'nonexist-site-xyz.example.com')
        assert prev is None

    def test_read_cert_files_rejects_traversal_site_name(self, deployer, tmp_path, monkeypatch):
        """恶意 site_name（穿越/绝对路径/分隔符）拼路径前即被拒，绝不读到目录外证书"""
        import lib.deployer as deployer_mod
        outside = tmp_path / 'secret'
        outside.mkdir(parents=True)
        (outside / 'fullchain.pem').write_text('LEAK-CERT')
        (outside / 'privkey.pem').write_text('LEAK-KEY')
        monkeypatch.setattr(deployer_mod, '_BT_CERT_DIRS',
                            (str(tmp_path / 'base' / '%s'),))
        for bad in ('../secret', '/etc/passwd', 'a/b', '..'):
            assert deployer._read_site_cert_files(bad) is None

    def test_read_cert_files_rejects_symlink_target(self, deployer, tmp_path, monkeypatch):
        """证书文件为符号链接时拒绝读取，避免经预置软链读到目录外文件"""
        import os
        import lib.deployer as deployer_mod
        secret = tmp_path / 'secret.pem'
        secret.write_text('LEAK')
        cert_dir = tmp_path / 'base' / 'evil.example.com'
        cert_dir.mkdir(parents=True)
        os.symlink(str(secret), str(cert_dir / 'fullchain.pem'))
        (cert_dir / 'privkey.pem').write_text('KEY')
        monkeypatch.setattr(deployer_mod, '_BT_CERT_DIRS',
                            (str(tmp_path / 'base' / '%s'),))
        assert deployer._read_site_cert_files('evil.example.com') is None

    def test_read_cert_files_normal_site_ok(self, deployer, tmp_path, monkeypatch):
        """正常域名不受加固影响，仍能读取宝塔证书目录文件"""
        import lib.deployer as deployer_mod
        cert_dir = tmp_path / 'base' / 'good.example.com'
        cert_dir.mkdir(parents=True)
        (cert_dir / 'fullchain.pem').write_text('CERT')
        (cert_dir / 'privkey.pem').write_text('KEY')
        monkeypatch.setattr(deployer_mod, '_BT_CERT_DIRS',
                            (str(tmp_path / 'base' / '%s'),))
        assert deployer._read_site_cert_files('good.example.com') == {'key': 'KEY', 'cert': 'CERT'}

    def test_setssl_writes_then_returns_false_rolls_back(self, deployer, monkeypatch):
        """SetSSL 写入后返回非成功状态 → 回滚到原证书（而非直接失败留下坏证书）"""
        import panelSite
        panelSite.panelSite._ssl_data['test.example.com'] = {'key': 'OLD-KEY', 'cert': 'OLD-CERT'}

        calls = {'n': 0}

        def write_then_false(self, params):
            calls['n'] += 1
            site = params['siteName']
            # 模拟宝塔 SetSSL 已写入证书但返回失败状态；回滚调用（第 2 次）返回成功
            panelSite.panelSite._ssl_data[site] = {'key': params['key'], 'cert': params['csr']}
            return {'status': False, 'msg': 'boom'} if calls['n'] == 1 else {'status': True}

        monkeypatch.setattr(panelSite.panelSite, 'SetSSL', write_then_false)
        with pytest.raises(DeployError, match='boom'):
            deployer._set_ssl('test.example.com', 'NEW-CERT', 'NEW-KEY')
        # 已回滚到原证书，站点不残留失败写入的新证书
        assert panelSite.panelSite._ssl_data['test.example.com'] == {'key': 'OLD-KEY', 'cert': 'OLD-CERT'}

    def test_setssl_raises_rolls_back(self, deployer, monkeypatch):
        """SetSSL 抛异常（可能已部分写入）→ 尝试回滚到原证书"""
        import panelSite
        panelSite.panelSite._ssl_data['test.example.com'] = {'key': 'OLD-KEY', 'cert': 'OLD-CERT'}

        calls = {'n': 0}

        def raise_after_write(self, params):
            calls['n'] += 1
            site = params['siteName']
            panelSite.panelSite._ssl_data[site] = {'key': params['key'], 'cert': params['csr']}
            if calls['n'] == 1:
                raise RuntimeError('SetSSL 内部异常')
            return {'status': True}  # 回滚调用返回成功

        monkeypatch.setattr(panelSite.panelSite, 'SetSSL', raise_after_write)
        with pytest.raises(DeployError, match='SetSSL 写入异常'):
            deployer._set_ssl('test.example.com', 'NEW-CERT', 'NEW-KEY')
        assert panelSite.panelSite._ssl_data['test.example.com'] == {'key': 'OLD-KEY', 'cert': 'OLD-CERT'}

    def test_setssl_false_no_prev_preserves_original_error(self, deployer, monkeypatch):
        """SetSSL 返回 False 且站点无原证书 → 回滚 no-op，保留原始错误信息"""
        import panelSite

        def just_false(self, params):
            return {'status': False, 'msg': '指定站点不存在'}

        monkeypatch.setattr(panelSite.panelSite, 'SetSSL', just_false)
        with pytest.raises(DeployError, match='指定站点不存在'):
            deployer._set_ssl('nonexist-xyz.example.com', 'NEW-CERT', 'NEW-KEY')


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
        """部分站点连续两轮缺失：存活站点部署，缺失站点二次确认后才解除绑定，回调 failure 带原因

        行为变更（缩小破坏半径）：首轮仅疑似不解绑，连续第二轮才解绑。
        """
        deployer, api, config = self._make(tmp_data_dir, [{'name': 'live.com', 'path': '/w'}])
        self._mock_cert_ok(mock_cert_utils)
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['live.com', 'deleted.com'])

        # 第一轮：缺失站点仅疑似，不解绑，但按失败上报
        r1 = deployer.deploy_multi(['live.com', 'deleted.com'], 'cert', 'key', order_id=12345)
        del_r1 = next(r for r in r1 if r['site_name'] == 'deleted.com')
        assert del_r1['status'] is False
        assert del_r1.get('site_missing') is True
        assert not del_r1.get('site_removed')
        assert config.get_cert(12345)['site_name'] == ['live.com', 'deleted.com']
        assert api.callback.call_args.kwargs['status'] == 'failure'

        # 跨最小间隔后第二轮：连续缺失确认，解除绑定
        _backdate_missing(config, 12345)
        results = deployer.deploy_multi(['live.com', 'deleted.com'], 'cert', 'key', order_id=12345)
        live_r = next(r for r in results if r['site_name'] == 'live.com')
        del_r = next(r for r in results if r['site_name'] == 'deleted.com')
        assert live_r['status'] is True
        assert del_r['status'] is False
        assert '解除绑定' in del_r['message']
        assert del_r.get('site_removed') is True
        # 只部署存活站点（两轮各一次），缺失站点从不写入
        assert mock_set_ssl.call_count == 2
        assert all(c.args[0] == 'live.com' for c in mock_set_ssl.call_args_list)
        # config 已解除已删站点绑定
        assert config.get_cert(12345)['site_name'] == ['live.com']
        # 回调 failure 带原因
        kwargs = api.callback.call_args.kwargs
        assert kwargs['status'] == 'failure'
        assert 'deleted.com' in kwargs['message']

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_self_heal_no_repeat_failure(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """连续两轮确认解绑后，再次部署不再包含已删站点，回调 success"""
        deployer, api, config = self._make(tmp_data_dir, [{'name': 'live.com', 'path': '/w'}])
        self._mock_cert_ok(mock_cert_utils)
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['live.com', 'deleted.com'])

        # 跨最小间隔的连续两轮含已删站点：首轮疑似、第二轮确认解绑，两轮均 failure
        deployer.deploy_multi(['live.com', 'deleted.com'], 'cert', 'key', order_id=12345)
        assert api.callback.call_args.kwargs['status'] == 'failure'
        _backdate_missing(config, 12345)
        deployer.deploy_multi(['live.com', 'deleted.com'], 'cert', 'key', order_id=12345)
        assert api.callback.call_args.kwargs['status'] == 'failure'

        # 第三次：用配置中剩余站点（已解除绑定）→ success
        remaining = config.get_cert(12345)['site_name']
        assert remaining == ['live.com']
        deployer.deploy_multi(remaining, 'cert', 'key', order_id=12345)
        assert api.callback.call_args.kwargs['status'] == 'success'

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_confirmed_deleted_site_prune_failure_is_reported(
            self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """解绑落盘失败时保持原绑定，并明确返回持久化失败"""
        deployer, api, config = self._make(tmp_data_dir, [{'name': 'live.com', 'path': '/w'}])
        self._mock_cert_ok(mock_cert_utils)
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['live.com', 'deleted.com'])

        deployer.deploy_multi(['live.com', 'deleted.com'], 'cert', 'key', order_id=12345)
        _backdate_missing(config, 12345)
        real_update_cert = config.update_cert

        def fail_site_name_update(order_id, updates):
            if 'site_name' in updates:
                raise OSError('磁盘写入失败')
            return real_update_cert(order_id, updates)

        with patch.object(config, 'update_cert', side_effect=fail_site_name_update):
            results = deployer.deploy_multi(
                ['live.com', 'deleted.com'], 'cert', 'key', order_id=12345)

        deleted = next(r for r in results if r['site_name'] == 'deleted.com')
        assert deleted.get('site_remove_failed') is True
        assert not deleted.get('site_removed')
        assert '持久化失败' in deleted['message']
        assert config.get_cert(12345)['site_name'] == ['live.com', 'deleted.com']
        callback = api.callback.call_args.kwargs
        assert callback['status'] == 'failure'
        assert '持久化失败' in callback['message']
        assert '已解除绑定' not in callback['message']

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_all_bound_sites_deleted_but_panel_has_sites(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """绑定站点全部缺失（面板仍有其他站点）：连续两轮确认后全部解除绑定，回调 failure"""
        deployer, api, config = self._make(tmp_data_dir, [{'name': 'other.com', 'path': '/w'}])
        self._mock_cert_ok(mock_cert_utils)
        config.add_cert(12345, 'test', ['a.com'], site_names=['x.com', 'y.com'])

        # 第一轮：全部疑似，不解绑
        r1 = deployer.deploy_multi(['x.com', 'y.com'], 'cert', 'key', order_id=12345)
        assert all(r['status'] is False for r in r1)
        assert config.get_cert(12345)['site_name'] == ['x.com', 'y.com']
        assert api.callback.call_args.kwargs['status'] == 'failure'

        # 跨最小间隔后第二轮：连续缺失确认，全部解绑
        _backdate_missing(config, 12345)
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


class TestSiteMissingTwoRoundConfirm:
    """站点缺失连续两轮确认才解绑（缩小误清绑定破坏半径）"""

    @staticmethod
    def _make(tmp_data_dir, get_sites_returns):
        """get_sites_returns: 逐轮 get_sites() 返回值序列（模拟每轮面板快照）"""
        config = ConfigManager(tmp_data_dir)
        api = MagicMock()
        api.callback.return_value = {'code': 1}
        site_mgr = MagicMock()
        site_mgr.get_sites.side_effect = list(get_sites_returns)
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
    def test_single_miss_not_unbound_counts_one(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """单次缺失 → 不解绑，连续缺失计数=1，按失败回报"""
        panel = [{'name': 'live.com', 'path': '/w'}]  # gone.com 缺失
        deployer, api, config = self._make(tmp_data_dir, [panel])
        self._mock_cert_ok(mock_cert_utils)
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['live.com', 'gone.com'])

        results = deployer.deploy_multi(['live.com', 'gone.com'], 'cert', 'key', order_id=12345)

        gone_r = next(r for r in results if r['site_name'] == 'gone.com')
        assert gone_r['status'] is False
        assert gone_r.get('site_missing') is True
        assert not gone_r.get('site_removed')
        # 未解绑
        assert config.get_cert(12345)['site_name'] == ['live.com', 'gone.com']
        # 连续缺失计数=1，随计数持久化上次计入时间戳
        track = config.get_cert(12345)['metadata']['site_missing_counts']
        assert track['gone.com']['count'] == 1
        assert track['gone.com']['last_at']
        # 按失败回报
        assert api.callback.call_args.kwargs['status'] == 'failure'

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_two_consecutive_misses_unbind(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """跨最小间隔的连续两轮缺失 → 第二轮确认解绑"""
        panel = [{'name': 'live.com', 'path': '/w'}]
        deployer, api, config = self._make(tmp_data_dir, [panel, panel])
        self._mock_cert_ok(mock_cert_utils)
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['live.com', 'gone.com'])

        deployer.deploy_multi(['live.com', 'gone.com'], 'cert', 'key', order_id=12345)
        assert config.get_cert(12345)['site_name'] == ['live.com', 'gone.com']  # 首轮不解绑

        _backdate_missing(config, 12345)
        results = deployer.deploy_multi(['live.com', 'gone.com'], 'cert', 'key', order_id=12345)
        gone_r = next(r for r in results if r['site_name'] == 'gone.com')
        assert gone_r.get('site_removed') is True
        assert config.get_cert(12345)['site_name'] == ['live.com']  # 第二轮解绑
        assert config.get_cert(12345)['metadata']['site_missing_counts']['gone.com']['count'] == 2

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_miss_then_recover_resets_count(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """缺失一轮后站点恢复 → 计数清零，不解绑"""
        panel_missing = [{'name': 'live.com', 'path': '/w'}]              # gone.com 缺失
        panel_full = [{'name': 'live.com', 'path': '/w'},
                      {'name': 'gone.com', 'path': '/w'}]                 # gone.com 恢复
        deployer, api, config = self._make(tmp_data_dir, [panel_missing, panel_full])
        self._mock_cert_ok(mock_cert_utils)
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['live.com', 'gone.com'])

        # 第一轮：gone.com 缺失，计数=1
        deployer.deploy_multi(['live.com', 'gone.com'], 'cert', 'key', order_id=12345)
        assert config.get_cert(12345)['metadata']['site_missing_counts']['gone.com']['count'] == 1

        # 第二轮：gone.com 恢复 → 计数清零、正常部署、不解绑
        results = deployer.deploy_multi(['live.com', 'gone.com'], 'cert', 'key', order_id=12345)
        assert all(r['status'] is True for r in results)
        assert config.get_cert(12345)['metadata'].get('site_missing_counts', {}) == {}
        assert config.get_cert(12345)['site_name'] == ['live.com', 'gone.com']
        assert api.callback.call_args.kwargs['status'] == 'success'

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_second_miss_within_12h_no_increment(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """同日（<12h）再次缺失 → 计数仍为 1 不解绑（两轮确认按时间跨度而非探测次数）"""
        panel = [{'name': 'live.com', 'path': '/w'}]
        deployer, api, config = self._make(tmp_data_dir, [panel, panel])
        self._mock_cert_ok(mock_cert_utils)
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['live.com', 'gone.com'])

        deployer.deploy_multi(['live.com', 'gone.com'], 'cert', 'key', order_id=12345)
        results = deployer.deploy_multi(['live.com', 'gone.com'], 'cert', 'key', order_id=12345)

        gone_r = next(r for r in results if r['site_name'] == 'gone.com')
        assert gone_r.get('site_missing') is True  # 仍为疑似态
        assert not gone_r.get('site_removed')
        assert config.get_cert(12345)['site_name'] == ['live.com', 'gone.com']  # 不解绑
        assert config.get_cert(12345)['metadata']['site_missing_counts']['gone.com']['count'] == 1
        assert api.callback.call_args.kwargs['status'] == 'failure'  # 仍按失败上报

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_naive_last_at_repaired_no_crash(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """可解析但无时区的 last_at（人工篡改）→ 不抛异常按损坏修复，健康站点照常部署"""
        panel = [{'name': 'live.com', 'path': '/w'}]
        deployer, api, config = self._make(tmp_data_dir, [panel])
        self._mock_cert_ok(mock_cert_utils)
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['live.com', 'gone.com'])
        # 预置 naive 时间戳（无 Z/偏移，fromisoformat 可解析成 naive datetime）
        config.update_metadata(12345, {'site_missing_counts': {
            'gone.com': {'count': 1, 'last_at': '2026-07-16T10:00:00'}}})

        results = deployer.deploy_multi(['live.com', 'gone.com'], 'cert', 'key', order_id=12345)

        # 健康站点照常部署（整轮不因 TypeError 中止）
        live_r = next(r for r in results if r['site_name'] == 'live.com')
        assert live_r['status'] is True
        mock_set_ssl.assert_called_once_with('live.com', 'cert', 'key')
        # 走损坏修复分支：计数不递增、保持疑似不解绑
        gone_r = next(r for r in results if r['site_name'] == 'gone.com')
        assert gone_r.get('site_missing') is True
        assert not gone_r.get('site_removed')
        track = config.get_cert(12345)['metadata']['site_missing_counts']['gone.com']
        assert track['count'] == 1
        # 时间戳已修复为带时区的当前时间
        assert track['last_at'] != '2026-07-16T10:00:00'
        repaired = datetime.fromisoformat(track['last_at'].replace('Z', '+00:00'))
        assert repaired.tzinfo is not None

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_second_miss_after_12h_unbinds(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """跨 12 小时的两轮缺失 → 确认删除并解绑"""
        panel = [{'name': 'live.com', 'path': '/w'}]
        deployer, api, config = self._make(tmp_data_dir, [panel, panel])
        self._mock_cert_ok(mock_cert_utils)
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['live.com', 'gone.com'])

        deployer.deploy_multi(['live.com', 'gone.com'], 'cert', 'key', order_id=12345)
        _backdate_missing(config, 12345)
        results = deployer.deploy_multi(['live.com', 'gone.com'], 'cert', 'key', order_id=12345)

        gone_r = next(r for r in results if r['site_name'] == 'gone.com')
        assert gone_r.get('site_removed') is True
        assert config.get_cert(12345)['site_name'] == ['live.com']


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


class TestSendCallbackFlag:
    """send_callback：手动路径默认发回调（语义不变）；自动续签编排层传 False 抑制底层回调"""

    @staticmethod
    def _make(tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        api = MagicMock()
        api.callback.return_value = {'code': 1}
        return Deployer(config, api, MagicMock()), api, config

    @staticmethod
    def _mock_ok(mock_cert_utils):
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = {'not_after': _FUTURE_EXPIRY, 'serial': 'X'}

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_suppressed_on_success_but_clears_counts(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """send_callback=False：不发回调，但成功仍清零签发与部署计数（deploy-spec §3.8）"""
        deployer, api, config = self._make(tmp_data_dir)
        self._mock_ok(mock_cert_utils)
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1'])
        config.update_metadata(12345, {'issue_retry_count': 4, 'deploy_attempt_count': 3,
                                       'deploy_started': True})
        results = deployer.deploy_multi(['s1'], 'cert', 'key', order_id=12345, send_callback=False)
        assert results[0]['status'] is True
        api.callback.assert_not_called()
        meta = config.get_cert(12345)['metadata']
        assert meta['deploy_attempt_count'] == 0
        assert meta['deploy_started'] is False
        assert meta['issue_retry_count'] == 0

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_suppressed_on_failure(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """send_callback=False：部署失败也不发底层回调（由编排层发）"""
        deployer, api, config = self._make(tmp_data_dir)
        self._mock_ok(mock_cert_utils)
        mock_set_ssl.side_effect = Exception('boom')
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1'])
        results = deployer.deploy_multi(['s1'], 'cert', 'key', order_id=12345, send_callback=False)
        assert results[0]['status'] is False
        api.callback.assert_not_called()

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_default_still_sends_callback(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """默认 send_callback=True（手动 deploy/setup 路径）语义不变，底层仍发回调"""
        deployer, api, config = self._make(tmp_data_dir)
        self._mock_ok(mock_cert_utils)
        mock_set_ssl.return_value = {'status': True}
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1'])
        deployer.deploy_multi(['s1'], 'cert', 'key', order_id=12345)
        api.callback.assert_called_once()
        assert api.callback.call_args.kwargs['status'] == 'success'


class TestDeployError:
    def test_error_attributes(self):
        err = DeployError('test error', phase='validate', retryable=True)
        assert str(err) == 'test error'
        assert err.phase == 'validate'
        assert err.retryable is True
