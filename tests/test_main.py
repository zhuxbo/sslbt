"""插件入口测试"""

import os
import json
import time
import fcntl
import subprocess
import sys
import textwrap
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.config import ConfigManager
from lib.logger import Logger
from sslbt_main import sslbt_main


# 使用独立的 fixture 构造 sslbt_main 实例，避免依赖真实路径
@pytest.fixture
def plugin(tmp_data_dir):
    """构造测试用插件实例"""
    from sslbt_main import sslbt_main

    inst = sslbt_main.__new__(sslbt_main)
    inst._data_dir = tmp_data_dir
    inst._config = ConfigManager(tmp_data_dir)
    inst._logger = Logger(os.path.join(tmp_data_dir, 'logs'))
    inst._site_mgr = MagicMock()
    return inst


TOKEN = 'a' * 32 + '.test-token-abcdefghij1234'


def test_reload_with_panel_module_name_refreshes_cached_lib_config():
    """宝塔用非 sslbt_main 模块名 reload 时，也必须清掉升级前的 lib 缓存。"""
    root = Path(__file__).resolve().parents[1]
    script = textwrap.dedent("""
        import importlib.util
        import os
        import sys
        import types

        root = sys.argv[1]
        sys.path.insert(0, os.path.join(root, 'src'))
        sys.path.insert(0, os.path.join(root, 'tests'))
        sys.modules['panelSite'] = __import__('mock_bt.panelSite', fromlist=['panelSite'])
        sys.modules['public'] = __import__('mock_bt.public', fromlist=['public'])

        from lib.config import ConfigManager
        stale_config = types.ModuleType('lib.config')
        stale_config.__file__ = os.path.join(root, 'src', 'lib', 'config.py')
        stale_config.ConfigManager = ConfigManager
        sys.modules['lib.config'] = stale_config

        entry = os.path.join(root, 'src', 'sslbt_main.py')
        spec = importlib.util.spec_from_file_location('panel_plugin_sslbt', entry)
        module = importlib.util.module_from_spec(spec)
        module.sslbt_main = object
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        assert hasattr(module, 'sslbt_main')
        assert hasattr(sys.modules['lib.config'], 'derive_or_validate_renew_policy')
    """)

    subprocess.run([sys.executable, '-c', script, str(root)], check=True)


class TestGetConfig:
    def test_get_config_no_api_fields(self, plugin):
        """全局配置不包含 api_url/api_token/check_interval_hours"""
        result = plugin.get_config()
        assert result['status'] is True
        data = result['data']
        assert 'api_url' not in data
        assert 'api_token' not in data
        assert 'api_token_masked' not in data
        assert 'check_interval_hours' not in data
        assert 'schedule' in data
        assert data['upgrade_channel'] == 'main'


class TestSaveConfig:
    def test_save_config_ignores_renew_days(self, plugin):
        """save_config 不再处理 renew_before_days（由 API 下发）"""
        plugin.save_config({'renew_before_days': '10'})
        cfg = plugin._config.get_config()
        assert cfg['schedule']['renew_before_days'] == 14  # 保持默认值

    def test_save_config_no_api_fields(self, plugin):
        """save_config 不处理 api_url/api_token"""
        plugin.save_config({
            'api_url': 'https://evil.com',
            'api_token': 'some-token',
        })
        cfg = plugin._config.get_config()
        assert 'api_url' not in cfg
        assert 'api_token' not in cfg
        assert 'check_interval_hours' not in cfg

    def test_save_renew_mode(self, plugin):
        plugin.save_config({'renew_mode': 'local'})
        cfg = plugin._config.get_config()
        assert cfg['schedule']['renew_mode'] == 'local'

    def test_save_upgrade_channel(self, plugin):
        plugin.save_config({'upgrade_channel': 'dev'})
        cfg = plugin._config.get_config()
        assert cfg['upgrade_channel'] == 'dev'


class TestAddCert:
    def test_requires_api(self, plugin):
        """无 api_url/api_token 时返回错误"""
        result = plugin.add_cert({'order_id': '100'})
        assert result['status'] is False
        assert 'API' in result['msg']

    def test_requires_order_id(self, plugin):
        result = plugin.add_cert({})
        assert result['status'] is False
        assert '订单' in result['msg']

    @patch('sslbt_main.APIClient')
    def test_add_cert_success(self, mock_api_cls, plugin):
        mock_api = MagicMock()
        mock_api.query_order.return_value = {
            'status': 'active',
            'domains': 'example.com,www.example.com',
        }
        mock_api_cls.return_value = mock_api

        result = plugin.add_cert({
            'order_id': '100',
            'api_url': 'https://api.example.com',
            'api_token': TOKEN,
            'site_names': 'example.com',
        })
        assert result['status'] is True
        cert = plugin._config.get_cert(100)
        assert cert is not None
        assert cert['api']['url'] == 'https://api.example.com'
        assert cert['api']['token'] == TOKEN


class TestParseCertDomains:
    def test_fallback_to_api_domains(self, plugin):
        """无证书 PEM 时回退到 API 域名"""
        from sslbt_main import sslbt_main
        result = sslbt_main._parse_cert_domains({'domains': 'a.com,b.com'})
        assert result == ['a.com', 'b.com']

    def test_empty_cert_uses_api(self, plugin):
        """certificate 为空时使用 API 域名"""
        from sslbt_main import sslbt_main
        result = sslbt_main._parse_cert_domains({'certificate': '', 'domains': 'a.com'})
        assert result == ['a.com']

    @patch('sslbt_main.parse_cert_info')
    def test_cert_pem_overrides_api(self, mock_parse, plugin):
        """有证书 PEM 时完全以证书域名为准"""
        from sslbt_main import sslbt_main
        mock_parse.return_value = {'domains': ['a.com', 'www.a.com', '*.a.com']}
        result = sslbt_main._parse_cert_domains({
            'certificate': '---CERT---',
            'domains': 'a.com',  # API 只返回 a.com
        })
        # 应从证书提取，包含 www 和通配符
        assert result == ['a.com', 'www.a.com', '*.a.com']

    @patch('sslbt_main.parse_cert_info')
    def test_cert_parse_fail_fallback(self, mock_parse, plugin):
        """证书解析失败时回退到 API 域名"""
        from sslbt_main import sslbt_main
        mock_parse.return_value = None
        result = sslbt_main._parse_cert_domains({
            'certificate': '---BAD-CERT---',
            'domains': 'a.com,b.com',
        })
        assert result == ['a.com', 'b.com']


class TestAutoCreateCron:
    @patch('sslbt_main.CronManager')
    @patch('sslbt_main.APIClient')
    def test_auto_create_cron_when_not_exists(self, mock_api_cls, mock_cron_cls, plugin):
        """添加证书时自动创建计划任务"""
        mock_api = MagicMock()
        mock_api.query_order.return_value = {'status': 'active', 'domains': 'a.com'}
        mock_api_cls.return_value = mock_api

        mock_cron = MagicMock()
        mock_cron.get_status.return_value = {'exists': False}
        mock_cron_cls.return_value = mock_cron

        plugin.add_cert({
            'order_id': '700',
            'api_url': 'https://api.example.com',
            'api_token': TOKEN,
        })
        mock_cron.setup.assert_called_once()

    @patch('sslbt_main.CronManager')
    @patch('sslbt_main.APIClient')
    def test_no_create_cron_when_exists(self, mock_api_cls, mock_cron_cls, plugin):
        """计划任务已存在时不重复创建"""
        mock_api = MagicMock()
        mock_api.query_order.return_value = {'status': 'active', 'domains': 'a.com'}
        mock_api_cls.return_value = mock_api

        mock_cron = MagicMock()
        mock_cron.get_status.return_value = {'exists': True}
        mock_cron_cls.return_value = mock_cron

        plugin.add_cert({
            'order_id': '701',
            'api_url': 'https://api.example.com',
            'api_token': TOKEN,
        })
        mock_cron.setup.assert_not_called()

    @patch('sslbt_main.CronManager')
    @patch('sslbt_main.APIClient')
    def test_cron_fail_does_not_block_add(self, mock_api_cls, mock_cron_cls, plugin):
        """cron 创建失败不影响证书添加"""
        mock_api = MagicMock()
        mock_api.query_order.return_value = {'status': 'active', 'domains': 'a.com'}
        mock_api_cls.return_value = mock_api

        mock_cron = MagicMock()
        mock_cron.get_status.side_effect = RuntimeError('db error')
        mock_cron_cls.return_value = mock_cron

        result = plugin.add_cert({
            'order_id': '702',
            'api_url': 'https://api.example.com',
            'api_token': TOKEN,
        })
        assert result['status'] is True


