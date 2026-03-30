"""证书平台 API 客户端"""

import json
import time
import ssl
import re
import http.client
from urllib.request import Request, HTTPHandler, HTTPSHandler, build_opener
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode, urlparse

API_CODE_SUCCESS = 1
MAX_RETRIES = 3
TIMEOUT_GET = 30
TIMEOUT_POST = 60
MAX_RESPONSE_SIZE = 512 * 1024  # 512KB
MAX_CALLBACK_RESPONSE_SIZE = 64 * 1024  # 64KB
BATCH_MAX_RESPONSE_SIZE = 5 * 1024 * 1024  # 5MB
TOKEN_PATTERN = re.compile(r'^[A-Za-z0-9\-_\.]+$')


class APIError(Exception):
    """API 调用异常"""
    def __init__(self, message, code=0, status_code=0):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def validate_token(token):
    if not token or len(token) < 32 or len(token) > 512:
        raise ValueError("Token 长度必须在 32-512 字符之间")
    if not TOKEN_PATTERN.match(token):
        raise ValueError("Token 包含非法字符，仅允许 A-Za-z0-9-_.")


def _build_api_url(base_url, suffix=''):
    """构建 API URL。如果 base_url 已包含路径则直接追加 suffix，否则添加 /api/deploy"""
    parsed = urlparse(base_url)
    path = parsed.path.rstrip('/')
    if path and path != '/' and '/api/' in path:
        return base_url.rstrip('/') + suffix
    return base_url.rstrip('/') + '/api/deploy' + suffix


# DNS Rebinding 防护：TCP 连接后二次校验目标 IP（spec 10.1）

class _SafeHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        from .net_guard import verify_ip
        ip = self.sock.getpeername()[0]
        reason = verify_ip(ip)
        if reason:
            self.close()
            raise OSError("DNS Rebinding 防护: %s" % reason)


class _SafeHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        from .net_guard import verify_ip
        ip = self.sock.getpeername()[0]
        reason = verify_ip(ip)
        if reason:
            self.close()
            raise OSError("DNS Rebinding 防护: %s" % reason)


class _SafeHTTPHandler(HTTPHandler):
    def http_open(self, req):
        return self.do_open(_SafeHTTPConnection, req)


class _SafeHTTPSHandler(HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_SafeHTTPSConnection, req, context=self._context)


