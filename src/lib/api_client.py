"""证书平台 API 客户端"""

import os
import json
import time
import ssl
import re
import http.client
from urllib.request import Request, HTTPHandler, HTTPSHandler, build_opener
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode, urlparse

from .logger import sanitize

API_CODE_SUCCESS = 1
MAX_RETRIES = 3
TIMEOUT_GET = 30
TIMEOUT_POST = 60
MAX_RESPONSE_SIZE = 512 * 1024  # 512KB
MAX_CALLBACK_RESPONSE_SIZE = 64 * 1024  # 64KB
CALLBACK_MESSAGE_MAX = 256  # 客户端回调 message 截断上限（服务端校验上限 500，客户端更严格截断至 256）
BATCH_MAX_RESPONSE_SIZE = 5 * 1024 * 1024  # 5MB
TOKEN_PATTERN = re.compile(r'^[A-Za-z0-9\-_\.]+$')
# 查询 order 参数形态（spec §2.3）：与服务端 ApiController::query 的正则同口径
ORDER_PARAM_PATTERN = re.compile(r'^\d+(,\d+)*$')
MAX_BATCH_QUERY_ITEMS = 100  # 逗号分隔项数上限（spec §11）

# 确定性失败分类（deploy-spec §2.2）。错误响应恒 HTTP 200 + code=0，故状态码无法区分
# "确定性失败"与"网络错误"，errors.error_code 是唯一可靠依据。取值与服务端
# ApiErrorCode 一一对应，一旦发布不得改动、只允许新增。
ERR_RATE_LIMITED = 'rate_limited'
ERR_TOKEN_MISSING = 'token_missing'
ERR_TOKEN_INVALID = 'token_invalid'
ERR_TOKEN_DISABLED = 'token_disabled'
ERR_ACCOUNT_DISABLED = 'account_disabled'
ERR_IP_NOT_ALLOWED = 'ip_not_allowed'
ERR_INVALID_ORDER = 'invalid_order'
ERR_ORDER_NOT_FOUND = 'order_not_found'
ERR_CERT_NOT_FOUND = 'cert_not_found'
# 以下四个出现于 §2.6（POST 提交），在本插件只可能来自 local 模式的 CSR 提交
ERR_ORDER_IN_PROGRESS = 'order_in_progress'                  # 唯一的过渡态，见下
ERR_VALIDATION_METHOD_UNSUPPORTED = 'validation_method_unsupported'
ERR_AUTO_RENEW_DISABLED = 'auto_renew_disabled'
ERR_INSUFFICIENT_BALANCE = 'insufficient_balance'

# token / 账号级：失败发生在服务端中间件，与具体订单无关，同一 token 的后续调用必然
# 同样失败。spec §2.2 要求"本轮停止"，调用方据此拉黑该 token 而非逐证书重试
AUTH_BLOCK_ERROR_CODES = frozenset((
    ERR_RATE_LIMITED, ERR_TOKEN_MISSING, ERR_TOKEN_INVALID,
    ERR_TOKEN_DISABLED, ERR_ACCOUNT_DISABLED, ERR_IP_NOT_ALLOWED,
))
# 订单级：只影响单张证书，同 token 的其他证书照常处理。
#
# 除 order_in_progress 外**刻意不再逐值分档**：spec §2.2 里它们的「客户端应对」都是
# 停止本轮该条目、等人工处理，而这正是「带 error_code 即确定性业务拒绝」已有的行为
# （清理在途 pending、计数保留、受签发上限约束，10 轮后触顶静默）。逐值分支只会引入
# 分类漂移。永久性失败的「有界」由计数上限提供、「可见」由 error_code 进错误文本提供，
# 不必也不应为它们各造一个终态——立即终态会杀死自动恢复：用户充值/开开关/改配置后
# 必须再回面板点「恢复自动续签」，只做了前一步的人会以为插件坏了。
ORDER_ERROR_CODES = frozenset((
    ERR_INVALID_ORDER, ERR_ORDER_NOT_FOUND, ERR_CERT_NOT_FOUND,
    ERR_ORDER_IN_PROGRESS, ERR_VALIDATION_METHOD_UNSUPPORTED,
    ERR_AUTO_RENEW_DISABLED, ERR_INSUFFICIENT_BALANCE,
))