class TestCertList:
    def test_masks_token(self, plugin):
        """列表中 token 被脱敏"""
        plugin._config.add_cert(
            order_id=100,
            cert_name='test',
            domains=['example.com'],
            api_url='https://api.example.com',
            api_token=TOKEN,
        )
        result = plugin.get_cert_list()
        assert result['status'] is True
        certs = result['data']
        assert len(certs) == 1
        assert certs[0]['api']['token'] == ''
        assert '***' in certs[0]['api'].get('token_masked', '')


class TestRemoveCert:
    def test_remove(self, plugin):
        plugin._config.add_cert(order_id=200, cert_name='test', domains=['a.com'])
        result = plugin.remove_cert({'order_id': '200'})
        assert result['status'] is True
        assert plugin._config.get_cert(200) is None


class TestDeployCert:
    def test_no_site(self, plugin):
        """未绑定站点返回错误"""
        plugin._config.add_cert(
            order_id=300,
            cert_name='test',
            domains=['a.com'],
            api_url='https://api.example.com',
            api_token=TOKEN,
        )
        result = plugin.deploy_cert({'order_id': '300'})
        assert result['status'] is False
        assert '站点' in result['msg']

    def test_no_api(self, plugin):
        """未配置 API 返回错误"""
        plugin._config.add_cert(
            order_id=301,
            cert_name='test',
            domains=['a.com'],
            site_names=['a.com'],
        )
        result = plugin.deploy_cert({'order_id': '301'})
        assert result['status'] is False
        assert 'API' in result['msg']

    @patch('sslbt_main.APIClient')
    def test_deploy_processing_with_file(self, mock_api_cls, plugin):
        """processing + file 状态放置验证文件"""
        plugin._config.add_cert(
            order_id=302,
            cert_name='test',
            domains=['a.com'],
            site_names=['a.com'],
            api_url='https://api.example.com',
            api_token=TOKEN,
        )
        mock_api = MagicMock()
        mock_api.query_order.return_value = {
            'status': 'processing',
            'file': {'path': '.well-known/acme-challenge/token123', 'content': 'verify'},
        }
        mock_api_cls.return_value = mock_api

        plugin._site_mgr.get_site.return_value = {
            'name': 'a.com',
            'path': '/tmp/test-webroot',
        }
        result = plugin.deploy_cert({'order_id': '302'})
        assert result['status'] is True
        assert '验证文件' in result['msg']

    @patch('sslbt_main.APIClient')
    def test_deploy_processing_no_file(self, mock_api_cls, plugin):
        """processing 无 file 字段返回提示"""
        plugin._config.add_cert(
            order_id=303,
            cert_name='test',
            domains=['a.com'],
            site_names=['a.com'],
            api_url='https://api.example.com',
            api_token=TOKEN,
        )
        mock_api = MagicMock()
        mock_api.query_order.return_value = {'status': 'processing'}
        mock_api_cls.return_value = mock_api

        result = plugin.deploy_cert({'order_id': '303'})
        assert result['status'] is False
        assert '处理中' in result['msg']

    @patch('sslbt_main.APIClient')
    def test_deploy_processing_file_place_fails(self, mock_api_cls, plugin):
        """验证文件放置全部失败时返回错误"""
        plugin._config.add_cert(
            order_id=304,
            cert_name='test',
            domains=['a.com'],
            site_names=['a.com'],
            api_url='https://api.example.com',
            api_token=TOKEN,
        )
        mock_api = MagicMock()
        mock_api.query_order.return_value = {
            'status': 'processing',
            'file': {'path': '.well-known/acme-challenge/token123', 'content': 'verify'},
        }
        mock_api_cls.return_value = mock_api

        # get_site 返回 None，导致 place_file 返回空列表
        plugin._site_mgr.get_site.return_value = None
        result = plugin.deploy_cert({'order_id': '304'})
        assert result['status'] is False
        assert '失败' in result['msg']

    @patch('sslbt_main.APIClient')
    def test_deploy_missing_ca_rejected(self, mock_api_cls, plugin):
        """active 但缺少中间证书 → 拒绝部署，避免残链覆盖完整链（BT-01）"""
        plugin._config.add_cert(
            order_id=306,
            cert_name='test',
            domains=['a.com'],
            site_names=['a.com'],
            api_url='https://api.example.com',
            api_token=TOKEN,
        )
        mock_api = MagicMock()
        mock_api.query_order.return_value = {
            'status': 'active',
            'certificate': '-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----',
            'ca_certificate': '',
        }
        mock_api_cls.return_value = mock_api
        result = plugin.deploy_cert({'order_id': '306'})
        assert result['status'] is False
        assert '中间证书' in result['msg']

    @patch('sslbt_main.APIClient')
    def test_deploy_all_sites_failed_returns_failure(self, mock_api_cls, plugin):
        """所有绑定站点部署失败时顶层状态必须为失败"""
        plugin._config.add_cert(
            order_id=305, cert_name='test', domains=['a.com'], site_names=['a.com'],
            api_url='https://api.example.com', api_token=TOKEN,
        )
        mock_api = MagicMock()
        mock_api.query_order.return_value = {
            'status': 'active', 'certificate': '---CERT---',
            'ca_certificate': '---CA---', 'private_key': '---KEY---',
        }
        mock_api_cls.return_value = mock_api
        deployer_mock = MagicMock()
        deployer_mock.deploy_multi.return_value = [
            {'site_name': 'a.com', 'status': False, 'message': '部署超时'},
        ]
        with patch('sslbt_main.Deployer', return_value=deployer_mock), \
             patch.object(plugin, '_resolve_private_key', return_value='---KEY---'):
            result = plugin.deploy_cert({'order_id': '305'})

        assert result['status'] is False
        assert '0 成功，1 失败' in result['msg']
        assert result['data'][0]['message'] == '部署超时'

    def test_deploy_cert_busy_when_renew_locked(self, plugin):
        """cron 续签占用 renew.lock 时手动部署返回 busy（BT-08）"""
        plugin._config.add_cert(
            order_id=307,
            cert_name='test',
            domains=['a.com'],
            site_names=['a.com'],
            api_url='https://api.example.com',
            api_token=TOKEN,
        )
        lock_path = os.path.join(plugin._data_dir, 'renew.lock')
        lock_fd = open(lock_path, 'w')
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = plugin.deploy_cert({'order_id': '307'})
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        assert result['status'] is False
        assert '续签' in result['msg']

    def test_deploy_all_busy_when_renew_locked(self, plugin):
        """cron 续签占用锁时批量部署返回 busy（BT-08）"""
        lock_path = os.path.join(plugin._data_dir, 'renew.lock')
        lock_fd = open(lock_path, 'w')
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = plugin.deploy_all({})
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        assert result['status'] is False
        assert '续签' in result['msg']


class TestCheckCert:
    def test_no_cert(self, plugin):
        """不存在的订单"""
        result = plugin.check_cert({'order_id': '999'})
        assert result['status'] is False
        assert '不存在' in result['msg']

    def test_no_api(self, plugin):
        """证书无 API 配置"""
        plugin._config.add_cert(order_id=400, cert_name='test', domains=['a.com'])
        result = plugin.check_cert({'order_id': '400'})
        assert result['status'] is False
        assert 'API' in result['msg']


