"""API 客户端测试"""

import json
import pytest
from unittest.mock import MagicMock
from urllib.error import HTTPError
from io import BytesIO

from lib.api_client import APIClient, APIError, validate_token, _build_api_url


class TestValidateToken:
    def test_valid_token(self):
        validate_token('a' * 32)
        validate_token('A-Za-z0-9._' + 'x' * 21)

    def test_short_token(self):
        with pytest.raises(ValueError, match='长度'):
            validate_token('short')

    def test_invalid_chars(self):
        with pytest.raises(ValueError, match='非法字符'):
            validate_token('a' * 32 + '!')


class TestBuildAPIURL:
    def test_bare_host(self):
        assert _build_api_url('https://api.example.com') == 'https://api.example.com/api/deploy'

    def test_with_path(self):
        assert _build_api_url('https://api.example.com/api/deploy') == 'https://api.example.com/api/deploy'

    def test_with_suffix(self):
        assert _build_api_url('https://api.example.com/api/deploy', '/callback') == 'https://api.example.com/api/deploy/callback'

    def test_trailing_slash(self):
        assert _build_api_url('https://api.example.com/') == 'https://api.example.com/api/deploy'


class TestAPIClient:
    @pytest.fixture
    def client(self):
        c = APIClient('https://api.example.com', 'a' * 32)
        c._opener = MagicMock()
        return c

    def test_init_empty_url(self):
        with pytest.raises(ValueError, match='URL'):
            APIClient('', 'a' * 32)

    def test_init_invalid_token(self):
        with pytest.raises(ValueError, match='长度'):
            APIClient('https://api.example.com', 'short')

    def test_init_invalid_url_scheme(self):
        with pytest.raises(ValueError, match='http'):
            APIClient('file:///etc/passwd', 'a' * 32)

    def test_init_ftp_url_scheme(self):
        with pytest.raises(ValueError, match='http'):
            APIClient('ftp://server.com', 'a' * 32)

    def test_init_http_non_loopback_rejected(self):
        """非 loopback 地址必须使用 HTTPS（spec 10.1）"""
        with pytest.raises(ValueError, match='HTTPS'):
            APIClient('http://api.example.com', 'a' * 32)

    def test_init_http_localhost_allowed(self):
        """localhost 允许 HTTP"""
        c = APIClient('http://localhost:8080', 'a' * 32)
        assert c is not None

    def test_init_http_127_allowed(self):
        """127.0.0.1 允许 HTTP"""
        c = APIClient('http://127.0.0.1:8080', 'a' * 32)
        assert c is not None

    def test_query_order_success(self, client):
        resp_data = json.dumps({
            'code': 1,
            'msg': 'success',
            'data': {
                'total': 1,
                'currentPage': 1,
                'pageSize': 100,
                'data': [{
                    'order_id': 12345,
                    'status': 'active',
                    'domain': 'example.com',
                    'certificate': '---CERT---',
                    'ca_certificate': '---CA---',
                    'private_key': '---KEY---',
                }],
            }
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        result = client.query_order(12345)
        assert result['status'] == 'active'
        assert result['order_id'] == 12345

    def test_query_order_api_error(self, client):
        resp_data = json.dumps({'code': 0, 'msg': '订单不存在'}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        with pytest.raises(APIError, match='订单不存在'):
            client.query_order(99999)

    def test_query_order_401(self, client):
        client._opener.open.side_effect = HTTPError(
            'https://api.example.com', 401, 'Unauthorized', {}, BytesIO(b'')
        )
        with pytest.raises(APIError, match='认证失败'):
            client.query_order(12345)

    def test_query_order_empty_result(self, client):
        """测试查询无结果时抛出异常"""
        resp_data = json.dumps({
            'code': 1,
            'msg': 'success',
            'data': {
                'total': 0,
                'currentPage': 1,
                'pageSize': 100,
                'data': [],
            }
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        with pytest.raises(APIError, match='未找到订单数据'):
            client.query_order(99999)

    def test_callback(self, client):
        resp_data = json.dumps({'code': 1, 'msg': 'success'}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        result = client.callback(
            order_id=12345,
            status='success',
            deployed_at='2026-01-01T00:00:00Z',
        )
        assert result['code'] == 1

    @staticmethod
    def _sent_payload(client):
        """提取 mock opener 收到的请求体 JSON"""
        req = client._opener.open.call_args.args[0]
        return json.loads(req.data.decode('utf-8'))

    def test_callback_failure_with_message(self, client):
        resp_data = json.dumps({'code': 1, 'msg': 'success'}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        client.callback(
            order_id=12345,
            status='failure',
            deployed_at='2026-01-01T00:00:00Z',
            message='s1: nginx 重载失败',
        )
        payload = self._sent_payload(client)
        assert payload['status'] == 'failure'
        assert payload['message'] == 's1: nginx 重载失败'

    def test_callback_without_message_omits_field(self, client):
        resp_data = json.dumps({'code': 1, 'msg': 'success'}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        client.callback(
            order_id=12345,
            status='success',
            deployed_at='2026-01-01T00:00:00Z',
        )
        payload = self._sent_payload(client)
        assert 'message' not in payload

    def test_callback_message_truncated_to_server_limit(self, client):
        # 服务端校验 message 最长 500 字符，超长须客户端截断而非整个回调被拒
        resp_data = json.dumps({'code': 1, 'msg': 'success'}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        client.callback(
            order_id=12345,
            status='failure',
            deployed_at='2026-01-01T00:00:00Z',
            message='错' * 600,
        )
        payload = self._sent_payload(client)
        assert len(payload['message']) == 500

    def test_test_connection_success(self, client):
        resp_data = json.dumps({'code': 1, 'msg': 'ok', 'data': {}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        ok, msg = client.test_connection()
        assert ok is True

    def test_test_connection_auth_fail(self, client):
        client._opener.open.side_effect = HTTPError(
            'https://api.example.com', 401, 'Unauthorized', {}, BytesIO(b'')
        )
        ok, msg = client.test_connection()
        assert ok is False
        assert '认证失败' in msg

    def test_submit_csr_without_validation_method(self, client):
        """submit_csr 不传 validation_method 时不包含该字段"""
        resp_data = json.dumps({'code': 1, 'data': {'status': 'processing'}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        client.submit_csr(123, 'csr-pem', ['a.com'])
        call_args = client._opener.open.call_args
        body = json.loads(call_args[0][0].data.decode('utf-8'))
        assert 'validation_method' not in body
        assert body['order_id'] == 123

    def test_submit_csr_with_validation_method(self, client):
        """submit_csr 传入 validation_method 时包含该字段"""
        resp_data = json.dumps({'code': 1, 'data': {'status': 'processing'}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        client.submit_csr(123, 'csr-pem', ['a.com'], validation_method='file')
        call_args = client._opener.open.call_args
        body = json.loads(call_args[0][0].data.decode('utf-8'))
        assert body['validation_method'] == 'file'

    def test_query_order_caches_renew_before_days(self, client):
        """query_order 响应中 renew_before_days 被缓存到 last_renew_before_days"""
        resp_data = json.dumps({
            'code': 1,
            'data': {
                'total': 1,
                'currentPage': 1,
                'pageSize': 100,
                'renew_before_days': 21,
                'data': [{'order_id': 1, 'status': 'active'}],
            }
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        client.query_order(1)
        assert client.last_renew_before_days == 21

    def test_query_order_no_renew_before_days(self, client):
        """响应中没有 renew_before_days 时 last_renew_before_days 保持 0"""
        resp_data = json.dumps({
            'code': 1,
            'data': {
                'total': 1,
                'data': [{'order_id': 1, 'status': 'active'}],
            }
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        client.query_order(1)
        assert client.last_renew_before_days == 0

    def test_submit_csr_caches_renew_before_days(self, client):
        """submit_csr 响应中 renew_before_days 被缓存"""
        resp_data = json.dumps({
            'code': 1,
            'data': {'status': 'processing', 'renew_before_days': 30},
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        client.submit_csr(123, 'csr-pem', ['a.com'])
        assert client.last_renew_before_days == 30

    def test_callback_caches_renew_before_days(self, client):
        """callback 响应中 renew_before_days 被缓存"""
        resp_data = json.dumps({
            'code': 1,
            'data': {'renew_before_days': 14},
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        client.callback(123, 'success')
        assert client.last_renew_before_days == 14

    def test_toggle_auto_reissue_success(self, client):
        """toggle_auto_reissue 成功调用并返回结果"""
        resp_data = json.dumps({'code': 1, 'msg': 'ok'}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        result = client.toggle_auto_reissue(123, True)
        assert result is not None
        call_args = client._opener.open.call_args
        body = json.loads(call_args[0][0].data.decode('utf-8'))
        assert body['order_id'] == 123
        assert body['auto_reissue'] is True

    def test_toggle_auto_reissue_failure_returns_none(self, client):
        """toggle_auto_reissue 失败时返回 None，不抛异常"""
        from urllib.error import URLError
        client._opener.open.side_effect = URLError('connection refused')

        result = client.toggle_auto_reissue(123, False)
        assert result is None