# 宝塔内置 Python/OpenSSL 的默认 CA 路径可能与宿主系统不一致；补充加载常见 Linux
# 系统 CA bundle，仍由 SSLContext 严格执行证书链和主机名校验。
_SYSTEM_CA_BUNDLES = (
    '/etc/ssl/certs/ca-certificates.crt',       # Debian / Ubuntu
    '/etc/pki/tls/certs/ca-bundle.crt',         # RHEL / CentOS
    '/etc/ssl/ca-bundle.pem',                   # openSUSE
    '/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem',
)


def _create_ssl_context():
    """创建严格校验的 SSL Context，并补充宿主系统 CA 信任库。"""
    context = ssl.create_default_context()
    for cafile in _SYSTEM_CA_BUNDLES:
        if not os.path.isfile(cafile):
            continue
        try:
            context.load_verify_locations(cafile=cafile)
        except (OSError, ssl.SSLError):
            continue
        break
    return context


class APIError(Exception):
    """API 调用异常。

    transport=True 表示"不确定结果"（网络超时/断连、重试耗尽、响应解析失败）：请求可能已
    到达服务端但结果未知，调用方（如 CSR 提交）应保留 pending key 待下轮查询订单状态恢复，
    不得据此判定业务失败并清理在途状态（deploy-spec §1.3/§10.3）。

    error_code 为服务端下发的确定性失败分类（spec §2.2），空串表示未分类——未分类错误
    沿用既有重试策略，不得因"没有 error_code"而放宽或收紧现有语义。
    """
    def __init__(self, message, code=0, status_code=0, transport=False,
                 error_code='', retry_after=0):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.transport = transport
        self.error_code = error_code
        self.retry_after = retry_after

    @property
    def auth_blocked(self):
        """token / 账号级确定性失败：本轮不应再用该 token 发起任何调用"""
        return self.error_code in AUTH_BLOCK_ERROR_CODES

    @property
    def order_rejected(self):
        """订单级确定性失败：该证书本轮停止，等人工核对配置"""
        return self.error_code in ORDER_ERROR_CODES


def _with_error_code(message, error_code, retry_after=0):
    """把 error_code 拼进错误文本（spec §2.2）

    规范要求它必须出现在客户端错误文本里：日志与面板上的这行字是运维判断「为何停止」的
    唯一线索——服务端 msg 可能是「Unauthorized」这类无指向的通用文案，而 token_disabled
    与 ip_not_allowed 的处置完全不同。

    retry_after 一并带上。其语义是**睡满即可重试的保守秒数**（spec §2.2，2026-07 语义变更，
    取值 61..120），**不是**「当前限流窗口的剩余秒数」：服务端按滑动窗口加权判定，该值刻意
    跨过下一个整窗口——只睡到下一窗口起点时，刚刚超限的那个窗口权重为 1、全额计入，估算值
    必然仍超限。故文案只说「N 秒后重试」，绝不表述成「窗口还剩 N 秒」，否则运维读到的等待
    时间最大偏差一个整窗口。
    """
    if not error_code:
        return message
    tag = error_code
    if retry_after:
        tag = '%s, %d 秒后重试' % (error_code, retry_after)
    return '%s [%s]' % (message, tag)


def _business_error(result, status_code=0):
    """把 code != 1 的错误信封转成 APIError，并提取 errors.error_code 分类（spec §2.2）

    对 errors 的形态一律防御性解析：分类字段缺失/类型不符时退回"未分类"，绝不因为
    解析异常把一次确定性失败变成崩溃。
    """
    errors = result.get('errors')
    error_code = ''
    retry_after = 0
    if isinstance(errors, dict):
        raw_code = errors.get('error_code', '')
        if isinstance(raw_code, str):
            error_code = raw_code
        try:
            retry_after = max(0, int(errors.get('retry_after', 0) or 0))
        except (TypeError, ValueError):
            retry_after = 0
    message = _with_error_code(result.get('msg', '未知错误'), error_code, retry_after)
    return APIError(message, code=result.get('code', 0),
                    status_code=status_code, error_code=error_code,
                    retry_after=retry_after)