class TestUpdateCertConfig:
    def test_update_api_url(self, plugin):
        """可更新证书的 API URL"""
        plugin._config.add_cert(order_id=500, cert_name='test', domains=['a.com'],
                                api_url='https://old.example.com', api_token=TOKEN)
        result = plugin.update_cert_config({
            'order_id': '500',
            'api_url': 'https://new.example.com',
        })
        assert result['status'] is True
        cert = plugin._config.get_cert(500)
        assert cert['api']['url'] == 'https://new.example.com'
        assert cert['api']['token'] == TOKEN  # token 未被清除

    def test_update_api_token(self, plugin):
        """可更新证书的 API Token"""
        new_token = 'b' * 32 + '.new-token-xyz'
        plugin._config.add_cert(order_id=501, cert_name='test', domains=['a.com'],
                                api_url='https://api.example.com', api_token=TOKEN)
        result = plugin.update_cert_config({
            'order_id': '501',
            'api_token': new_token,
        })
        assert result['status'] is True
        cert = plugin._config.get_cert(501)
        assert cert['api']['token'] == new_token

    def test_update_api_url_invalid_scheme(self, plugin):
        """非法 URL 协议被拒绝"""
        plugin._config.add_cert(order_id=502, cert_name='test', domains=['a.com'])
        result = plugin.update_cert_config({
            'order_id': '502',
            'api_url': 'file:///etc/passwd',
        })
        assert result['status'] is False
        assert 'http' in result['msg']

    def test_update_api_token_invalid(self, plugin):
        """非法 Token 被拒绝"""
        plugin._config.add_cert(order_id=503, cert_name='test', domains=['a.com'])
        result = plugin.update_cert_config({
            'order_id': '503',
            'api_token': 'too-short',
        })
        assert result['status'] is False

    def test_update_validation_method_ok(self, plugin):
        """普通域名可设置 file 验证方式"""
        plugin._config.add_cert(order_id=504, cert_name='test', domains=['a.example.com'],
                                api_url='https://api.example.com', api_token=TOKEN)
        result = plugin.update_cert_config({
            'order_id': '504',
            'renew_mode': 'local',
            'validation_method': 'file',
        })
        assert result['status'] is True
        cert = plugin._config.get_cert(504)
        assert cert['validation_method'] == 'file'

    def test_update_validation_method_wildcard_rejects_file(self, plugin):
        """通配符域名拒绝 file 验证"""
        plugin._config.add_cert(order_id=505, cert_name='test', domains=['*.example.com'],
                                api_url='https://api.example.com', api_token=TOKEN)
        result = plugin.update_cert_config({
            'order_id': '505',
            'renew_mode': 'local',
            'validation_method': 'file',
        })
        assert result['status'] is False

    def test_update_validation_method_ip_derives_file(self, plugin):
        """IP 域名自动派生为 local/file（覆盖非法入参 delegation，spec §5.2）"""
        plugin._config.add_cert(order_id=506, cert_name='test', domains=['1.2.3.4'],
                                api_url='https://api.example.com', api_token=TOKEN)
        result = plugin.update_cert_config({
            'order_id': '506',
            'renew_mode': 'local',
            'validation_method': 'delegation',
        })
        assert result['status'] is True
        cert = plugin._config.get_cert(506)
        assert cert['renew_mode'] == 'local'
        assert cert['validation_method'] == 'file'


class TestGetSiteMatches:
    def test_missing_order_id(self, plugin):
        result = plugin.get_site_matches({})
        assert result['status'] is False

    def test_order_not_found(self, plugin):
        result = plugin.get_site_matches({'order_id': '999'})
        assert result['status'] is False

    def test_returns_all_sites_with_match_info(self, plugin):
        plugin._config.add_cert(order_id=700, cert_name='test',
                                domains=['a.example.com', 'b.example.com'],
                                api_url='https://api.example.com', api_token=TOKEN,
                                site_names=['site-a.example.com'])
        plugin._site_mgr.get_sites.return_value = [
            {'name': 'site-a.example.com', 'domains': ['a.example.com', 'b.example.com']},
            {'name': 'site-b.example.com', 'domains': ['c.example.com']},
        ]
        result = plugin.get_site_matches({'order_id': '700'})
        assert result['status'] is True
        data = result['data']
        assert len(data) == 2
        # site-a: 已绑定，全部匹配
        assert data[0]['site_name'] == 'site-a.example.com'
        assert data[0]['bound'] is True
        assert data[0]['match_type'] == 'full'
        # site-b: 未绑定，无匹配
        assert data[1]['site_name'] == 'site-b.example.com'
        assert data[1]['bound'] is False
        assert data[1]['match_type'] is None

    def test_partial_match_returns_unmatched(self, plugin):
        """部分匹配站点返回未覆盖域名清单（前端据此提示 TLS 覆盖缺口，BT-05）"""
        plugin._config.add_cert(order_id=703, cert_name='test',
                                domains=['a.example.com'],
                                api_url='https://api.example.com', api_token=TOKEN)
        plugin._site_mgr.get_sites.return_value = [
            {'name': 'site-p.example.com', 'domains': ['a.example.com', 'uncovered.example.com']},
        ]
        result = plugin.get_site_matches({'order_id': '703'})
        assert result['status'] is True
        row = result['data'][0]
        assert row['match_type'] == 'partial'
        assert row['unmatched'] == ['uncovered.example.com']

    def test_no_sites(self, plugin):
        plugin._config.add_cert(order_id=701, cert_name='test', domains=['a.example.com'],
                                api_url='https://api.example.com', api_token=TOKEN)
        plugin._site_mgr.get_sites.return_value = []
        result = plugin.get_site_matches({'order_id': '701'})
        assert result['status'] is True
        assert result['data'] == []

    def test_site_name_string_compat(self, plugin):
        """site_name 为旧格式字符串时也能正确判断 bound"""
        plugin._config.add_cert(order_id=702, cert_name='test', domains=['a.example.com'],
                                api_url='https://api.example.com', api_token=TOKEN,
                                site_names=['s.example.com'])
        # 模拟旧格式：手动改为字符串
        certs = plugin._config.get_certs()
        for c in certs:
            if c['order_id'] == 702:
                c['site_name'] = 's.example.com'
        plugin._config.save_certs(certs)
        plugin._site_mgr.get_sites.return_value = [
            {'name': 's.example.com', 'domains': ['a.example.com']},
        ]
        result = plugin.get_site_matches({'order_id': '702'})
        assert result['status'] is True
        assert result['data'][0]['bound'] is True


class TestGetRenewStatus:
    """最近续签状态读取（B5）"""

    def test_no_status_file(self, plugin):
        """无状态文件时返回 data=None"""
        result = plugin.get_renew_status()
        assert result['status'] is True
        assert result['data'] is None

    def test_reads_status_file(self, plugin):
        """读取续签状态文件内容"""
        path = os.path.join(plugin._data_dir, 'renew_status.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'last_run': '2026-07-17T10:00:00Z', 'total': 3,
                       'success': 2, 'pending': 0, 'failure': 1}, f)
        result = plugin.get_renew_status()
        assert result['status'] is True
        assert result['data']['total'] == 3
        assert result['data']['success'] == 2
        assert result['data']['failure'] == 1


