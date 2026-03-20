"""SSL 自动部署 - 宝塔面板插件入口"""

import os
import sys
import json
import time
import secrets

# 插件路径
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, 'data')

# 添加 lib 到路径
sys.path.insert(0, PLUGIN_DIR)

from lib.config import ConfigManager  # noqa: E402
from lib.logger import Logger  # noqa: E402
from lib.api_client import APIClient, APIError  # noqa: E402
from lib.cert_utils import build_fullchain  # noqa: E402
from lib.site_manager import SiteManager  # noqa: E402
from lib.deployer import Deployer, DeployError  # noqa: E402
from lib.renew import RenewEngine  # noqa: E402
from lib.cron import CronManager  # noqa: E402
from lib.updater import Updater  # noqa: E402


def _get_param(args, name, default=''):
    """从宝塔请求参数中获取值"""
    if args is None:
        return default
    if isinstance(args, dict):
        return args.get(name, default)
    return getattr(args, name, default)


def _ok(data=None, msg='操作成功'):
    return {'status': True, 'msg': msg, 'data': data}


def _err(msg='操作失败'):
    return {'status': False, 'msg': msg}


SESSION_EXPIRE_SECONDS = 600  # 10 分钟


class sslbt_main:
    """SSL 自动部署插件"""

    _setup_done = False

    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self._config = ConfigManager(DATA_DIR)
        self._logger = Logger(os.path.join(DATA_DIR, 'logs'))
        self._site_mgr = SiteManager(self._logger)
        self._pending_tokens = {}  # session_id -> {api_url, api_token, created_at}

    def _get_api_for_cert(self, cert_entry):
        """获取证书级别的 API 客户端"""
        url, token = self._config.get_cert_api(cert_entry)
        if not url or not token:
            return None
        return APIClient(url, token, self._logger)

    def _cleanup_sessions(self):
        """清理过期的 pending token 会话"""
        now = time.time()
        expired = [k for k, v in self._pending_tokens.items()
                   if now - v['created_at'] > SESSION_EXPIRE_SECONDS]
        for k in expired:
            del self._pending_tokens[k]

    def _resolve_api_params(self, args):
        """从 session_id 或直接参数中解析 api_url/api_token，返回 (api_url, api_token) 或 (None, error_msg)"""
        session_id = _get_param(args, 'session_id', '').strip()
        if session_id:
            self._cleanup_sessions()
            session = self._pending_tokens.get(session_id)
            if not session:
                return None, '会话已过期，请重新解析部署链接'
            return (session['api_url'], session['api_token']), None
        api_url = _get_param(args, 'api_url', '').strip().rstrip('/')
        api_token = _get_param(args, 'api_token', '').strip()
        if not api_url or not api_token:
            return None, '请提供 API URL 和 Token'
        return (api_url, api_token), None

    def _get_deployer(self):
        return Deployer(self._config, None, self._logger)

    # ==================== 配置管理 ====================

    def get_config(self, args=None):
        """获取插件配置"""
        try:
            cfg = self._config.get_config()
            safe_cfg = dict(cfg)
            # 读取插件版本号
            try:
                with open(os.path.join(PLUGIN_DIR, 'info.json'), 'r') as f:
                    info = json.load(f)
                safe_cfg['plugin_version'] = info.get('versions', '0.0.0')
            except (OSError, json.JSONDecodeError):
                safe_cfg['plugin_version'] = '0.0.0'

            return _ok(safe_cfg)
        except Exception as e:
            return _err('获取配置失败: %s' % str(e))

    def save_config(self, args=None):
        """保存插件配置"""
        try:
            cfg = self._config.get_config()
            interval = _get_param(args, 'check_interval_hours', '')
            renew_days = _get_param(args, 'renew_before_days', '')
            renew_mode = _get_param(args, 'renew_mode', '')

            if interval:
                cfg['check_interval_hours'] = max(1, int(interval))
            if renew_days:
                cfg['renew_before_days'] = max(1, int(renew_days))
            if renew_mode in ('pull', 'local'):
                cfg['renew_mode'] = renew_mode

            update_channel = _get_param(args, 'update_channel', '')
            if update_channel in ('main', 'dev'):
                cfg['update_channel'] = update_channel

            self._config.save_config(cfg)
            self._logger.info("配置已更新")
            return _ok(msg='配置保存成功')
        except Exception as e:
            self._logger.error("保存配置失败: %s", str(e))
            return _err('保存失败: %s' % str(e))

    # ==================== 证书管理 ====================

    def get_cert_list(self, args=None):
        """获取证书列表"""
        try:
            certs = self._config.get_certs()
            for c in certs:
                token = c.get('api_token', '')
                if token:
                    c['api_token_masked'] = token[:6] + '***' + token[-4:] if len(token) > 10 else '***'
                    c['api_token'] = ''
            return _ok(certs)
        except Exception as e:
            return _err('获取证书列表失败: %s' % str(e))

    def add_cert(self, args=None):
        """添加证书订单"""
        try:
            order_id = _get_param(args, 'order_id', '')
            site_names_str = _get_param(args, 'site_names', '')
            site_names = [s.strip() for s in site_names_str.split(',') if s.strip()] if site_names_str else []
            renew_mode = _get_param(args, 'renew_mode', '')

            if not order_id:
                return _err('请提供订单 ID')

            order_id = int(order_id)

            result, err = self._resolve_api_params(args)
            if err:
                return _err(err)
            api_url, api_token = result
            api = APIClient(api_url, api_token, self._logger)

            cert_data = api.query_order(order_id)
            domains = self._parse_cert_domains(cert_data)

            cert_name = 'order-%d' % order_id
            entry = self._config.add_cert(
                order_id=order_id,
                cert_name=cert_name,
                domains=domains,
                site_names=site_names,
                renew_mode=renew_mode,
                api_url=api_url,
                api_token=api_token,
            )

            self._logger.info("添加证书: order_id=%s, domains=%s", order_id, ','.join(domains))
            return _ok(entry, msg='证书添加成功')
        except ValueError as e:
            return _err(str(e))
        except APIError as e:
            return _err('API 错误: %s' % str(e))
        except Exception as e:
            self._logger.error("添加证书失败: %s", str(e))
            return _err('添加失败: %s' % str(e))

    @staticmethod
    def _parse_cert_domains(cert_data):
        """从 API 响应中解析域名列表"""
        domains = []
        domains_str = cert_data.get('domains', '')
        if domains_str:
            for d in domains_str.split(','):
                d = d.strip()
                if d and d not in domains:
                    domains.append(d)
        return domains

    def update_cert_config(self, args=None):
        """更新证书配置"""
        try:
            order_id = _get_param(args, 'order_id', '')
            if not order_id:
                return _err('请提供订单 ID')
            order_id = int(order_id)

            updates = {}
            site_names_str = _get_param(args, 'site_names', '')
            if site_names_str is not None and site_names_str != '':
                updates['site_name'] = [s.strip() for s in site_names_str.split(',') if s.strip()]

            renew_mode = _get_param(args, 'renew_mode', '')
            if renew_mode in ('pull', 'local', ''):
                updates['renew_mode'] = renew_mode

            api_url = _get_param(args, 'api_url', '')
            api_token = _get_param(args, 'api_token', '')
            if api_url:
                if not api_url.startswith(('http://', 'https://')):
                    return _err('API URL 必须以 http:// 或 https:// 开头')
                updates['api_url'] = api_url.strip().rstrip('/')
            if api_token:
                from lib.api_client import validate_token
                validate_token(api_token.strip())
                updates['api_token'] = api_token.strip()

            if not updates:
                return _err('无更新内容')

            self._config.update_cert(order_id, updates)
            self._logger.info("更新证书配置: order_id=%s", order_id)
            return _ok(msg='配置更新成功')
        except ValueError as e:
            return _err(str(e))
        except Exception as e:
            return _err('更新失败: %s' % str(e))

    def remove_cert(self, args=None):
        """删除证书"""
        try:
            order_id = _get_param(args, 'order_id', '')
            if not order_id:
                return _err('请提供订单 ID')
            self._config.remove_cert(int(order_id))
            self._logger.info("删除证书: order_id=%s", order_id)
            return _ok(msg='证书已删除')
        except Exception as e:
            return _err('删除失败: %s' % str(e))

    def deploy_cert(self, args=None):
        """部署指定证书到多个站点"""
        try:
            order_id = _get_param(args, 'order_id', '')
            if not order_id:
                return _err('请提供订单 ID')

            order_id = int(order_id)
            cert_entry = self._config.get_cert(order_id)
            if not cert_entry:
                return _err('订单 %d 不存在' % order_id)

            # site_name 为列表（兼容字符串）
            site_name = cert_entry.get('site_name', [])
            if isinstance(site_name, str):
                site_name = [site_name] if site_name else []
            if not site_name:
                return _err('该证书未绑定站点')

            api = self._get_api_for_cert(cert_entry)
            if not api:
                return _err('请先配置 API 连接')

            # 查询证书
            cert_data = api.query_order(order_id)
            status = cert_data.get('status', '')
            if status != 'active':
                return _err('证书状态为 %s，无法部署' % status)

            certificate = cert_data.get('certificate', '')
            ca_certificate = cert_data.get('ca_certificate', '')
            private_key = cert_data.get('private_key', '')

            if not certificate:
                return _err('证书内容为空')

            fullchain = build_fullchain(certificate, ca_certificate)
            deployer = self._get_deployer()
            results = deployer.deploy_multi(
                site_names=site_name,
                fullchain_pem=fullchain,
                key_pem=private_key,
                order_id=order_id,
                domains=cert_entry.get('domains', []),
                api_client=api,
            )

            success_count = sum(1 for r in results if r['status'])
            fail_count = len(results) - success_count
            if fail_count == 0:
                return _ok(results, msg='部署成功（%d 个站点）' % success_count)
            return _ok(results, msg='部署完成：%d 成功，%d 失败' % (success_count, fail_count))
        except DeployError as e:
            self._logger.error("部署失败: %s", str(e))
            return _err('部署失败: %s' % str(e))
        except APIError as e:
            return _err('API 错误: %s' % str(e))
        except Exception as e:
            self._logger.error("部署失败: %s", str(e))
            return _err('部署失败: %s' % str(e))

    def deploy_all(self, args=None):
        """部署所有证书"""
        try:
            certs = self._config.get_certs()
            results = []
            for cert in certs:
                if not cert.get('enabled', True):
                    continue
                # site_name 为列表，检查是否有绑定站点
                site_name = cert.get('site_name', [])
                if isinstance(site_name, str):
                    site_name = [site_name] if site_name else []
                if not site_name:
                    continue

                class FakeArgs:
                    pass

                fa = FakeArgs()
                fa.order_id = str(cert['order_id'])
                result = self.deploy_cert(fa)
                results.append({
                    'order_id': cert['order_id'],
                    'result': result,
                })
            return _ok(results, msg='批量部署完成')
        except Exception as e:
            return _err('批量部署失败: %s' % str(e))

    def check_cert(self, args=None):
        """检查证书状态"""
        try:
            order_id = _get_param(args, 'order_id', '')
            if not order_id:
                return _err('请提供订单 ID')

            cert_entry = self._config.get_cert(int(order_id))
            if not cert_entry:
                return _err('订单 %s 不存在' % order_id)
            api = self._get_api_for_cert(cert_entry)
            if not api:
                return _err('该证书未配置 API 连接')

            cert_data = api.query_order(int(order_id))
            return _ok(cert_data)
        except APIError as e:
            return _err('API 错误: %s' % str(e))
        except Exception as e:
            return _err('检查失败: %s' % str(e))

    def fetch_deploy_url(self, args=None):
        """后端代理：GET 用户粘贴的部署 URL，返回证书数据"""
        try:
            url = _get_param(args, 'url', '').strip()
            if not url:
                return _err('请提供部署链接')

            from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
            parsed = urlparse(url)
            if parsed.scheme not in ('http', 'https'):
                return _err('不支持的 URL 协议')

            # 从 URL 中解析 token 和 order
            params = parse_qs(parsed.query)
            token_list = params.get('token', [])
            if not token_list:
                return _err('URL 中缺少 token 参数')
            token = token_list[0]

            order_list = params.get('order', [])
            if not order_list:
                return _err('URL 中缺少 order 参数')
            order_value = order_list[0]

            # 构造 api_url（scheme + netloc）
            api_url = '%s://%s' % (parsed.scheme, parsed.netloc)

            # 重建请求 URL：保留路径和 order 参数，去掉 token（通过 Bearer 传递）
            query_params = {k: v[0] for k, v in params.items() if k != 'token'}
            request_url = urlunparse((
                parsed.scheme, parsed.netloc, parsed.path,
                '', urlencode(query_params), '',
            ))

            # GET 请求
            import json as _json
            from urllib.request import Request, urlopen
            import ssl as _ssl
            headers = {
                'Authorization': 'Bearer %s' % token,
                'Accept': 'application/json',
            }
            req = Request(request_url, headers=headers, method='GET')
            ctx = _ssl.create_default_context() if url.startswith('https') else None
            resp = urlopen(req, timeout=30, context=ctx)
            data = _json.loads(resp.read(5 * 1024 * 1024).decode('utf-8'))

            code = data.get('code', 0)
            if code != 1:
                return _err(data.get('msg', 'API 返回错误'))

            cert_data = data.get('data')
            # 统一为列表（分页格式：data.data 为数组）
            if isinstance(cert_data, dict):
                if 'data' in cert_data:
                    certs = cert_data['data'] if isinstance(cert_data['data'], list) else [cert_data['data']]
                else:
                    certs = [cert_data]
            elif isinstance(cert_data, list):
                certs = cert_data
            else:
                certs = []

            # 自动匹配站点
            sites = self._site_mgr.get_sites()
            for c in certs:
                domains = self._parse_cert_domains(c)
                c['_domains'] = domains
                c['_matches'] = SiteManager.match_sites_for_cert(domains, sites)

            # 暂存 token，前端仅持有 session_id
            self._cleanup_sessions()
            session_id = secrets.token_urlsafe(32)
            self._pending_tokens[session_id] = {
                'api_url': api_url,
                'api_token': token,
                'created_at': time.time(),
            }
            return _ok({
                'api_url': api_url,
                'api_token_masked': token[:6] + '***' if len(token) > 6 else '***',
                'session_id': session_id,
                'order': order_value,
                'certs': certs,
            })
        except Exception as e:
            self._logger.error("fetch_deploy_url 失败: %s", str(e))
            return _err('请求失败: %s' % str(e))

    def get_cert_detail(self, args=None):
        """获取单个证书详情（含站点匹配信息）"""
        try:
            order_id = _get_param(args, 'order_id', '')
            if not order_id:
                return _err('请提供订单 ID')

            cert_entry = self._config.get_cert(int(order_id))
            if not cert_entry:
                return _err('订单 %s 不存在' % order_id)

            # 隐藏 api_token
            token = cert_entry.get('api_token', '')
            if token:
                cert_entry['api_token_masked'] = token[:6] + '***' + token[-4:] if len(token) > 10 else '***'
                cert_entry['api_token'] = ''

            # 站点匹配详情
            site_names = cert_entry.get('site_name', [])
            if isinstance(site_names, str):
                site_names = [site_names] if site_names else []

            site_details = []
            for sn in site_names:
                site_info = self._site_mgr.get_site(sn)
                match_info = None
                if site_info:
                    match_info = SiteManager.match_domains(
                        cert_entry.get('domains', []),
                        site_info.get('domains', []),
                    )
                site_details.append({
                    'site_name': sn,
                    'found': site_info is not None,
                    'match': match_info,
                })
            cert_entry['_site_details'] = site_details

            # 有效续签模式
            cert_entry['_effective_renew_mode'] = self._config.get_renew_mode(cert_entry)

            return _ok(cert_entry)
        except Exception as e:
            return _err('获取证书详情失败: %s' % str(e))

    def batch_set_renew_mode(self, args=None):
        """批量设置所有证书的续签模式"""
        try:
            mode = _get_param(args, 'renew_mode', '')
            if mode not in ('pull', 'local'):
                return _err('无效的续签模式')
            certs = self._config.get_certs()
            count = 0
            for c in certs:
                self._config.update_cert(c['order_id'], {'renew_mode': mode})
                count += 1
            return _ok(msg='已将 %d 个证书设为 %s' % (count, '自动签发' if mode == 'pull' else '本机提交'))
        except Exception as e:
            return _err('批量设置失败: %s' % str(e))

    # ==================== 续签 ====================

    def run_renew(self, args=None):
        """手动执行续签检查"""
        try:
            deployer = self._get_deployer()
            engine = RenewEngine(self._config, self._get_api_for_cert, deployer, self._logger)
            results = engine.check_and_renew_all()
            return _ok(results, msg='续签检查完成')
        except Exception as e:
            self._logger.error("续签检查失败: %s", str(e))
            return _err('续签检查失败: %s' % str(e))

    # ==================== 计划任务 ====================

    def setup_cron(self, args=None):
        """设置计划任务"""
        try:
            cfg = self._config.get_config()
            interval = int(_get_param(args, 'interval_hours', '') or cfg.get('check_interval_hours', 6))
            cron_mgr = CronManager(DATA_DIR, self._logger)
            res = cron_mgr.setup(interval)
            if res.get('status'):
                return _ok(msg=res.get('message', '计划任务已创建'))
            return _err(res.get('message', '创建失败'))
        except Exception as e:
            return _err('设置计划任务失败: %s' % str(e))

    def remove_cron(self, args=None):
        """删除计划任务"""
        try:
            cron_mgr = CronManager(DATA_DIR, self._logger)
            cron_mgr.remove()
            return _ok(msg='计划任务已删除')
        except Exception as e:
            return _err('删除失败: %s' % str(e))

    # ==================== 日志 ====================

    def get_logs(self, args=None):
        """获取日志"""
        try:
            date = _get_param(args, 'date', None)
            lines = int(_get_param(args, 'lines', '200') or 200)
            content = self._logger.get_logs(date=date, lines=lines)
            dates = self._logger.get_log_dates()
            return _ok({'content': content, 'dates': dates})
        except Exception as e:
            return _err('获取日志失败: %s' % str(e))

    def clear_logs(self, args=None):
        """清除日志"""
        try:
            self._logger.clear_logs()
            return _ok(msg='日志已清除')
        except Exception as e:
            return _err('清除失败: %s' % str(e))

    # ==================== 更新 ====================

    def check_update(self, args=None):
        """检查插件更新"""
        try:
            updater = Updater(PLUGIN_DIR, self._config, self._logger)
            result = updater.check_update()
            return _ok(result)
        except Exception as e:
            return _err('检查更新失败: %s' % str(e))

    def do_update(self, args=None):
        """执行插件更新"""
        try:
            version = _get_param(args, 'version', '')
            if not version:
                return _err('请指定目标版本')

            download_path = _get_param(args, 'download_path', '')
            checksum = _get_param(args, 'checksum', '')

            updater = Updater(PLUGIN_DIR, self._config, self._logger)
            updater.do_update(
                version=version,
                download_path=download_path,
                checksum=checksum,
            )
            return _ok(msg='更新完成，请刷新页面')
        except Exception as e:
            self._logger.error("更新失败: %s", str(e))
            return _err('更新失败: %s' % str(e))
