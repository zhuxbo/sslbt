"""API 客户端测试"""

import json
import pytest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from io import BytesIO

from lib.api_client import APIClient, APIError, validate_token, _build_api_url


class TestSSLContext:
    def test_loads_ubuntu_system_ca_bundle(self):
        """宝塔 Python 默认 CA 路径异常时，仍补充加载 Ubuntu 系统 CA。"""
        import lib.api_client as api_client
        assert hasattr(api_client, '_create_ssl_context')
        context = MagicMock()

        def isfile(path):
            return path == '/etc/ssl/certs/ca-certificates.crt'

        with patch('lib.api_client.os.path.isfile', side_effect=isfile), \
                patch('lib.api_client.ssl.create_default_context', return_value=context):
            assert api_client._create_ssl_context() is context
        context.load_verify_locations.assert_called_once_with(
            cafile='/etc/ssl/certs/ca-certificates.crt')


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
        assert _build_api_url('https://api.example.com/api/deploy', '/callback') == \
            'https://api.example.com/api/deploy/callback'

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
                'renew_before_days': 14,
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

    def test_query_order_non_200_from_proxy(self, client):
        """业务失败恒 HTTP 200（spec §2.2），非 200 只可能来自反代：不再按状态码猜原因"""
        client._opener.open.side_effect = HTTPError(
            'https://api.example.com', 401, 'Unauthorized', {}, BytesIO(b'')
        )
        with pytest.raises(APIError, match='HTTP 401') as ei:
            client.query_order(12345)
        assert ei.value.status_code == 401
        assert ei.value.error_code == ''

    def test_query_order_empty_result(self, client):
        """测试查询无结果时抛出异常"""
        resp_data = json.dumps({
            'code': 1,
            'msg': 'success',
            'data': {'renew_before_days': 14, 'data': []},
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

    def test_callback_message_truncated_to_client_limit(self, client):
        # 客户端将 message 截断至 ≤256（服务端上限 500，客户端更严格），超长不致整条被拒
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
        assert len(payload['message']) == 256

    def test_callback_failure_message_sanitized(self, client):
        # 失败原因含 Bearer Token 时须脱敏，绝不泄漏原始凭证到回调请求体
        resp_data = json.dumps({'code': 1, 'msg': 'success'}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        secret = 'Bearer abcDEF123456_secret-token.value'
        client.callback(
            order_id=12345,
            status='failure',
            deployed_at='2026-01-01T00:00:00Z',
            message='部署失败 %s 请重试' % secret,
        )
        payload = self._sent_payload(client)
        assert 'abcDEF123456_secret-token.value' not in payload['message']
        assert 'Bearer ***REDACTED***' in payload['message']

    def test_callback_message_sanitize_before_truncate(self, client):
        # 密钥材料位于 256 截断点之内、END 标记在点外：
        # 若实现改成"先截断后脱敏"，截断产物含 BEGIN+材料但无 END，私钥正则不命中即泄漏，本用例转红
        resp_data = json.dumps({'code': 1, 'msg': 'success'}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        raw = 'x' * 150 + '-----BEGIN PRIVATE KEY-----\nLEAKMATERIAL' + 'A' * 80 + '\n-----END PRIVATE KEY-----'
        client.callback(
            order_id=12345,
            status='failure',
            deployed_at='2026-01-01T00:00:00Z',
            message=raw,
        )
        payload = self._sent_payload(client)
        assert 'LEAKMATERIAL' not in payload['message']
        assert '***REDACTED PRIVATE KEY***' in payload['message']
        assert len(payload['message']) <= 256

    def test_callback_success_with_message_omits_field(self, client):
        # message 仅 failure 携带：即便误传，success 也绝不带出 message
        resp_data = json.dumps({'code': 1, 'msg': 'success'}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        client.callback(
            order_id=12345,
            status='success',
            deployed_at='2026-01-01T00:00:00Z',
            message='不应出现的内容',
        )
        payload = self._sent_payload(client)
        assert 'message' not in payload

    def test_test_connection_success(self, client):
        resp_data = json.dumps({'code': 1, 'msg': 'ok', 'data': {}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        ok, msg = client.test_connection()
        assert ok is True

    def test_test_connection_invalid_order_means_reachable(self, client):
        """不带 order 必被判 invalid_order，而这恰好证明已穿过认证抵达业务层"""
        resp_data = json.dumps({
            'code': 0, 'msg': 'order 参数必填',
            'errors': {'error_code': 'invalid_order'},
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        ok, msg = client.test_connection()
        assert ok is True
        assert '连接成功' in msg

    def test_test_connection_auth_fail(self, client):
        """认证失败走 HTTP 200 + code=0 + error_code（spec §2.2）"""
        resp_data = json.dumps({
            'code': 0, 'msg': 'Invalid token',
            'errors': {'error_code': 'token_invalid'},
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = resp_data
        client._opener.open.return_value = mock_resp

        ok, msg = client.test_connection()
        assert ok is False
        assert 'Token 校验失败' in msg

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

    def test_submit_csr_transport_failure_is_not_retried(self, client):
        """携带 CSR 的 POST 可能已送达，传输失败只能交给 query-only 恢复。"""
        client._opener.open.side_effect = URLError('connection reset')

        with pytest.raises(APIError) as exc:
            client.submit_csr(123, 'csr-pem', ['a.com'])

        assert exc.value.transport is True
        assert client._opener.open.call_count == 1

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


class TestErrorCodeEnvelope:
    """deploy-spec §2.2：errors.error_code 是确定性失败的唯一可靠分类依据"""

    @pytest.fixture
    def client(self):
        c = APIClient('https://api.example.com', 'a' * 32)
        c._opener = MagicMock()
        return c

    @staticmethod
    def _respond(client, payload):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(payload).encode()
        client._opener.open.return_value = mock_resp

    def test_rate_limited_carries_retry_after_and_blocks_token(self, client):
        self._respond(client, {
            'code': 0, 'msg': 'Deploy token rate limit exceeded',
            'errors': {'error_code': 'rate_limited', 'retry_after': 100},
        })
        with pytest.raises(APIError) as ei:
            client.query_order(1)
        assert ei.value.error_code == 'rate_limited'
        assert ei.value.retry_after == 100
        assert ei.value.auth_blocked is True
        assert ei.value.order_rejected is False
        # 确定性失败绝不能被当成网络错误：transport 必须为假，否则调用方会保留在途状态重试
        assert ei.value.transport is False

    @pytest.mark.parametrize('code', [
        'token_missing', 'token_invalid', 'token_disabled',
        'account_disabled', 'ip_not_allowed',
    ])
    def test_auth_level_codes(self, client, code):
        self._respond(client, {'code': 0, 'msg': 'x', 'errors': {'error_code': code}})
        with pytest.raises(APIError) as ei:
            client.query_order(1)
        assert ei.value.auth_blocked is True
        assert ei.value.order_rejected is False

    @pytest.mark.parametrize('code', [
        'invalid_order', 'order_not_found', 'cert_not_found',
        # §2.6（POST 提交）的四个，本插件只可能来自 local 模式的 CSR 提交
        'order_in_progress', 'validation_method_unsupported',
        'auto_renew_disabled', 'insufficient_balance',
    ])
    def test_order_level_codes(self, client, code):
        self._respond(client, {'code': 0, 'msg': 'x', 'errors': {'error_code': code}})
        with pytest.raises(APIError) as ei:
            client.query_order(1)
        assert ei.value.order_rejected is True
        assert ei.value.auth_blocked is False

    def test_unclassified_error_keeps_existing_semantics(self, client):
        """无 error_code 的错误维持原状：既不算 token 阻断也不算订单拒绝"""
        self._respond(client, {'code': 0, 'msg': '未知错误'})
        with pytest.raises(APIError) as ei:
            client.query_order(1)
        assert ei.value.error_code == ''
        assert ei.value.auth_blocked is False
        assert ei.value.order_rejected is False

    @pytest.mark.parametrize('errors', [
        {'error_code': 'rate_limited', 'retry_after': 'soon'},
        {'error_code': 'rate_limited', 'retry_after': None},
        {'error_code': 'rate_limited', 'retry_after': -5},
        {'error_code': 123},
        'not-a-dict',
        None,
    ])
    def test_malformed_errors_never_crash(self, client, errors):
        """errors 形态异常时退回未分类，绝不因解析失败把确定性失败变成崩溃"""
        self._respond(client, {'code': 0, 'msg': 'x', 'errors': errors})
        with pytest.raises(APIError) as ei:
            client.query_order(1)
        assert ei.value.retry_after >= 0

    def test_submit_csr_error_code_is_not_transport(self, client):
        """提交路径同样要能区分：确定性拒绝不得被当作"结果不确定"保留在途状态"""
        self._respond(client, {
            'code': 0, 'msg': 'rate limited',
            'errors': {'error_code': 'rate_limited', 'retry_after': 7},
        })
        with pytest.raises(APIError) as ei:
            client.submit_csr(1, '---CSR---', ['a.com'])
        assert ei.value.auth_blocked is True
        assert ei.value.transport is False

    def test_http_429_from_proxy_is_not_retried(self, client):
        """反代 429 不重试：退避 1s→2s→4s 全落在同一限流窗口内，只会推后恢复"""
        client._opener.open.side_effect = HTTPError(
            'https://api.example.com', 429, 'Too Many Requests', {},
            BytesIO(json.dumps({
                'code': 0, 'msg': 'rate limited',
                'errors': {'error_code': 'rate_limited', 'retry_after': 30},
            }).encode()))
        with pytest.raises(APIError) as ei:
            client.query_order(1)
        assert client._opener.open.call_count == 1
        assert ei.value.error_code == 'rate_limited'
        assert ei.value.retry_after == 30

    def test_http_5xx_still_retries(self, client):
        client._opener.open.side_effect = HTTPError(
            'https://api.example.com', 503, 'Unavailable', {}, BytesIO(b''))
        with patch('lib.api_client.time.sleep'):
            with pytest.raises(APIError) as ei:
                client.query_order(1)
        assert client._opener.open.call_count == 3
        assert ei.value.transport is True


class TestNoPaginationQuery:
    """deploy-spec §2.3：order 必填只接受 ID，查询无分页，客户端不得实现翻页循环"""

    @pytest.fixture
    def client(self):
        c = APIClient('https://api.example.com', 'a' * 32)
        c._opener = MagicMock()
        return c

    @staticmethod
    def _respond(client, payload):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(payload).encode()
        client._opener.open.return_value = mock_resp

    @staticmethod
    def _urls(client):
        return [c[0][0].full_url for c in client._opener.open.call_args_list]

    def test_single_request_even_if_server_lies_about_total(self, client):
        """服务端谎报 total 且返回满页：仍只发一次请求，且 URL 不带任何分页参数

        这是本条最要紧的不变式——翻页循环的终止只依赖服务端自报的计数与非空页，
        两者同时失真即无限翻页且累积内存无界。
        """
        self._respond(client, {
            'code': 1, 'msg': 'ok',
            'data': {
                'total': 99999, 'page': 1, 'page_size': 100,
                'renew_before_days': 14,
                'data': [{'order_id': i} for i in range(100)],
            },
        })
        items = client.query_batch('1,2,3')
        assert len(items) == 100
        assert client._opener.open.call_count == 1
        url = self._urls(client)[0]
        assert 'page' not in url and 'currentPage' not in url and 'pageSize' not in url
        assert 'order=1%2C2%2C3' in url

    def test_renew_before_days_still_picked_up(self, client):
        self._respond(client, {
            'code': 1, 'msg': 'ok',
            'data': {'renew_before_days': 21, 'data': [{'order_id': 1}]},
        })
        client.query_batch('1')
        assert client.last_renew_before_days == 21

    def test_empty_batch_result_is_not_an_error(self, client):
        """批量全未命中返回空数组而非报错（spec §2.3）"""
        self._respond(client, {'code': 1, 'msg': 'ok', 'data': {'data': []}})
        assert client.query_batch('1,2') == []

    @pytest.mark.parametrize('bad', [
        'example.com',            # 已移除：按域名查询
        '',                       # 已移除：空参数列全量
        '   ',
        '1,example.com',          # 已移除：ID + 域名混合
        '1,,2',
        '1;2',
        'abc',
        ','.join(str(i) for i in range(101)),  # 超过批量上限
    ])
    def test_bad_order_form_rejected_locally_without_request(self, client, bad):
        with pytest.raises(APIError) as ei:
            client.query_batch(bad)
        assert ei.value.error_code == 'invalid_order'
        assert client._opener.open.call_count == 0, '形态非法不应发出请求'

    def test_batch_upper_bound_is_accepted(self, client):
        """正好 100 个 ID 必须放行：上限是 100，不是 99"""
        self._respond(client, {'code': 1, 'msg': 'ok', 'data': {'data': []}})
        client.query_batch(','.join(str(i) for i in range(1, 101)))
        assert client._opener.open.call_count == 1

    @pytest.mark.parametrize('data', [
        {'data': 'not-a-list'},
        {'nope': []},
        [],
        'string',
    ])
    def test_malformed_response_shape_rejected(self, client, data):
        self._respond(client, {'code': 1, 'msg': 'ok', 'data': data})
        with pytest.raises(APIError, match='无效的 API 响应格式'):
            client.query_batch('1')

    def test_query_order_sends_no_pagination_params(self, client):
        self._respond(client, {
            'code': 1, 'msg': 'ok', 'data': {'data': [{'order_id': 7, 'status': 'active'}]},
        })
        client.query_order(7)
        url = self._urls(client)[0]
        assert url.endswith('?order=7')


class TestErrorCodeInMessage:
    """spec §2.2：error_code 须进入客户端错误文本

    它是运维判断「为何停止」的唯一线索——服务端 msg 可能是「Unauthorized」这类无指向的
    通用文案，而 token_disabled 与 ip_not_allowed 的处置完全不同。
    """

    @pytest.fixture
    def client(self):
        c = APIClient('https://api.example.com', 'a' * 32)
        c._opener = MagicMock()
        return c

    @staticmethod
    def _respond(client, payload):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(payload).encode()
        client._opener.open.return_value = mock_resp

    def test_error_code_appears_in_text(self, client):
        self._respond(client, {
            'code': 0, 'msg': 'Unauthorized',
            'errors': {'error_code': 'token_disabled'},
        })
        with pytest.raises(APIError) as ei:
            client.query_order(1)
        text = str(ei.value)
        assert 'Unauthorized' in text
        assert 'token_disabled' in text, 'error_code 必须出现在错误文本里'

    def test_retry_after_appears_for_rate_limited(self, client):
        self._respond(client, {
            'code': 0, 'msg': 'rate limit exceeded',
            'errors': {'error_code': 'rate_limited', 'retry_after': 100},
        })
        with pytest.raises(APIError) as ei:
            client.query_order(1)
        text = str(ei.value)
        assert 'rate_limited' in text
        assert '100' in text, '可重试秒数要可见，供运维判断限流状态'
        assert '窗口' not in text, '语义是「睡满即可重试的秒数」，不得表述成窗口剩余'

    def test_unclassified_text_unchanged(self, client):
        """无 error_code 时文本保持原样，不添加空标签"""
        self._respond(client, {'code': 0, 'msg': '订单不存在'})
        with pytest.raises(APIError) as ei:
            client.query_order(1)
        assert str(ei.value) == '订单不存在'