class APIClient:
    """证书平台 API 客户端"""

    def __init__(self, base_url, token, logger=None, timeout=None):
        if not base_url:
            raise ValueError("API URL 不能为空")
        if not base_url.startswith(('http://', 'https://')):
            raise ValueError("API URL 必须以 http:// 或 https:// 开头")
        # HTTPS 强制：仅 localhost/127.0.0.1 允许 HTTP（spec 10.1）
        parsed = urlparse(base_url)
        if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError("API URL 必须使用 HTTPS（仅 localhost/127.0.0.1 允许 HTTP）")
        from .net_guard import check_ssrf
        ssrf_reason = check_ssrf(base_url)
        if ssrf_reason:
            raise ValueError("API URL 不安全: %s" % ssrf_reason)
        validate_token(token)
        self._base_url = base_url.rstrip('/')
        self._token = token
        self._logger = logger
        self._timeout = timeout
        # 使用系统 CA 验证，不支持自签名证书（API 服务端本身是证书签发方，应有有效证书）
        self._ssl_ctx = ssl.create_default_context()
        # DNS Rebinding 防护的安全 opener
        self._opener = build_opener(
            _SafeHTTPHandler(),
            _SafeHTTPSHandler(context=self._ssl_ctx),
        )
        # 上次 API 调用返回的 renew_before_days（> 0 时有效）
        self.last_renew_before_days = 0

    def _request(self, method, url, data=None, max_size=MAX_RESPONSE_SIZE):
        """发送 HTTP 请求，带重试"""
        headers = {
            'Authorization': 'Bearer %s' % self._token,
            'Accept': 'application/json',
        }
        body = None
        if data is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(data).encode('utf-8')

        last_err = None
        for attempt in range(MAX_RETRIES):
            if attempt > 0:
                time.sleep(2 ** (attempt - 1))  # 指数退避: 1s, 2s, 4s
                if self._logger:
                    self._logger.info("API 重试 %d/%d: %s", attempt + 1, MAX_RETRIES, url)
            try:
                req = Request(url, data=body, headers=headers, method=method)
                timeout = self._timeout
                if timeout is None:
                    timeout = TIMEOUT_POST if method == 'POST' else TIMEOUT_GET
                resp = self._opener.open(req, timeout=timeout)
                resp_data = resp.read(max_size)
                return json.loads(resp_data.decode('utf-8'))
            except HTTPError as e:
                last_err = e
                status = e.code
                if status == 401:
                    raise APIError("认证失败，请检查 Token", status_code=401)
                if status == 404:
                    raise APIError("订单不存在", status_code=404)
                if status < 500 and status != 429:
                    try:
                        body_text = e.read(max_size).decode('utf-8')
                        result = json.loads(body_text)
                        raise APIError(result.get('msg', 'API 错误'), code=result.get('code', 0), status_code=status)
                    except (json.JSONDecodeError, OSError):
                        raise APIError("API 错误: HTTP %d" % status, status_code=status)
                # 5xx / 429 → 重试
            except (URLError, OSError) as e:
                last_err = e

        raise APIError("API 请求失败（已重试 %d 次）: %s" % (MAX_RETRIES, str(last_err)))

    def _parse_data(self, result):
        """解析 API 响应，提取 data 字段"""
        if not isinstance(result, dict):
            raise APIError("无效的 API 响应格式")
        code = result.get('code', 0)
        if code != API_CODE_SUCCESS:
            raise APIError(result.get('msg', '未知错误'), code=code)
        data = result.get('data')
        if data is None:
            return {}
        # data 可能是数组，取第一个元素
        if isinstance(data, list):
            return data[0] if data else {}
        return data

    def query_order(self, order_id):
        """查询订单状态"""
        url = _build_api_url(self._base_url) + '?order=%s' % order_id
        if self._logger:
            self._logger.info("查询订单: order_id=%s", order_id)
        result = self._request('GET', url)
        items, total, renew_before_days = self._parse_paginated_data(result)
        if renew_before_days > 0:
            self.last_renew_before_days = renew_before_days
        if not items:
            raise APIError("未找到订单数据")
        return items[0]

    def submit_csr(self, order_id, csr, domains, validation_method=''):
        """提交 CSR。对标 Fetcher.Update"""
        url = _build_api_url(self._base_url)
        if self._logger:
            domain_str = ','.join(domains) if isinstance(domains, list) else domains
            self._logger.info("提交 CSR: order_id=%s, domains=%s", order_id, domain_str)
        data = {
            'order_id': int(order_id),
            'csr': csr,
            'domains': ','.join(domains) if isinstance(domains, list) else domains,
        }
        if validation_method:
            data['validation_method'] = validation_method
        result = self._request('POST', url, data=data)
        cert_data = self._parse_data(result)
        renew_before_days = cert_data.get('renew_before_days', 0)
        if renew_before_days and int(renew_before_days) > 0:
            self.last_renew_before_days = int(renew_before_days)
        return cert_data

    def callback(self, order_id, status, deployed_at=''):
        """部署结果回调"""
        url = _build_api_url(self._base_url, '/callback')
        if self._logger:
            self._logger.info("部署回调: order_id=%s, status=%s", order_id, status)
        data = {
            'order_id': int(order_id),
            'status': status,
            'deployed_at': deployed_at,
        }
        result = self._request('POST', url, data=data, max_size=MAX_CALLBACK_RESPONSE_SIZE)
        if isinstance(result, dict) and result.get('code') == API_CODE_SUCCESS:
            resp_data = result.get('data') or {}
            if isinstance(resp_data, dict):
                renew_before_days = resp_data.get('renew_before_days', 0)
                if renew_before_days and int(renew_before_days) > 0:
                    self.last_renew_before_days = int(renew_before_days)
        return result

    def _parse_paginated_data(self, result):
        """解析批量查询的分页响应，返回 (items, total, renew_before_days)"""
        if not isinstance(result, dict):
            raise APIError("无效的 API 响应格式")
        code = result.get('code', 0)
        if code != API_CODE_SUCCESS:
            raise APIError(result.get('msg', '未知错误'), code=code)
        data = result.get('data')
        # renew_before_days 在 data 层（与 total/data 同级）
        renew_before_days = 0
        if data is None:
            return [], 0, 0
        # 分页格式: {"total": N, "currentPage": 1, "pageSize": 100, "data": [...], "renew_before_days": N}
        if isinstance(data, dict) and 'data' in data:
            items = data.get('data', [])
            total = data.get('total', len(items))
            rbd = data.get('renew_before_days', 0)
            if rbd and int(rbd) > 0:
                renew_before_days = int(rbd)
            return items if isinstance(items, list) else [], total, renew_before_days
        # 兼容数组格式
        if isinstance(data, list):
            return data, len(data), 0
        # 兼容单对象
        return [data], 1, 0

    def query_batch(self, query=''):
        """批量查询证书，自动分页"""
        if self._logger:
            self._logger.info("批量查询证书: query=%s", query or '(全部)')
        page_size = 100
        all_certs = []
        for page in range(1, 100):  # 安全上限
            params = {'pageSize': str(page_size), 'currentPage': str(page)}
            if query:
                params['order'] = query
            url = _build_api_url(self._base_url) + '?' + urlencode(params)
            result = self._request('GET', url, max_size=BATCH_MAX_RESPONSE_SIZE)
            items, total, renew_before_days = self._parse_paginated_data(result)
            if renew_before_days > 0:
                self.last_renew_before_days = renew_before_days
            all_certs.extend(items)
            if len(all_certs) >= total or not items:
                break
        if self._logger:
            self._logger.info("批量查询完成: 共 %d 条", len(all_certs))
        return all_certs

    def toggle_auto_reissue(self, order_id, auto_reissue):
        """切换订单自动续签开关（非关键路径，失败仅记日志）"""
        url = _build_api_url(self._base_url, '/auto-reissue')
        if self._logger:
            self._logger.info("toggle_auto_reissue: order_id=%s, auto_reissue=%s", order_id, auto_reissue)
        data = {
            'order_id': int(order_id),
            'auto_reissue': bool(auto_reissue),
        }
        try:
            result = self._request('POST', url, data=data)
            return result
        except Exception as e:
            if self._logger:
                self._logger.warning("toggle_auto_reissue 失败: order_id=%s, error=%s", order_id, str(e))
            return None

    def test_connection(self):
        """测试 API 连接"""
        url = _build_api_url(self._base_url)
        try:
            self._request('GET', url)
            return True, '连接成功'
        except APIError as e:
            # 401 说明能连通但认证失败
            if e.status_code == 401:
                return False, '认证失败，请检查 Token'
            return False, str(e)
        except Exception as e:
            return False, '连接失败: %s' % str(e)
