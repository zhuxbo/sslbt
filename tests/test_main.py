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
        """全局配置不包含 api_url/api_token"""
        result = plugin.get_config()
        assert result['status'] is True
        data = result['data']
        assert 'api_url' not in data
        assert 'api_token' not in data
        assert 'api_token_masked' not in data
        assert 'check_interval_hours' in data


class TestSaveConfig:
    def test_save_interval_and_renew_days(self, plugin):
        result = plugin.save_config({'check_interval_hours': '12', 'renew_before_days': '15'})
        assert result['status'] is True
        cfg = plugin._config.get_config()
        assert cfg['check_interval_hours'] == 12
        assert cfg['renew_before_days'] == 15

    def test_save_config_no_api_fields(self, plugin):
        """save_config 不处理 api_url/api_token"""
        plugin.save_config({
            'api_url': 'https://evil.com',
            'api_token': 'some-token',
            'check_interval_hours': '8',
        })
        cfg = plugin._config.get_config()
        assert 'api_url' not in cfg
        assert 'api_token' not in cfg
        assert cfg['check_interval_hours'] == 8

    def test_save_renew_mode(self, plugin):
        plugin.save_config({'renew_mode': 'local'})
        cfg = plugin._config.get_config()
        assert cfg['renew_mode'] == 'local'

    def test_save_update_channel(self, plugin):
        plugin.save_config({'update_channel': 'dev'})
        cfg = plugin._config.get_config()
        assert cfg['update_channel'] == 'dev'


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
        assert cert['api_url'] == 'https://api.example.com'
        assert cert['api_token'] == TOKEN


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
        assert certs[0]['api_token'] == ''
        assert '***' in certs[0].get('api_token_masked', '')


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
        assert cert['api_url'] == 'https://new.example.com'
        assert cert['api_token'] == TOKEN  # token 未被清除

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
        assert cert['api_token'] == new_token

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
        assert 'api_token' not in data  # 明文 token 不应出现
        assert data['api_token_masked'].endswith('***')  # 仅返回脱敏值

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
            assert cert['api_url'] == 'https://api.example.com'
            assert cert['api_token'] == TOKEN

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


class TestLogs:
    def test_get_logs(self, plugin):
        plugin._logger.info("test message")
        result = plugin.get_logs({})
        assert result['status'] is True
        assert 'test message' in result['data']['content']