class TestFetchDeployUrl:
    def test_missing_url(self, plugin):
        result = plugin.fetch_deploy_url({})
        assert result['status'] is False
        assert '链接' in result['msg']

    def test_invalid_scheme(self, plugin):
        result = plugin.fetch_deploy_url({'url': 'ftp://evil.com/api?token=abc&order=1'})
        assert result['status'] is False
        assert '协议' in result['msg']

    def test_missing_token_param(self, plugin):
        result = plugin.fetch_deploy_url({'url': 'https://api.example.com/deploy?order=1'})
        assert result['status'] is False
        assert 'token' in result['msg']

    def test_missing_order_param(self, plugin):
        result = plugin.fetch_deploy_url({'url': 'https://api.example.com/deploy?token=abc'})
        assert result['status'] is False
        assert 'order' in result['msg']

    @pytest.mark.parametrize('order', ['example.com', '100,example.com', 'abc', '1;2'])
    @patch('sslbt_main.APIClient')
    def test_domain_form_link_rejected_with_actionable_msg(self, mock_api_cls, plugin, order):
        """spec §2.3 起 order 只接受订单 ID：域名形态的旧链接给可执行提示，且不发请求"""
        result = plugin.fetch_deploy_url(
            {'url': 'https://api.example.com/api/deploy?token=' + TOKEN + '&order=' + order})
        assert result['status'] is False
        assert '订单 ID' in result['msg']
        assert '重新复制部署链接' in result['msg']
        mock_api_cls.assert_not_called()

    @patch('sslbt_main.APIClient')
    def test_success_returns_session_id(self, mock_api_cls, plugin):
        """正常流程返回 session_id 而非明文 token"""
        mock_api = MagicMock()
        mock_api.query_batch.return_value = [{'order_id': 100, 'domains': 'a.com', 'status': 'active'}]
        mock_api_cls.return_value = mock_api
        plugin._site_mgr.get_sites.return_value = []

        result = plugin.fetch_deploy_url({'url': 'https://api.example.com/api/deploy?token=' + TOKEN + '&order=100'})
        assert result['status'] is True
        data = result['data']
        assert 'session_id' in data
        assert 'token' not in data.get('api', {})  # 明文 token 不应出现
        assert data['api']['token_masked'].endswith('***')  # 仅返回脱敏值

    @patch('sslbt_main.APIClient')
    def test_partial_match_includes_unmatched(self, mock_api_cls, plugin):
        """fetch_deploy_url 为部分匹配站点返回未覆盖域名（前端一键部署列表据此提示，BT-05）"""
        mock_api = MagicMock()
        mock_api.query_batch.return_value = [{'order_id': 200, 'domains': 'a.example.com', 'status': 'active'}]
        mock_api_cls.return_value = mock_api
        plugin._site_mgr.get_sites.return_value = [
            {'name': 'site-x.example.com', 'domains': ['a.example.com', 'b.uncovered.com']},
        ]
        result = plugin.fetch_deploy_url({'url': 'https://api.example.com/api/deploy?token=' + TOKEN + '&order=200'})
        assert result['status'] is True
        matches = result['data']['certs'][0]['_matches']
        assert len(matches) == 1
        assert matches[0]['match_type'] == 'partial'
        assert matches[0]['unmatched'] == ['b.uncovered.com']

    def test_http_scheme_rejected(self, plugin):
        """http 部署链接被拒绝（统一客户端 HTTPS 强制，BT-09）"""
        result = plugin.fetch_deploy_url({
            'url': 'http://api.example.com/api/deploy?token=' + TOKEN + '&order=1'})
        assert result['status'] is False
        assert 'HTTPS' in result['msg'] or '不安全' in result['msg']

    @patch('sslbt_main.APIClient')
    def test_reverse_proxy_subpath_preserved(self, mock_api_cls, plugin):
        """反代子路径部署链接保留路径前缀（api_url 不得被改写回 host-root）"""
        mock_api = MagicMock()
        mock_api.query_batch.return_value = [{'order_id': 300, 'domains': 'a.com', 'status': 'active'}]
        mock_api_cls.return_value = mock_api
        plugin._site_mgr.get_sites.return_value = []

        result = plugin.fetch_deploy_url({
            'url': 'https://host.example.com/manager/api/deploy?token=' + TOKEN + '&order=300'})
        assert result['status'] is True
        # APIClient 用含子路径的 base_url 构造，session/config 后续沿用同一地址
        assert mock_api_cls.call_args[0][0] == 'https://host.example.com/manager/api/deploy'
        assert result['data']['api']['url'] == 'https://host.example.com/manager/api/deploy'

    @patch('sslbt_main.APIClient')
    def test_host_root_link_behavior_unchanged(self, mock_api_cls, plugin):
        """标准链接（path=/api/deploy）功能不变，api_url 含标准路径"""
        mock_api = MagicMock()
        mock_api.query_batch.return_value = [{'order_id': 301, 'domains': 'a.com', 'status': 'active'}]
        mock_api_cls.return_value = mock_api
        plugin._site_mgr.get_sites.return_value = []

        result = plugin.fetch_deploy_url({
            'url': 'https://api.example.com/api/deploy?token=' + TOKEN + '&order=301'})
        assert result['status'] is True
        assert mock_api_cls.call_args[0][0] == 'https://api.example.com/api/deploy'

    @patch('sslbt_main.APIClient')
    def test_add_cert_with_session_id(self, mock_api_cls, plugin):
        """通过 session_id 添加证书"""
        mock_api = MagicMock()
        mock_api.query_batch.return_value = [{'order_id': 100, 'domains': 'a.com', 'status': 'active'}]
        mock_api.query_order.return_value = {'status': 'active', 'domains': 'a.com'}
        mock_api_cls.return_value = mock_api
        plugin._site_mgr.get_sites.return_value = []

        fetch_result = plugin.fetch_deploy_url(
            {'url': 'https://api.example.com/api/deploy?token=' + TOKEN + '&order=100'})
        session_id = fetch_result['data']['session_id']

        # 用 session_id 添加证书（api_url 保留链接路径，_build_api_url 对含 /api/ 的路径直接追加后缀）
        result = plugin.add_cert({
            'order_id': '100',
            'session_id': session_id,
            'site_names': 'a.com',
        })
        assert result['status'] is True
        cert = plugin._config.get_cert(100)
        assert cert['api']['url'] == 'https://api.example.com/api/deploy'
        assert cert['api']['token'] == TOKEN

    @patch('sslbt_main.APIClient')
    def test_session_id_reusable_for_batch(self, mock_api_cls, plugin):
        """同一 session_id 可用于批量添加多个证书"""
        mock_api = MagicMock()
        mock_api.query_batch.return_value = [
            {'order_id': 101, 'domains': 'a.com'},
            {'order_id': 102, 'domains': 'b.com'},
        ]
        mock_api.query_order.side_effect = [
            {'status': 'active', 'domains': 'a.com'},
            {'status': 'active', 'domains': 'b.com'},
        ]
        mock_api_cls.return_value = mock_api
        plugin._site_mgr.get_sites.return_value = []

        fetch_result = plugin.fetch_deploy_url(
            {'url': 'https://api.example.com/api/deploy?token=' + TOKEN + '&order=101'})
        session_id = fetch_result['data']['session_id']

        r1 = plugin.add_cert({'order_id': '101', 'session_id': session_id, 'site_names': 'a.com'})
        r2 = plugin.add_cert({'order_id': '102', 'session_id': session_id, 'site_names': 'b.com'})
        assert r1['status'] is True
        assert r2['status'] is True

    def test_expired_session(self, plugin):
        """过期 session_id 被拒绝"""
        plugin._save_sessions({
            'old-session': {
                'api_url': 'https://api.example.com',
                'api_token': TOKEN,
                'created_at': time.time() - 700,  # 超过 10 分钟
            }
        })
        result = plugin.add_cert({'order_id': '100', 'session_id': 'old-session'})
        assert result['status'] is False
        assert '过期' in result['msg']

    def test_load_sessions_missing_file(self, plugin):
        """session 文件不存在时 _load_sessions 返回空 dict"""
        path = plugin._session_file()
        assert not os.path.exists(path)
        assert plugin._load_sessions() == {}

    def test_load_sessions_corrupted_json(self, plugin):
        """session 文件 JSON 损坏时 _load_sessions 安全降级返回空 dict"""
        path = plugin._session_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{not valid json')
        assert plugin._load_sessions() == {}

    def test_load_sessions_filters_expired_and_invalid(self, plugin):
        """_load_sessions 过滤过期项与缺 created_at 的脏数据"""
        plugin._save_sessions({
            'fresh': {
                'api_url': 'https://api.example.com',
                'api_token': TOKEN,
                'created_at': time.time(),
            },
            'expired': {
                'api_url': 'https://api.example.com',
                'api_token': TOKEN,
                'created_at': time.time() - 700,
            },
            'malformed': 'not-a-dict',
            'no_timestamp': {'api_url': 'x', 'api_token': 'y'},
        })
        loaded = plugin._load_sessions()
        assert 'fresh' in loaded
        assert 'expired' not in loaded
        assert 'malformed' not in loaded
        assert 'no_timestamp' not in loaded

    def test_save_sessions_permission_0600(self, plugin):
        """session 文件落盘后权限为 0600"""
        plugin._save_sessions({
            's1': {'api_url': 'https://api.example.com', 'api_token': TOKEN, 'created_at': time.time()},
        })
        path = plugin._session_file()
        assert os.path.exists(path)
        mode = os.stat(path).st_mode & 0o777
        assert mode == 0o600, 'session file mode should be 0600, got %o' % mode

    def test_save_sessions_no_tmp_residue(self, plugin):
        """save 完成后不应残留 .tmp 文件（os.replace 是原子的）"""
        plugin._save_sessions({
            's1': {'api_url': 'https://api.example.com', 'api_token': TOKEN, 'created_at': time.time()},
        })
        tmp_path = plugin._session_file() + '.tmp'
        assert not os.path.exists(tmp_path)

    def test_session_persists_across_instances(self, tmp_data_dir):
        """模拟宝塔 reload：同一 data_dir 下新建实例仍能读到 session（核心回归测试）"""
        from sslbt_main import sslbt_main as _SM

        # 第一个实例写入 session
        inst1 = _SM.__new__(_SM)
        inst1._data_dir = tmp_data_dir
        inst1._config = ConfigManager(tmp_data_dir)
        inst1._logger = Logger(os.path.join(tmp_data_dir, 'logs'))
        inst1._site_mgr = MagicMock()
        inst1._save_sessions({
            'sid-A': {
                'api_url': 'https://api.example.com',
                'api_token': TOKEN,
                'created_at': time.time(),
            },
        })

        # 第二个实例（模拟 reload 后新建）——类变量已重置，但磁盘还在
        inst2 = _SM.__new__(_SM)
        inst2._data_dir = tmp_data_dir
        inst2._config = ConfigManager(tmp_data_dir)
        inst2._logger = Logger(os.path.join(tmp_data_dir, 'logs'))
        inst2._site_mgr = MagicMock()

        loaded = inst2._load_sessions()
        assert 'sid-A' in loaded
        assert loaded['sid-A']['api_token'] == TOKEN

    @patch('sslbt_main.APIClient')
    def test_api_error(self, mock_api_cls, plugin):
        """API 返回错误时透传"""
        from lib.api_client import APIError
        mock_api = MagicMock()
        mock_api.query_batch.side_effect = APIError('认证失败')
        mock_api_cls.return_value = mock_api

        result = plugin.fetch_deploy_url({'url': 'https://api.example.com/api/deploy?token=' + TOKEN + '&order=1'})
        assert result['status'] is False
        assert '认证失败' in result['msg']


class TestSaveReleaseUrl:
    def test_save_release_url(self, plugin):
        """save_config 可设置 release_url"""
        result = plugin.save_config({'release_url': 'https://release.example.com/sslbt'})
        assert result['status'] is True
        cfg = plugin._config.get_config()
        assert cfg['release_url'] == 'https://release.example.com/sslbt'

    def test_save_release_url_strips(self, plugin):
        """release_url 自动去除首尾空格和末尾斜杠"""
        plugin.save_config({'release_url': '  https://release.example.com/sslbt/  '})
        cfg = plugin._config.get_config()
        assert cfg['release_url'] == 'https://release.example.com/sslbt'

    def test_save_release_url_empty(self, plugin):
        """可清空 release_url"""
        plugin.save_config({'release_url': 'https://release.example.com/sslbt'})
        plugin.save_config({'release_url': ''})
        cfg = plugin._config.get_config()
        assert cfg['release_url'] == ''

    def test_save_release_url_not_affect_other_fields(self, plugin):
        """设置 release_url 不影响其他字段"""
        plugin.save_config({'renew_mode': 'local'})
        plugin.save_config({'release_url': 'https://release.example.com/sslbt'})
        cfg = plugin._config.get_config()
        assert cfg['schedule']['renew_mode'] == 'local'
        assert cfg['release_url'] == 'https://release.example.com/sslbt'


class TestCheckCertOrderUpdate:
    @patch('sslbt_main.APIClient')
    def test_check_cert_updates_order_id(self, mock_api_cls, plugin):
        """check_cert 检测到新 order_id 时自动更新配置"""
        plugin._config.add_cert(
            order_id=800, cert_name='order-800', domains=['a.example.com'],
            api_url='https://api.example.com', api_token=TOKEN,
        )
        mock_api = MagicMock()
        mock_api.query_order.return_value = {
            'order_id': 900,
            'status': 'active',
            'domains': 'a.example.com',
        }
        mock_api_cls.return_value = mock_api

        result = plugin.check_cert({'order_id': '800'})
        assert result['status'] is True
        assert result['data']['_order_updated'] is True
        # 旧 ID 不存在，新 ID 存在
        assert plugin._config.get_cert(800) is None
        new_cert = plugin._config.get_cert(900)
        assert new_cert is not None
        assert new_cert['cert_name'] == 'order-900'

    @patch('sslbt_main.APIClient')
    def test_check_cert_same_order_id(self, mock_api_cls, plugin):
        """check_cert order_id 未变化时不更新"""
        plugin._config.add_cert(
            order_id=801, cert_name='order-801', domains=['a.example.com'],
            api_url='https://api.example.com', api_token=TOKEN,
        )
        mock_api = MagicMock()
        mock_api.query_order.return_value = {
            'order_id': 801,
            'status': 'active',
            'domains': 'a.example.com',
        }
        mock_api_cls.return_value = mock_api

        result = plugin.check_cert({'order_id': '801'})
        assert result['status'] is True
        assert '_order_updated' not in result['data']
        assert plugin._config.get_cert(801) is not None

    @patch('sslbt_main.APIClient')
    def test_check_cert_order_update_conflict(self, mock_api_cls, plugin):
        """check_cert 新 order_id 已存在时不更新，不报错"""
        plugin._config.add_cert(
            order_id=802, cert_name='order-802', domains=['a.example.com'],
            api_url='https://api.example.com', api_token=TOKEN,
        )
        plugin._config.add_cert(
            order_id=903, cert_name='order-903', domains=['b.example.com'],
            api_url='https://api.example.com', api_token=TOKEN,
        )
        mock_api = MagicMock()
        mock_api.query_order.return_value = {
            'order_id': 903,
            'status': 'active',
            'domains': 'a.example.com',
        }
        mock_api_cls.return_value = mock_api

        result = plugin.check_cert({'order_id': '802'})
        assert result['status'] is True
        # 冲突时不更新，旧 ID 仍存在
        assert '_order_updated' not in result['data']
        assert plugin._config.get_cert(802) is not None

    @patch('sslbt_main.APIClient')
    def test_check_cert_updates_domains(self, mock_api_cls, plugin):
        """check_cert 更新 order_id 时同步更新域名"""
        plugin._config.add_cert(
            order_id=803, cert_name='order-803', domains=['old.example.com'],
            api_url='https://api.example.com', api_token=TOKEN,
        )
        mock_api = MagicMock()
        mock_api.query_order.return_value = {
            'order_id': 904,
            'status': 'active',
            'domains': 'new.example.com,www.new.example.com',
        }
        mock_api_cls.return_value = mock_api

        result = plugin.check_cert({'order_id': '803'})
        assert result['status'] is True
        new_cert = plugin._config.get_cert(904)
        assert 'new.example.com' in new_cert['domains']
        assert 'www.new.example.com' in new_cert['domains']


class TestLogs:
    def test_get_logs(self, plugin):
        plugin._logger.info("test message")
        result = plugin.get_logs({})
        assert result['status'] is True
        assert 'test message' in result['data']['content']


class TestToggleAutoReissue:
    @patch('sslbt_main.CronManager')
    @patch('sslbt_main.APIClient')
    def test_add_cert_calls_toggle_pull_mode(self, mock_api_cls, mock_cron_cls, plugin):
        """pull 模式添加证书时调用 toggle_auto_reissue(True)"""
        mock_api = MagicMock()
        mock_api.query_order.return_value = {'status': 'active', 'domains': 'a.com'}
        mock_api_cls.return_value = mock_api
        mock_cron = MagicMock()
        mock_cron.get_status.return_value = {'exists': True}
        mock_cron_cls.return_value = mock_cron

        plugin.add_cert({
            'order_id': '900',
            'api_url': 'https://api.example.com',
            'api_token': TOKEN,
            'renew_mode': 'pull',
        })
        mock_api.toggle_auto_reissue.assert_called_once_with(900, True)

    @patch('sslbt_main.CronManager')
    @patch('sslbt_main.APIClient')
    def test_add_cert_calls_toggle_local_mode(self, mock_api_cls, mock_cron_cls, plugin):
        """local 模式添加证书时调用 toggle_auto_reissue(False)"""
        mock_api = MagicMock()
        mock_api.query_order.return_value = {'status': 'active', 'domains': 'a.com'}
        mock_api_cls.return_value = mock_api
        mock_cron = MagicMock()
        mock_cron.get_status.return_value = {'exists': True}
        mock_cron_cls.return_value = mock_cron

        plugin.add_cert({
            'order_id': '901',
            'api_url': 'https://api.example.com',
            'api_token': TOKEN,
            'renew_mode': 'local',
        })
        mock_api.toggle_auto_reissue.assert_called_once_with(901, False)

    @patch('sslbt_main.CronManager')
    @patch('sslbt_main.APIClient')
    def test_add_cert_toggle_failure_does_not_block(self, mock_api_cls, mock_cron_cls, plugin):
        """toggle_auto_reissue 抛异常时添加证书仍然成功"""
        mock_api = MagicMock()
        mock_api.query_order.return_value = {'status': 'active', 'domains': 'a.com'}
        mock_api.toggle_auto_reissue.side_effect = Exception('network error')
        mock_api_cls.return_value = mock_api
        mock_cron = MagicMock()
        mock_cron.get_status.return_value = {'exists': True}
        mock_cron_cls.return_value = mock_cron

        result = plugin.add_cert({
            'order_id': '902',
            'api_url': 'https://api.example.com',
            'api_token': TOKEN,
        })
        assert result['status'] is True

    @patch('sslbt_main.APIClient')
    def test_deploy_cert_calls_toggle_on_success(self, mock_api_cls, plugin):
        """deploy_cert 部署成功后调用 toggle_auto_reissue"""
        plugin._config.add_cert(
            order_id=910, cert_name='test', domains=['a.com'],
            site_names=['a.com'], api_url='https://api.example.com',
            api_token=TOKEN, renew_mode='pull',
        )
        mock_api = MagicMock()
        mock_api.query_order.return_value = {
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
            'private_key': '---KEY---',
        }
        mock_api_cls.return_value = mock_api

        deployer_mock = MagicMock()
        deployer_mock.deploy_multi.return_value = [{'site_name': 'a.com', 'status': True, 'message': 'ok'}]
        with patch('sslbt_main.Deployer', return_value=deployer_mock), \
             patch.object(plugin, '_resolve_private_key', return_value='---KEY---'):
            plugin.deploy_cert({'order_id': '910'})

        mock_api.toggle_auto_reissue.assert_called_once_with(910, True)


class TestResolvePrivateKey:
    """_resolve_private_key 私钥回退链测试"""

    def _make_plugin_with_key_match(self, plugin, match_sources):
        """构造 plugin，mock verify_cert_key_match 使指定来源的 key 匹配"""
        # match_sources: set of key_pem values that should "match"
        def fake_verify(cert_pem, key_pem):
            return key_pem in match_sources
        plugin._verify_cert_key_match = fake_verify
        return plugin

    @patch('sslbt_main.verify_cert_key_match')
    @patch('sslbt_main.validate_key_pem', return_value=(True, None))
    def test_api_key_used_first(self, mock_validate, mock_verify, plugin):
        """优先使用 API 返回的私钥"""
        mock_verify.side_effect = lambda c, k: k == 'API-KEY'
        cert_data = {'private_key': 'API-KEY'}
        result = plugin._resolve_private_key(cert_data, {}, 'CERT', ['a.com'])
        assert result == 'API-KEY'

    @patch('sslbt_main.verify_cert_key_match')
    @patch('sslbt_main.validate_key_pem', return_value=(True, None))
    def test_user_key_as_fallback(self, mock_validate, mock_verify, plugin):
        """API 无私钥时使用用户粘贴的 PEM"""
        mock_verify.side_effect = lambda c, k: k == 'USER-KEY'
        cert_data = {}
        args = {'private_key': 'USER-KEY'}
        result = plugin._resolve_private_key(cert_data, args, 'CERT', ['a.com'])
        assert result == 'USER-KEY'

    @patch('sslbt_main.verify_cert_key_match')
    @patch('sslbt_main.validate_key_pem', return_value=(True, None))
    @patch.object(sslbt_main, '_read_site_key', return_value='SITE-KEY')
    def test_site_key_fallback(self, mock_site_key, mock_validate, mock_verify, plugin):
        """API 无私钥时回退到站点已有私钥"""
        mock_verify.side_effect = lambda c, k: k == 'SITE-KEY'
        cert_data = {}
        result = plugin._resolve_private_key(cert_data, {}, 'CERT', ['a.com'])
        assert result == 'SITE-KEY'

    @patch('sslbt_main.verify_cert_key_match', return_value=False)
    @patch('sslbt_main.validate_key_pem', return_value=(True, None))
    def test_no_match_returns_empty(self, mock_validate, mock_verify, plugin):
        """所有来源的私钥均不匹配时返回空"""
        cert_data = {'private_key': 'BAD-KEY'}
        result = plugin._resolve_private_key(cert_data, {}, 'CERT', ['a.com'])
        assert result == ''

    @patch('sslbt_main.verify_cert_key_match')
    @patch('sslbt_main.validate_key_pem', return_value=(True, None))
    def test_key_path_param(self, mock_validate, mock_verify, plugin, tmp_data_dir):
        """参数提供私钥绝对路径"""
        mock_verify.side_effect = lambda c, k: k == 'FILE-KEY'
        key_file = os.path.join(tmp_data_dir, 'test.key')
        with open(key_file, 'w') as f:
            f.write('FILE-KEY')
        cert_data = {}
        args = {'private_key_path': key_file}
        result = plugin._resolve_private_key(cert_data, args, 'CERT', ['a.com'])
        assert result == 'FILE-KEY'

    def test_key_path_relative_rejected(self, plugin):
        """相对路径被拒绝"""
        result = plugin._read_key_file('relative/path.key')
        assert result == ''

    def test_key_path_symlink_rejected(self, plugin, tmp_data_dir):
        """符号链接被拒绝"""
        target = os.path.join(tmp_data_dir, 'real.key')
        link = os.path.join(tmp_data_dir, 'link.key')
        with open(target, 'w') as f:
            f.write('KEY')
        os.symlink(target, link)
        result = plugin._read_key_file(link)
        assert result == ''

    @patch('sslbt_main.APIClient')
    @patch('sslbt_main.verify_cert_key_match', return_value=False)
    @patch('sslbt_main.validate_key_pem', return_value=(True, None))
    def test_deploy_cert_returns_need_key(self, mock_validate, mock_verify, mock_api_cls, plugin):
        """deploy_cert 无匹配私钥时返回 need_key"""
        plugin._config.add_cert(
            order_id=920, cert_name='test', domains=['a.com'],
            site_names=['a.com'], api_url='https://api.example.com',
            api_token=TOKEN,
        )
        mock_api = MagicMock()
        mock_api.query_order.return_value = {
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
        }
        mock_api_cls.return_value = mock_api
        result = plugin.deploy_cert({'order_id': '920'})
        assert result['status'] is False
        assert result.get('need_key') is True

    @patch('sslbt_main.APIClient')
    @patch('sslbt_main.verify_cert_key_match')
    @patch('sslbt_main.validate_key_pem', return_value=(True, None))
    def test_deploy_cert_with_user_key_succeeds(self, mock_validate, mock_verify, mock_api_cls, plugin):
        """用户提供私钥后 deploy_cert 成功部署"""
        mock_verify.side_effect = lambda c, k: k == 'USER-KEY'
        plugin._config.add_cert(
            order_id=921, cert_name='test', domains=['a.com'],
            site_names=['a.com'], api_url='https://api.example.com',
            api_token=TOKEN,
        )
        mock_api = MagicMock()
        mock_api.query_order.return_value = {
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
        }
        mock_api_cls.return_value = mock_api
        deployer_mock = MagicMock()
        deployer_mock.deploy_multi.return_value = [{'site_name': 'a.com', 'status': True, 'message': 'ok'}]
        with patch('sslbt_main.Deployer', return_value=deployer_mock):
            result = plugin.deploy_cert({'order_id': '921', 'private_key': 'USER-KEY'})
        assert result['status'] is True
        deployer_mock.deploy_multi.assert_called_once()


class TestDeployAll:
    """deploy_all 汇总逻辑测试"""

    def _add_cert(self, plugin, order_id, cert_name='test'):
        site = '%s.example.com' % cert_name
        plugin._config.add_cert(
            order_id=order_id, cert_name=cert_name, domains=[site],
            site_names=[site], api_url='https://api.example.com',
            api_token=TOKEN,
        )

    def test_all_success(self, plugin):
        """全部部署成功"""
        self._add_cert(plugin, 930, 'cert-a')
        self._add_cert(plugin, 931, 'cert-b')
        with patch.object(plugin, 'deploy_cert', return_value={'status': True, 'msg': 'ok'}):
            result = plugin.deploy_all()
        assert result['status'] is True
        assert '2 成功' in result['msg']
        assert '失败' not in result['msg']
        assert '私钥' not in result['msg']

    def test_mixed_results(self, plugin):
        """混合结果：成功 + 失败 + need_key"""
        self._add_cert(plugin, 932, 'cert-ok')
        self._add_cert(plugin, 933, 'cert-fail')
        self._add_cert(plugin, 934, 'cert-nokey')

        def fake_deploy(args):
            oid = int(args['order_id'])
            if oid == 932:
                return {'status': True, 'msg': 'ok'}
            if oid == 933:
                return {'status': False, 'msg': 'error'}
            return {'status': False, 'msg': '需要私钥', 'need_key': True}

        with patch.object(plugin, 'deploy_cert', side_effect=fake_deploy):
            result = plugin.deploy_all()
        assert result['status'] is False
        assert '1 成功' in result['msg']
        assert '1 失败' in result['msg']
        assert '1 需要私钥' in result['msg']
        assert 'cert-nokey' in result['msg']

    def test_all_need_key(self, plugin):
        """全部需要私钥"""
        self._add_cert(plugin, 935, 'cert-x')
        self._add_cert(plugin, 936, 'cert-y')
        with patch.object(plugin, 'deploy_cert',
                          return_value={'status': False, 'msg': '需要私钥', 'need_key': True}):
            result = plugin.deploy_all()
        assert result['status'] is False
        assert '2 需要私钥' in result['msg']
        assert 'cert-x' in result['msg']
        assert 'cert-y' in result['msg']
        assert '成功' not in result['msg']

    def test_all_failed(self, plugin):
        """全部证书部署失败时批量顶层状态必须为失败"""
        self._add_cert(plugin, 940, 'cert-a')
        self._add_cert(plugin, 941, 'cert-b')
        with patch.object(plugin, 'deploy_cert',
                          return_value={'status': False, 'msg': '部署失败'}):
            result = plugin.deploy_all()
        assert result['status'] is False
        assert '2 失败' in result['msg']
        assert '成功' not in result['msg']

    def test_no_deployable_certs(self, plugin):
        """无可部署的证书（全部未绑定站点）"""
        plugin._config.add_cert(order_id=937, cert_name='no-site', domains=['a.example.com'])
        result = plugin.deploy_all()
        assert '无可部署' in result['msg']

    def test_filter_by_order_ids(self, plugin):
        """order_ids 过滤只部署选中的证书"""
        self._add_cert(plugin, 938, 'cert-p')
        self._add_cert(plugin, 939, 'cert-q')
        calls = []

        def fake_deploy(args):
            calls.append(int(args['order_id']))
            return {'status': True, 'msg': 'ok'}

        with patch.object(plugin, 'deploy_cert', side_effect=fake_deploy):
            result = plugin.deploy_all({'order_ids': '938'})
        assert result['status'] is True
        assert calls == [938]


class TestBatchSetValidationMethod:
    def test_batch_set_delegation(self, plugin):
        """批量设置委托验证"""
        plugin._config.add_cert(order_id=950, cert_name='test1', domains=['a.example.com'],
                                api_url='https://api.example.com', api_token=TOKEN)
        plugin._config.add_cert(order_id=951, cert_name='test2', domains=['b.example.com'],
                                api_url='https://api.example.com', api_token=TOKEN)
        result = plugin.batch_set_validation_method({'validation_method': 'delegation'})
        assert result['status'] is True
        assert '2' in result['msg']
        assert plugin._config.get_cert(950)['validation_method'] == 'delegation'
        assert plugin._config.get_cert(951)['validation_method'] == 'delegation'

    def test_batch_set_file(self, plugin):
        """批量设置文件验证"""
        plugin._config.add_cert(order_id=952, cert_name='test', domains=['a.example.com'],
                                api_url='https://api.example.com', api_token=TOKEN)
        result = plugin.batch_set_validation_method({'validation_method': 'file'})
        assert result['status'] is True
        assert plugin._config.get_cert(952)['validation_method'] == 'file'

    def test_batch_skip_incompatible(self, plugin):
        """批量设置 file 时跳过通配符域名证书"""
        plugin._config.add_cert(order_id=953, cert_name='normal', domains=['a.example.com'],
                                api_url='https://api.example.com', api_token=TOKEN)
        plugin._config.add_cert(order_id=954, cert_name='wildcard', domains=['*.example.com'],
                                api_url='https://api.example.com', api_token=TOKEN)
        result = plugin.batch_set_validation_method({'validation_method': 'file'})
        assert result['status'] is True
        assert '1' in result['msg']  # 1 个成功
        assert '跳过' in result['msg']
        assert plugin._config.get_cert(953)['validation_method'] == 'file'
        # 通配符证书未被修改
        assert plugin._config.get_cert(954).get('validation_method', '') != 'file'

    def test_batch_invalid_method(self, plugin):
        """无效验证方式被拒绝"""
        result = plugin.batch_set_validation_method({'validation_method': 'invalid'})
        assert result['status'] is False


class TestAddCertPolicyDerive:
    """add_cert 策略派生（SAN 含 IP 强制 local/file）与 auto_reissue（local 关/pull 开）"""

    @patch('sslbt_main.CronManager')
    @patch('sslbt_main.APIClient')
    def test_ip_cert_derives_local_file_auto_reissue_off(self, mock_api_cls, mock_cron_cls, plugin):
        """SAN 含 IP：即使请求 pull 也派生为 local/file，auto_reissue 关闭"""
        mock_api = MagicMock()
        mock_api.query_order.return_value = {'status': 'active', 'domains': '1.2.3.4'}
        mock_api_cls.return_value = mock_api
        mock_cron = MagicMock()
        mock_cron.get_status.return_value = {'exists': True}
        mock_cron_cls.return_value = mock_cron
        result = plugin.add_cert({
            'order_id': '1000', 'api_url': 'https://api.example.com', 'api_token': TOKEN,
            'renew_mode': 'pull',
        })
        assert result['status'] is True
        cert = plugin._config.get_cert(1000)
        assert cert['renew_mode'] == 'local'
        assert cert['validation_method'] == 'file'
        mock_api.toggle_auto_reissue.assert_called_once_with(1000, False)

    @patch('sslbt_main.CronManager')
    @patch('sslbt_main.APIClient')
    def test_dns_pull_auto_reissue_on(self, mock_api_cls, mock_cron_cls, plugin):
        """DNS + pull：保持 pull，auto_reissue 开启"""
        mock_api = MagicMock()
        mock_api.query_order.return_value = {'status': 'active', 'domains': 'a.example.com'}
        mock_api_cls.return_value = mock_api
        mock_cron = MagicMock()
        mock_cron.get_status.return_value = {'exists': True}
        mock_cron_cls.return_value = mock_cron
        result = plugin.add_cert({
            'order_id': '1001', 'api_url': 'https://api.example.com', 'api_token': TOKEN,
            'renew_mode': 'pull',
        })
        assert result['status'] is True
        assert plugin._config.get_cert(1001)['renew_mode'] == 'pull'
        mock_api.toggle_auto_reissue.assert_called_once_with(1001, True)


class TestBatchSetRenewPolicy:
    """批量续签策略：一次原子后端操作、逐证书派生，DNS 不受混合批次影响"""

    def _add(self, plugin, order_id, name, domains):
        plugin._config.add_cert(order_id=order_id, cert_name=name, domains=domains,
                                api_url='https://api.example.com', api_token=TOKEN)

    def test_mixed_batch_per_cert_derivation(self, plugin):
        """混合批次：DNS→local/file，IP→强制 local/file，wildcard+file 跳过（逐证书派生）"""
        self._add(plugin, 800, 'dns', ['a.example.com'])
        self._add(plugin, 801, 'ip', ['1.2.3.4'])
        self._add(plugin, 802, 'wild', ['*.example.com'])
        result = plugin.batch_set_renew_policy({'renew_mode': 'local', 'validation_method': 'file'})
        assert result['status'] is True
        dns = plugin._config.get_cert(800)
        assert dns['renew_mode'] == 'local' and dns['validation_method'] == 'file'
        ip = plugin._config.get_cert(801)
        assert ip['renew_mode'] == 'local' and ip['validation_method'] == 'file'
        wild = plugin._config.get_cert(802)
        assert wild.get('validation_method', '') != 'file'  # 通配符+file 不兼容，跳过
        assert '跳过' in result['msg']

    def test_pull_batch_ip_still_local(self, plugin):
        """批次设为 pull：DNS→pull，IP 证书仍独立派生 local/file，不受混合批次影响"""
        self._add(plugin, 810, 'dns', ['a.example.com'])
        self._add(plugin, 811, 'ip', ['1.2.3.4'])
        result = plugin.batch_set_renew_policy({'renew_mode': 'pull'})
        assert result['status'] is True
        assert plugin._config.get_cert(810)['renew_mode'] == 'pull'
        ip = plugin._config.get_cert(811)
        assert ip['renew_mode'] == 'local'
        assert ip['validation_method'] == 'file'

    def test_clears_policy_blocked(self, plugin):
        """重新设置合法策略清除 policy_blocked 终态"""
        self._add(plugin, 820, 'ip', ['1.2.3.4'])
        plugin._config.update_metadata(820, {'last_issue_state': 'policy_blocked_needs_setup'})
        plugin.batch_set_renew_policy({'renew_mode': 'local', 'validation_method': 'file'})
        assert plugin._config.get_cert(820)['metadata']['last_issue_state'] == ''

    def test_invalid_mode(self, plugin):
        assert plugin.batch_set_renew_policy({'renew_mode': 'invalid'})['status'] is False

    def test_local_requires_validation(self, plugin):
        assert plugin.batch_set_renew_policy({'renew_mode': 'local'})['status'] is False


class TestUpdateClearsPolicyBlocked:
    def test_edit_ip_to_local_file_clears_block(self, plugin):
        """编辑 policy_blocked 的 IP 证书为 local/file → 派生并清除阻断终态"""
        plugin._config.add_cert(order_id=530, cert_name='ip', domains=['1.2.3.4'],
                                api_url='https://api.example.com', api_token=TOKEN)
        plugin._config.update_metadata(530, {'last_issue_state': 'policy_blocked_needs_setup'})
        result = plugin.update_cert_config({
            'order_id': '530', 'renew_mode': 'local', 'validation_method': 'file'})
        assert result['status'] is True
        cert = plugin._config.get_cert(530)
        assert cert['renew_mode'] == 'local'
        assert cert['validation_method'] == 'file'
        assert cert['metadata']['last_issue_state'] == ''


class TestResetIssueState:
    """终态手动恢复入口：清除停更状态与本地计数（deploy-spec §3.2）"""

    def _add(self, plugin, order_id=900):
        plugin._config.add_cert(order_id=order_id, cert_name='c', domains=['a.example.com'],
                                api_url='https://api.example.com', api_token=TOKEN)
        return order_id

    def test_clears_capped_and_counts(self, plugin):
        oid = self._add(plugin)
        plugin._config.update_metadata(oid, {
            'last_issue_state': 'CAPPED',
            'capped_phase': 'issue',
            'issue_retry_count': 10,
            'deploy_attempt_count': 4,
            'deploy_started': True,
            'last_deploy_block_reason': 'Web 服务配置校验失败: boom',
            'last_deploy_block_at': '2026-07-01T00:00:00Z',
        })
        result = plugin.reset_issue_state({'order_id': str(oid)})
        assert result['status'] is True
        meta = plugin._config.get_cert(oid)['metadata']
        assert meta['last_issue_state'] == ''
        assert meta['capped_phase'] == ''
        assert meta['issue_retry_count'] == 0
        assert meta['deploy_attempt_count'] == 0
        assert meta['deploy_started'] is False
        assert meta['last_deploy_block_reason'] == ''
        assert meta['last_deploy_block_at'] == ''

    def test_reset_makes_renewal_eligible_again(self, plugin):
        """恢复后续签引擎重新接手：不再命中终态跳过、也不再命中触顶"""
        from lib.config import TERMINAL_ISSUE_STATES, MAX_ISSUE_RETRY_COUNT

        oid = self._add(plugin, 901)
        plugin._config.update_metadata(oid, {
            'last_issue_state': 'CAPPED', 'capped_phase': 'issue',
            'issue_retry_count': MAX_ISSUE_RETRY_COUNT,
        })
        plugin.reset_issue_state({'order_id': str(oid)})
        meta = plugin._config.get_cert(oid)['metadata']
        assert meta['last_issue_state'] not in TERMINAL_ISSUE_STATES
        assert meta['issue_retry_count'] < MAX_ISSUE_RETRY_COUNT

    def test_missing_order_id(self, plugin):
        assert plugin.reset_issue_state({})['status'] is False

    def test_unknown_order(self, plugin):
        assert plugin.reset_issue_state({'order_id': '99999'})['status'] is False


class TestRenewStatusSymlinkGuard:
    def test_rejects_symlink(self, plugin, tmp_data_dir):
        """状态文件被替换为符号链接时拒绝读取，与其他状态文件读写约定一致"""
        target = os.path.join(tmp_data_dir, 'outside.json')
        with open(target, 'w', encoding='utf-8') as f:
            json.dump({'last_run': 'x'}, f)
        link = os.path.join(tmp_data_dir, 'renew_status.json')
        os.symlink(target, link)
        result = plugin.get_renew_status()
        assert result['status'] is False
        assert '符号链接' in result['msg']


class TestDeployLockBusyMessage:
    """部署/续签互斥锁：提示语不再谎称是续签任务，且先重试再放弃"""

    def test_busy_message_mentions_deploy_or_renew(self, plugin, tmp_data_dir, monkeypatch):
        import sslbt_main as main_mod

        monkeypatch.setattr(main_mod, 'LOCK_RETRIES', 2)
        monkeypatch.setattr(main_mod, 'LOCK_RETRY_INTERVAL', 0)
        holder = open(os.path.join(tmp_data_dir, 'renew.lock'), 'w')
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = plugin.deploy_cert({'order_id': '1'})
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
        assert result['status'] is False
        assert result['msg'] == main_mod.BUSY_MSG
        assert '部署' in result['msg'] and '续签' in result['msg']

    def test_retries_before_giving_up(self, plugin, tmp_data_dir, monkeypatch):
        """抢锁失败先重试若干次，覆盖面板并发请求的短窗口"""
        import sslbt_main as main_mod

        sleeps = []
        monkeypatch.setattr(main_mod, 'LOCK_RETRIES', 3)
        monkeypatch.setattr(main_mod, 'LOCK_RETRY_INTERVAL', 0)
        monkeypatch.setattr(main_mod.time, 'sleep', lambda s: sleeps.append(s))
        holder = open(os.path.join(tmp_data_dir, 'renew.lock'), 'w')
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            plugin.deploy_all({})
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
        assert len(sleeps) == 2  # 3 次尝试之间等待 2 次

    def test_nested_deploy_reenters_without_new_lock(self, plugin):
        """deploy_all 内部嵌套 deploy_cert 走可重入分支，不再抢锁"""
        with plugin._renew_lock() as outer:
            assert outer is True
            with plugin._renew_lock() as inner:
                assert inner is True


class TestManualDeployEnvironmentGate:
    """手动部署遇到坏配置必须直接给出原因，而不是只报「0 成功 N 失败」"""

    @pytest.fixture(autouse=True)
    def _broken_config(self, monkeypatch):
        import public
        monkeypatch.setattr(
            public, 'checkWebConfig',
            lambda: 'nginx: [emerg] unknown directive "-" in /www/.../a.conf:13\n'
                    'nginx: configuration file test failed')

    def _add(self, plugin, order_id=950):
        plugin._config.add_cert(order_id=order_id, cert_name='c', domains=['a.example.com'],
                                site_names=['a.example.com'],
                                api_url='https://api.example.com', api_token=TOKEN)
        return order_id

    def test_deploy_cert_reports_reason(self, plugin):
        oid = self._add(plugin)
        result = plugin.deploy_cert({'order_id': str(oid)})
        assert result['status'] is False
        assert 'Web 服务配置校验失败（非本次部署导致' in result['msg']
        assert 'unknown directive' in result['msg']
        assert '成功' not in result['msg']

    def test_deploy_cert_fails_before_api_query(self, plugin):
        """环境闸门在查询 API 之前，避免无谓的网络往返"""
        oid = self._add(plugin, 951)
        api = MagicMock()
        with patch.object(plugin, '_get_api_for_cert', return_value=api):
            plugin.deploy_cert({'order_id': str(oid)})
        api.query_order.assert_not_called()

    def test_deploy_all_fails_whole_batch_once(self, plugin):
        self._add(plugin, 952)
        self._add(plugin, 953)
        result = plugin.deploy_all({})
        assert result['status'] is False
        assert 'Web 服务配置校验失败（非本次部署导致' in result['msg']

    def test_reason_is_single_line_and_capped(self, plugin):
        oid = self._add(plugin, 954)
        msg = plugin.deploy_cert({'order_id': str(oid)})['msg']
        assert '\n' not in msg


class TestRenewSummaryMessage:
    """续签汇总口径：全失败时不得显示成功语气"""

    def test_summary_counts(self, plugin):
        results = [
            {'order_id': 1, 'status': 'success'},
            {'order_id': 2, 'status': 'pending'},
            {'order_id': 3, 'status': 'failure'},
            {'order_id': 4, 'status': 'failure'},
        ]
        msg = plugin._renew_summary(results)
        assert msg == '续签检查完成：1 成功，1 等待，2 失败'

    def test_summary_empty(self, plugin):
        assert plugin._renew_summary([]) == '续签检查完成：无需续签'

    def test_run_renew_msg_reflects_failures(self, plugin, monkeypatch):
        from lib import renew as renew_mod

        monkeypatch.setattr(
            renew_mod.RenewEngine, 'check_and_renew_all',
            lambda self, spread=False, lock_wait=0: [{'order_id': 1, 'status': 'failure', 'message': 'boom'}])
        result = plugin.run_renew()
        assert result['status'] is True  # 检查本身跑完了，明细在 data 里
        assert '0 成功' in result['msg'] and '1 失败' in result['msg']
