"""插件入口测试"""

import os
import json
import time
import pytest
from unittest.mock import MagicMock, patch

from lib.config import ConfigManager
from lib.logger import Logger


# 使用独立的 fixture 构造 sslbt_main 实例，避免依赖真实路径
@pytest.fixture
def plugin(tmp_data_dir):
    """构造测试用插件实例"""
    from sslbt_main import sslbt_main

    inst = sslbt_main.__new__(sslbt_main)
    inst._config = ConfigManager(tmp_data_dir)
    inst._logger = Logger(os.path.join(tmp_data_dir, 'logs'))
    inst._site_mgr = MagicMock()
    inst._pending_tokens = {}
    return inst


TOKEN = 'a' * 32 + '.test-token-abcdefghij1234'


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

    @patch('urllib.request.urlopen')
    def test_success_returns_session_id(self, mock_urlopen, plugin):
        """正常流程返回 session_id 而非明文 token"""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            'code': 1,
            'data': {'order_id': 100, 'domains': 'a.com', 'status': 'active'},
        }).encode()
        mock_urlopen.return_value = mock_resp
        plugin._site_mgr.get_sites.return_value = []

        result = plugin.fetch_deploy_url({'url': 'https://api.example.com/deploy?token=secret123456789012345678901234&order=100'})
        assert result['status'] is True
        data = result['data']
        assert 'session_id' in data
        assert 'token' not in data.get('api', {}) or data['api']['token'] == ''  # 明文 token 不应出现
        assert data['api']['token_masked'].endswith('***')  # 仅返回脱敏值

    @patch('urllib.request.urlopen')
    def test_add_cert_with_session_id(self, mock_urlopen, plugin):
        """通过 session_id 添加证书"""
        # 先 fetch 获取 session_id
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            'code': 1,
            'data': {'order_id': 100, 'domains': 'a.com', 'status': 'active'},
        }).encode()
        mock_urlopen.return_value = mock_resp
        plugin._site_mgr.get_sites.return_value = []

        fetch_result = plugin.fetch_deploy_url({'url': 'https://api.example.com/deploy?token=' + TOKEN + '&order=100'})
        session_id = fetch_result['data']['session_id']

        # 用 session_id 添加证书
        with patch('sslbt_main.APIClient') as mock_api_cls:
            mock_api = MagicMock()
            mock_api.query_order.return_value = {'status': 'active', 'domains': 'a.com'}
            mock_api_cls.return_value = mock_api

            result = plugin.add_cert({
                'order_id': '100',
                'session_id': session_id,
                'site_names': 'a.com',
            })
            assert result['status'] is True
            cert = plugin._config.get_cert(100)
            assert cert['api']['url'] == 'https://api.example.com'
            assert cert['api']['token'] == TOKEN

    @patch('urllib.request.urlopen')
    def test_session_id_reusable_for_batch(self, mock_urlopen, plugin):
        """同一 session_id 可用于批量添加多个证书"""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            'code': 1,
            'data': [
                {'order_id': 101, 'domains': 'a.com'},
                {'order_id': 102, 'domains': 'b.com'},
            ],
        }).encode()
        mock_urlopen.return_value = mock_resp
        plugin._site_mgr.get_sites.return_value = []

        fetch_result = plugin.fetch_deploy_url({'url': 'https://api.example.com/deploy?token=' + TOKEN + '&order=101'})
        session_id = fetch_result['data']['session_id']

        with patch('sslbt_main.APIClient') as mock_api_cls:
            mock_api = MagicMock()
            mock_api.query_order.side_effect = [
                {'status': 'active', 'domains': 'a.com'},
                {'status': 'active', 'domains': 'b.com'},
            ]
            mock_api_cls.return_value = mock_api

            r1 = plugin.add_cert({'order_id': '101', 'session_id': session_id, 'site_names': 'a.com'})
            r2 = plugin.add_cert({'order_id': '102', 'session_id': session_id, 'site_names': 'b.com'})
            assert r1['status'] is True
            assert r2['status'] is True

    def test_expired_session(self, plugin):
        """过期 session_id 被拒绝"""
        plugin._pending_tokens['old-session'] = {
            'api_url': 'https://api.example.com',
            'api_token': TOKEN,
            'created_at': time.time() - 700,  # 超过 10 分钟
        }
        result = plugin.add_cert({'order_id': '100', 'session_id': 'old-session'})
        assert result['status'] is False
        assert '过期' in result['msg']

    @patch('urllib.request.urlopen')
    def test_api_error(self, mock_urlopen, plugin):
        """API 返回错误"""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({'code': 0, 'msg': '认证失败'}).encode()
        mock_urlopen.return_value = mock_resp

        result = plugin.fetch_deploy_url({'url': 'https://api.example.com/deploy?token=abc&order=1'})
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
        with patch('sslbt_main.Deployer', return_value=deployer_mock):
            plugin.deploy_cert({'order_id': '910'})

        mock_api.toggle_auto_reissue.assert_called_once_with(910, True)