def validate_order_param(order):
    """校验查询用的 order 形态（spec §2.3：必填，且只接受订单 ID）

    服务端用 `^\\d+(,\\d+)*$` 判定，不匹配即 invalid_order；本地同口径先挡一道，
    对已移除的形态（域名、空参数）给出可执行的提示而不是服务端原文。
    """
    order = str(order or '').strip()
    if not order:
        raise APIError("查询必须指定订单 ID（order 参数必填）",
                       error_code=ERR_INVALID_ORDER)
    if not ORDER_PARAM_PATTERN.match(order):
        raise APIError(
            "order 参数只接受订单 ID（多个用英文逗号分隔）；按域名查询与空参数查询已不再支持",
            error_code=ERR_INVALID_ORDER)
    if order.count(',') + 1 > MAX_BATCH_QUERY_ITEMS:
        raise APIError("单次最多查询 %d 个订单 ID" % MAX_BATCH_QUERY_ITEMS,
                       error_code=ERR_INVALID_ORDER)
    return order


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
        self._ssl_ctx = _create_ssl_context()
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
                try:
                    return json.loads(resp_data.decode('utf-8'))
                except (json.JSONDecodeError, UnicodeDecodeError) as pe:
                    # 响应已到达但无法解析：属"不确定结果"，标记 transport 供上层保留 pending 恢复
                    raise APIError("响应解析失败: %s" % str(pe), transport=True)
            except HTTPError as e:
                last_err = e
                status = e.code
                # JSON 端点的业务成败一律 HTTP 200 + code=0（spec §2.2），非 200 只可能来自
                # 反代/网关或错误的 URL；认证与订单类失败走 errors.error_code，不再按状态码
                # 猜测原因（401→"认证失败"、404→"订单不存在"对反代响应是误导）。
                # 4xx 一律不重试，429 同样不重试：服务端刻意不用 429 表达限流，而反代的 429
                # 退避（1s→2s→4s）也全落在同一限流窗口内注定失败，只会把恢复时间往后拖
                if status < 500:
                    try:
                        body_text = e.read(max_size).decode('utf-8')
                        result = json.loads(body_text)
                    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                        raise APIError("API 错误: HTTP %d" % status, status_code=status)
                    if not isinstance(result, dict):
                        raise APIError("API 错误: HTTP %d" % status, status_code=status)
                    raise _business_error(result, status_code=status)
                # 5xx → 重试
            except (URLError, OSError) as e:
                last_err = e

        # 网络失败重试耗尽：结果不确定（请求可能已到达服务端），标记 transport
        raise APIError("API 请求失败（已重试 %d 次）: %s" % (MAX_RETRIES, str(last_err)),
                       transport=True)

    def _parse_data(self, result):
        """解析 API 响应，提取 data 字段"""
        if not isinstance(result, dict):
            raise APIError("无效的 API 响应格式")
        code = result.get('code', 0)
        if code != API_CODE_SUCCESS:
            raise _business_error(result)
        data = result.get('data')
        if data is None:
            return {}
        # data 可能是数组，取第一个元素
        if isinstance(data, list):
            return data[0] if data else {}
        return data

    def query_order(self, order_id):
        """查询单个订单状态

        单 ID 未命中由服务端以 error_code=order_not_found 表达（spec §2.3），
        下方空列表分支只是防御——正常契约下走不到。
        """
        order = validate_order_param(order_id)
        url = _build_api_url(self._base_url) + '?' + urlencode({'order': order})
        if self._logger:
            self._logger.info("查询订单: order_id=%s", order_id)
        result = self._request('GET', url)
        items, renew_before_days = self._parse_list_data(result)
        if renew_before_days > 0:
            self.last_renew_before_days = renew_before_days
        if not items:
            raise APIError("未找到订单数据", error_code=ERR_ORDER_NOT_FOUND)
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

    def callback(self, order_id, status, deployed_at='', message=''):
        """部署结果回调（spec 2.8）。

        message 仅在 status=failure 时携带失败原因摘要：先复用 logger 的脱敏规则
        过滤敏感信息（Bearer/Basic/私钥/token 等），再截断至 ≤256 字符（先脱敏后
        截断，避免截断切断凭证残留半个 token；success 不带 message）。
        """
        url = _build_api_url(self._base_url, '/callback')
        if self._logger:
            self._logger.info("部署回调: order_id=%s, status=%s", order_id, status)
        data = {
            'order_id': int(order_id),
            'status': status,
            'deployed_at': deployed_at,
        }
        if message and status == 'failure':
            data['message'] = sanitize(str(message))[:CALLBACK_MESSAGE_MAX]
        result = self._request('POST', url, data=data, max_size=MAX_CALLBACK_RESPONSE_SIZE)
        if isinstance(result, dict) and result.get('code') == API_CODE_SUCCESS:
            resp_data = result.get('data') or {}
            if isinstance(resp_data, dict):
                renew_before_days = resp_data.get('renew_before_days', 0)
                if renew_before_days and int(renew_before_days) > 0:
                    self.last_renew_before_days = int(renew_before_days)
        return result

    def _parse_list_data(self, result):
        """解析查询响应，返回 (items, renew_before_days)

        响应形态为 `{"data": [...], "renew_before_days": N}`（spec §2.3，无分页）。
        刻意不读 total / page / page_size：协议层已不提供，读了只会给翻页循环留接口。
        """
        if not isinstance(result, dict):
            raise APIError("无效的 API 响应格式")
        code = result.get('code', 0)
        if code != API_CODE_SUCCESS:
            raise _business_error(result)
        data = result.get('data')
        if data is None:
            return [], 0
        if not isinstance(data, dict):
            raise APIError("无效的 API 响应格式：data 不是对象")
        items = data.get('data')
        if not isinstance(items, list):
            raise APIError("无效的 API 响应格式：data.data 不是数组")
        renew_before_days = 0
        rbd = data.get('renew_before_days', 0)
        try:
            if rbd and int(rbd) > 0:
                renew_before_days = int(rbd)
        except (TypeError, ValueError):
            renew_before_days = 0
        return items, renew_before_days

    def query_batch(self, query):
        """查询证书（单 ID 或逗号分隔的多个 ID），单次请求取完即止

        spec §2.3 禁止任何翻页循环：翻页的终止只依赖服务端自报的计数与非空页，
        一旦失真即无限翻页且累积内存无界。协议层已不提供该计数，此处也不发分页参数。
        形态校验放在本地、发请求之前——省一次注定被判 invalid_order 的往返，
        也让"域名/空参数"这类已移除的形态得到明确的本地报错。
        """
        query = validate_order_param(query)
        if self._logger:
            self._logger.info("查询证书: order=%s", query)
        url = _build_api_url(self._base_url) + '?' + urlencode({'order': query})
        result = self._request('GET', url, max_size=BATCH_MAX_RESPONSE_SIZE)
        items, renew_before_days = self._parse_list_data(result)
        if renew_before_days > 0:
            self.last_renew_before_days = renew_before_days
        if self._logger:
            self._logger.info("查询完成: 共 %d 条", len(items))
        return items

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
        """测试 API 连通性与 Token 有效性

        刻意不带 order 参数：spec §2.3 下这必然被判 invalid_order，而该错误恰好证明请求
        已穿过认证中间件抵达业务层参数校验——即连通且 Token 有效，无需先持有真实订单号。
        token/账号/IP 类 error_code 则表示连通但被拒，据此给出可执行的提示。
        """
        url = _build_api_url(self._base_url)
        try:
            result = self._request('GET', url)
        except APIError as e:
            # 业务错误恒 HTTP 200，走不到这里；此分支只兜反代/网关的非 200 响应
            if e.auth_blocked:
                return False, 'Token 校验失败: %s' % str(e)
            return False, str(e)
        except Exception as e:
            return False, '连接失败: %s' % str(e)

        if not isinstance(result, dict):
            return False, '无效的 API 响应格式'
        if result.get('code', 0) == API_CODE_SUCCESS:
            return True, '连接成功'
        err = _business_error(result)
        if err.error_code == ERR_INVALID_ORDER:
            return True, '连接成功'
        if err.auth_blocked:
            return False, 'Token 校验失败: %s' % str(err)
        return False, str(err)
