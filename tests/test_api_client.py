"""API 客户端测试"""

import json
import pytest
from unittest.mock import patch, MagicMock
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
        return APIClient('https://api.example.com', 'a' * 32)

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

    @patch('lib.api_client.urlopen')
    def test_query_order_success(self, mock_urlopen, client):
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
        mock_urlopen.return_value = mock_resp

        result = client.query_order(12345)
        assert result['status'] == 'active'
        assert result['order_id'] == 12345

    @patch('lib.api_client.urlopen')
    def test_query_order_api_error(self, mock_urlopen, client):
        resp_data = json.dumps({'code': 0, 'msg': '订单不存在'}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        mock_urlopen.return_value = mock_resp

        with pytest.raises(APIError, match='订单不存在'):
            client.query_order(99999)

    @patch('lib.api_client.urlopen')
    def test_query_order_401(self, mock_urlopen, client):
        mock_urlopen.side_effect = HTTPError(
            'https://api.example.com', 401, 'Unauthorized', {}, BytesIO(b'')
        )
        with pytest.raises(APIError, match='认证失败'):
            client.query_order(12345)

    @patch('lib.api_client.urlopen')
    def test_query_order_empty_result(self, mock_urlopen, client):
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
        mock_urlopen.return_value = mock_resp

        with pytest.raises(APIError, match='未找到订单数据'):
            client.query_order(99999)

    @patch('lib.api_client.urlopen')
    def test_callback(self, mock_urlopen, client):
        resp_data = json.dumps({'code': 1, 'msg': 'success'}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        mock_urlopen.return_value = mock_resp

        result = client.callback(
            order_id=12345,
            status='success',
            deployed_at='2026-01-01T00:00:00Z',
        )
        assert result['code'] == 1

    @patch('lib.api_client.urlopen')
    def test_test_connection_success(self, mock_urlopen, client):
        resp_data = json.dumps({'code': 1, 'msg': 'ok', 'data': {}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        mock_urlopen.return_value = mock_resp

        ok, msg = client.test_connection()
        assert ok is True

    @patch('lib.api_client.urlopen')
    def test_test_connection_auth_fail(self, mock_urlopen, client):
        mock_urlopen.side_effect = HTTPError(
            'https://api.example.com', 401, 'Unauthorized', {}, BytesIO(b'')
        )
        ok, msg = client.test_connection()
        assert ok is False
        assert '认证失败' in msg

    @patch('lib.api_client.urlopen')
    def test_submit_csr_without_validation_method(self, mock_urlopen, client):
        """submit_csr 不传 validation_method 时不包含该字段"""
        resp_data = json.dumps({'code': 1, 'data': {'status': 'processing'}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        mock_urlopen.return_value = mock_resp

        client.submit_csr(123, 'csr-pem', ['a.com'])
        call_args = mock_urlopen.call_args
        body = json.loads(call_args[0][0].data.decode('utf-8'))
        assert 'validation_method' not in body
        assert body['order_id'] == 123

    @patch('lib.api_client.urlopen')
    def test_submit_csr_with_validation_method(self, mock_urlopen, client):
        """submit_csr 传入 validation_method 时包含该字段"""
        resp_data = json.dumps({'code': 1, 'data': {'status': 'processing'}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        mock_urlopen.return_value = mock_resp

        client.submit_csr(123, 'csr-pem', ['a.com'], validation_method='file')
        call_args = mock_urlopen.call_args
        body = json.loads(call_args[0][0].data.decode('utf-8'))
        assert body['validation_method'] == 'file'
