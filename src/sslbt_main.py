"""SSL 自动部署 - 宝塔面板插件入口"""

import os
import sys
import json
import time
import fcntl
import secrets
import contextlib

# 插件路径
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, 'data')

# 添加 lib 到路径
sys.path.insert(0, PLUGIN_DIR)

# 热更新：宝塔面板每次请求调用 reload(sslbt_main)，但不会递归 reload 子模块，
# 导致升级后 lib/ 下的模块仍是旧版本。检测到 reload 时清除缓存，重新 import 即可。
# 判断方式：reload 会保留当前模块 globals，首次 import 时 sslbt_main 类尚未定义。
# 不依赖模块注册名，兼容宝塔以自定义模块名加载插件。
# 注意：reload 会重置类变量；session 已改为磁盘持久化（data/sessions.json），不受影响。
if 'sslbt_main' in globals():
    for _mod in [k for k in sys.modules if k == 'lib' or k.startswith('lib.')]:
        del sys.modules[_mod]

from lib.config import (  # noqa: E402
    ConfigManager, derive_or_validate_renew_policy, ISSUE_STATE_POLICY_BLOCKED,
)
from lib.logger import Logger  # noqa: E402
from lib.api_client import APIClient, APIError  # noqa: E402
from lib.cert_utils import build_fullchain, parse_cert_info, verify_cert_key_match, validate_key_pem  # noqa: E402
from lib.site_manager import SiteManager  # noqa: E402
from lib.deployer import Deployer, DeployError  # noqa: E402
from lib.renew import RenewEngine  # noqa: E402
from lib.file_verifier import FileVerifier  # noqa: E402
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
SESSION_FILE_NAME = 'sessions.json'


class sslbt_main:
    """SSL 自动部署插件"""

    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        self._data_dir = DATA_DIR
        self._logger = Logger(os.path.join(DATA_DIR, 'logs'))
        self._config = ConfigManager(DATA_DIR, logger=self._logger)
        self._site_mgr = SiteManager(self._logger)

    def _session_file(self):
        return os.path.join(self._data_dir, SESSION_FILE_NAME)

    def _load_sessions(self):
        """从磁盘加载 session 并丢弃过期项。读失败返回空 dict。"""
        path = self._session_file()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f) or {}
        except (OSError, ValueError):
            return {}
        now = time.time()
        return {
            k: v for k, v in data.items()
            if isinstance(v, dict) and v.get('created_at')
            and now - v['created_at'] <= SESSION_EXPIRE_SECONDS
        }

    def _save_sessions(self, tokens):
        """原子写入 session 文件，权限 0600。"""
        os.makedirs(self._data_dir, exist_ok=True)
        path = self._session_file()
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(tokens, f)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)

    def _get_api_for_cert(self, cert_entry):
        """获取证书级别的 API 客户端"""
        url, token = self._config.get_cert_api(cert_entry)
        if not url or not token:
            return None
        return APIClient(url, token, self._logger)

    def _resolve_api_params(self, args):
        """从 session_id 或直接参数中解析 api_url/api_token，返回 (api_url, api_token) 或 (None, error_msg)"""
        session_id = _get_param(args, 'session_id', '').strip()
        if session_id:
            tokens = self._load_sessions()
            session = tokens.get(session_id)
            if not session:
                return None, '会话已过期，请重新解析部署链接'
            return (session['api_url'], session['api_token']), None
        api_url = _get_param(args, 'api_url', '').strip().rstrip('/')
        api_token = _get_param(args, 'api_token', '').strip()
        if not api_url or not api_token:
            return None, '请提供 API URL 和 Token'
        return (api_url, api_token), None

    def _get_deployer(self):
        return Deployer(self._config, None, self._logger, self._site_mgr)

    @contextlib.contextmanager
    def _renew_lock(self):
        """获取续签互斥锁（与 cron 续签共用 data/renew.lock，非阻塞，进程内可重入）

        手动部署与 cron 续签共用同一把锁，避免并发交错执行 SetSSL/reload。
        批量部署（deploy_all）持锁后嵌套调用单证书部署（deploy_cert）会重入放行；
        被其他进程/实例占用时 yield False，调用方应返回 busy 提示。
        """
        # 可重入：本实例已持锁则直接放行（deploy_all 内部嵌套 deploy_cert）
        if getattr(self, '_renew_lock_fd', None) is not None:
            yield True
            return
        lock_path = os.path.join(self._data_dir, 'renew.lock')
        lock_fd = open(lock_path, 'w')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_fd.close()
            yield False
            return
        self._renew_lock_fd = lock_fd
        try:
            yield True
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
            self._renew_lock_fd = None

    def _resolve_private_key(self, cert_data, args, fullchain_pem, site_names):
        """按优先级尝试获取与证书匹配的私钥（deploy-spec §5.3）

        1. API 返回的 private_key
        2. 调用参数指定的私钥路径
        3. 绑定站点的已部署私钥
        4. 调用参数直接传入的 PEM（前端弹窗粘贴）
        """
        candidates = []

        # 1. API 返回
        api_key = cert_data.get('private_key', '')
        if api_key:
            candidates.append(('API', api_key))

        # 2. 参数指定路径
        key_path = _get_param(args, 'private_key_path', '')
        if key_path:
            key_from_file = self._read_key_file(key_path)
            if key_from_file:
                candidates.append(('文件路径', key_from_file))

        # 3. 站点已有私钥
        for sn in site_names:
            site_key = self._read_site_key(sn)
            if site_key:
                candidates.append(('站点 %s' % sn, site_key))
                break  # 同一张证书的多个站点私钥相同，取第一个即可

        # 4. 用户粘贴的 PEM（前端弹窗回传）
        user_key = _get_param(args, 'private_key', '')
        if user_key:
            candidates.append(('用户提供', user_key))

        for source, key_pem in candidates:
            ok, _ = validate_key_pem(key_pem)
            if not ok:
                continue
            if verify_cert_key_match(fullchain_pem, key_pem):
                self._logger.info("私钥来源: %s", source)
                return key_pem

        return ''

    @staticmethod
    def _read_key_file(path):
        """读取私钥文件，路径必须为绝对路径"""
        try:
            if not os.path.isabs(path):
                return ''
            if os.path.islink(path):
                return ''
            if not os.path.isfile(path):
                return ''
            with open(path, 'r') as f:
                return f.read()
        except Exception:
            return ''

    @staticmethod
    def _read_site_key(site_name):
        """通过 panelSite.GetSSL() 读取站点已有私钥"""
        try:
            import panelSite
            params = type('_P', (), {'siteName': site_name})()
            result = panelSite.panelSite().GetSSL(params)
            if isinstance(result, dict):
                return result.get('key', '')
        except Exception:
            pass
        return ''

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
            renew_mode = _get_param(args, 'renew_mode', '')

            if renew_mode in ('pull', 'local'):
                if 'schedule' not in cfg:
                    cfg['schedule'] = {}
                cfg['schedule']['renew_mode'] = renew_mode

            upgrade_channel = _get_param(args, 'upgrade_channel', '')
            if upgrade_channel in ('main', 'dev'):
                cfg['upgrade_channel'] = upgrade_channel

            release_url = _get_param(args, 'release_url', None)
            if release_url is not None:
                cfg['release_url'] = release_url.strip().rstrip('/')

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
                api = c.get('api', {})
                token = api.get('token', '')
                if token:
                    api['token_masked'] = token[:6] + '***' + token[-4:] if len(token) > 10 else '***'
                    api['token'] = ''
                c['_effective_renew_mode'] = self._config.get_renew_mode(c)
            return _ok(certs)
        except Exception as e:
            return _err('获取证书列表失败: %s' % str(e))

    def add_cert(self, args=None):
        """添加证书订单"""
        self._logger.info("add_cert 调用: args=%s", args)
        try:
            order_id = _get_param(args, 'order_id', '')
            site_names_str = _get_param(args, 'site_names', '')
            site_names = [s.strip() for s in site_names_str.split(',') if s.strip()] if site_names_str else []
            renew_mode = _get_param(args, 'renew_mode', '')
            validation_method = _get_param(args, 'validation_method', '')

            if not order_id:
                self._logger.warning("add_cert 早返回: 缺少 order_id")
                return _err('请提供订单 ID')

            order_id = int(order_id)

            result, err = self._resolve_api_params(args)
            if err:
                self._logger.warning("add_cert 早返回: _resolve_api_params 失败 order_id=%s err=%s", order_id, err)
                return _err(err)
            api_url, api_token = result
            api = APIClient(api_url, api_token, self._logger)

            cert_data = api.query_order(order_id)
            domains = self._parse_cert_domains(cert_data)

            # 派生续签策略（SAN 含 IP 强制 local/file；DNS 校验兼容性）——唯一权威
            renew_mode, validation_method, err_msg = derive_or_validate_renew_policy(
                domains, renew_mode, validation_method)
            if err_msg:
                self._logger.warning("add_cert 早返回: 策略派生失败 order_id=%s err=%s", order_id, err_msg)
                return _err(err_msg)

            cert_name = 'order-%d' % order_id
            entry = self._config.add_cert(
                order_id=order_id,
                cert_name=cert_name,
                domains=domains,
                site_names=site_names,
                renew_mode=renew_mode,
                api={'url': api_url, 'token': api_token},
                validation_method=validation_method,
            )

            self._logger.info("添加证书: order_id=%s, domains=%s", order_id, ','.join(domains))

            # 按派生模式设置 auto_reissue（local 关 / pull 开；不自动开付费 auto_renew）
            effective_mode = renew_mode or self._config.get_config().get('schedule', {}).get('renew_mode', 'pull')
            try:
                api.toggle_auto_reissue(order_id, effective_mode == 'pull')
            except Exception as e:
                self._logger.warning("toggle_auto_reissue 失败: order_id=%s, error=%s", order_id, str(e))

            # 自动创建计划任务（如果尚未设置）
            try:
                cron_mgr = CronManager(DATA_DIR, self._logger)
                if not cron_mgr.get_status().get('exists'):
                    cron_mgr.setup()
            except Exception as e:
                self._logger.warning("自动创建计划任务失败: %s", str(e))

            return _ok(entry, msg='证书添加成功')
        except ValueError as e:
            self._logger.warning("add_cert ValueError: %s", str(e))
            return _err(str(e))
        except APIError as e:
            self._logger.error("add_cert APIError: %s", str(e))
            return _err('API 错误: %s' % str(e))
        except Exception as e:
            import traceback
            self._logger.error("add_cert 异常: %s\n%s", str(e), traceback.format_exc())
            return _err('添加失败: %s' % str(e))

    @staticmethod
    def _parse_cert_domains(cert_data):
        """从证书 PEM 提取域名（SAN + CN），证书未签发时回退到 API 域名"""
        # 从证书 PEM 提取（SAN 是权威来源）
        certificate = cert_data.get('certificate', '')
        if certificate:
            cert_info = parse_cert_info(certificate)
            if cert_info and cert_info.get('domains'):
                return list(cert_info['domains'])
        # 证书未签发，回退到 API 返回的域名
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
                requested = [s.strip() for s in site_names_str.split(',') if s.strip()]
                # 排除已被其他证书绑定的站点
                bound = set()
                for c in self._config.get_certs():
                    if c.get('order_id') != order_id:
                        for s in c.get('site_name', []):
                            if s:
                                bound.add(s)
                conflict = [s for s in requested if s in bound]
                if conflict:
                    return _err('站点已被其他证书绑定: %s' % ', '.join(conflict))
                updates['site_name'] = requested

            # 续签策略：以请求值（缺省沿用现值）统一派生（SAN 含 IP 强制 local/file）——唯一权威
            cert0 = self._config.get_cert(order_id)
            domains0 = cert0.get('domains', []) if cert0 else []
            renew_mode = _get_param(args, 'renew_mode', '')
            validation_method = _get_param(args, 'validation_method', '')
            mode_provided = renew_mode in ('pull', 'local', '')
            vm_provided = validation_method in ('file', 'delegation')
            if mode_provided or vm_provided:
                base_mode = renew_mode if mode_provided else (cert0.get('renew_mode', '') if cert0 else '')
                base_vm = validation_method if vm_provided else (cert0.get('validation_method', '') if cert0 else '')
                # pull 模式 validation 不参与（避免旧值触发误报）；IP 会被派生强制为 local/file
                d_mode, d_vm, err_msg = derive_or_validate_renew_policy(
                    domains0, base_mode, base_vm if base_mode != 'pull' else '')
                if err_msg:
                    return _err(err_msg)
                if mode_provided:
                    updates['renew_mode'] = d_mode
                if d_mode == 'local' and (vm_provided or d_vm != base_vm):
                    updates['validation_method'] = d_vm

            api_url = _get_param(args, 'api_url', '')
            api_token = _get_param(args, 'api_token', '')
            if api_url or api_token:
                api_updates = {}
                if api_url:
                    if not api_url.startswith(('http://', 'https://')):
                        return _err('API URL 必须以 http:// 或 https:// 开头')
                    api_updates['url'] = api_url.strip().rstrip('/')
                if api_token:
                    from lib.api_client import validate_token
                    validate_token(api_token.strip())
                    api_updates['token'] = api_token.strip()
                updates['api'] = api_updates

            if not updates:
                return _err('无更新内容')

            self._config.update_cert(order_id, updates)
            # 原为 policy_blocked（旧非法 IP 配置）且本次已设为合法策略 → 清除阻断终态（重新 setup）
            if cert0 and 'renew_mode' in updates \
                    and cert0.get('metadata', {}).get('last_issue_state') == ISSUE_STATE_POLICY_BLOCKED:
                self._config.update_metadata(order_id, {'last_issue_state': ''})
                self._logger.info("证书重新设置合法策略，清除 policy_blocked: order_id=%s", order_id)
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
        """部署指定证书到多个站点（与 cron 续签互斥）"""
        with self._renew_lock() as acquired:
            if not acquired:
                self._logger.warning("deploy_cert 早返回: 续签任务占用锁")
                return _err('续签任务正在执行，请稍后再试')
            return self._deploy_cert_locked(args)

    def _deploy_cert_locked(self, args=None):
        """部署指定证书到多个站点（已持有续签锁）"""
        self._logger.info("deploy_cert 调用: args=%s", args)
        try:
            order_id = _get_param(args, 'order_id', '')
            if not order_id:
                self._logger.warning("deploy_cert 早返回: 缺少 order_id")
                return _err('请提供订单 ID')

            order_id = int(order_id)
            cert_entry = self._config.get_cert(order_id)
            if not cert_entry:
                self._logger.warning("deploy_cert 早返回: 订单 %d 不存在", order_id)
                return _err('订单 %d 不存在' % order_id)

            # site_name 为列表（兼容字符串）
            site_name = cert_entry.get('site_name', [])
            if isinstance(site_name, str):
                site_name = [site_name] if site_name else []
            if not site_name:
                self._logger.warning("deploy_cert 早返回: order_id=%s 未绑定站点", order_id)
                return _err('该证书未绑定站点')

            api = self._get_api_for_cert(cert_entry)
            if not api:
                self._logger.warning("deploy_cert 早返回: order_id=%s API 未配置", order_id)
                return _err('请先配置 API 连接')

            # 查询证书
            cert_data = api.query_order(order_id)

            # 检查订单 ID 是否变化（续费场景）
            new_id = cert_data.get('order_id')
            if new_id and int(new_id) != order_id:
                self._logger.info("订单续费，ID 更新: %s → %s", order_id, new_id)
                try:
                    self._config.update_order_id(order_id, int(new_id))
                    order_id = int(new_id)
                except ValueError as e:
                    self._logger.warning("更新订单 ID 失败: %s", str(e))

            status = cert_data.get('status', '')

            if status == 'processing':
                file_info = cert_data.get('file')
                if file_info:
                    verifier = FileVerifier(self._site_mgr, self._logger)
                    placed = verifier.place_file(file_info, site_name)
                    if placed:
                        self._config.update_metadata(order_id, {
                            'pending_file_verify': file_info,
                            'pending_verify_paths': placed,
                            'last_issue_state': 'processing',
                        })
                        return _ok(msg='验证文件已放置，等待 CA 验证后签发')
                    self._logger.warning("deploy_cert 早返回: order_id=%s 验证文件放置失败", order_id)
                    return _err('验证文件放置失败')
                self._logger.warning("deploy_cert 早返回: order_id=%s processing 但无 file_info", order_id)
                return _err('证书处理中，请稍后再试')

            if status != 'active':
                self._logger.warning("deploy_cert 早返回: order_id=%s 状态为 %s 非 active", order_id, status)
                return _err('证书状态为 %s，无法部署' % status)

            certificate = cert_data.get('certificate', '')
            ca_certificate = cert_data.get('ca_certificate', '')

            if not certificate:
                self._logger.warning("deploy_cert 早返回: order_id=%s 证书内容为空", order_id)
                return _err('证书内容为空')

            # 缺少中间证书守卫：避免残链覆盖站点原有完整链导致信任链断裂
            # （与 renew.py 自动路径 _renew_pull/_handle_processing 一致）
            if not ca_certificate:
                self._logger.warning("deploy_cert 早返回: order_id=%s 缺少中间证书", order_id)
                return _err('缺少中间证书，无法部署')

            fullchain = build_fullchain(certificate, ca_certificate)

            # 私钥回退链（deploy-spec §5.3）
            private_key = self._resolve_private_key(
                cert_data, args, fullchain, site_name)
            if not private_key:
                self._logger.warning("deploy_cert 早返回: order_id=%s 私钥回退链未命中，need_key", order_id)
                return {'status': False, 'msg': '未找到匹配的私钥，请提供私钥', 'need_key': True}

            # 从证书提取域名并更新配置
            domains = self._parse_cert_domains(cert_data)
            if domains and domains != cert_entry.get('domains', []):
                self._config.update_cert(order_id, {'domains': domains})

            deployer = self._get_deployer()
            results = deployer.deploy_multi(
                site_names=site_name,
                fullchain_pem=fullchain,
                key_pem=private_key,
                order_id=order_id,
                domains=domains,
                api_client=api,
            )

            success_count = sum(1 for r in results if r['status'])
            fail_count = len(results) - success_count

            # 部署有成功则更新 auto_reissue
            if success_count > 0:
                effective_mode = self._config.get_renew_mode(cert_entry)
                try:
                    api.toggle_auto_reissue(order_id, effective_mode == 'pull')
                except Exception as e:
                    self._logger.warning("toggle_auto_reissue 失败: order_id=%s, error=%s", order_id, str(e))

            if fail_count == 0:
                return _ok(results, msg='部署成功（%d 个站点）' % success_count)
            return {
                'status': False,
                'msg': '部署完成：%d 成功，%d 失败' % (success_count, fail_count),
                'data': results,
            }
        except DeployError as e:
            self._logger.error("部署失败: %s", str(e))
            return _err('部署失败: %s' % str(e))
        except APIError as e:
            self._logger.error("deploy_cert API 错误: %s", str(e))
            return _err('API 错误: %s' % str(e))
        except Exception as e:
            self._logger.error("部署失败: %s", str(e))
            return _err('部署失败: %s' % str(e))

    def deploy_all(self, args=None):
        """部署证书，支持 order_ids 过滤（与 cron 续签互斥）"""
        with self._renew_lock() as acquired:
            if not acquired:
                return _err('续签任务正在执行，请稍后再试')
            return self._deploy_all_locked(args)

    def _deploy_all_locked(self, args=None):
        """批量部署（已持有续签锁），复用 _deploy_cert_locked 避免重复获取锁"""
        try:
            certs = self._config.get_certs()
            # 支持选中部署
            order_ids_str = _get_param(args, 'order_ids', '')
            filter_ids = set(int(x) for x in order_ids_str.split(',') if x.strip()) if order_ids_str else None

            results = []
            for cert in certs:
                if not cert.get('enabled', True):
                    continue
                if filter_ids and cert['order_id'] not in filter_ids:
                    continue
                site_name = cert.get('site_name', [])
                if isinstance(site_name, str):
                    site_name = [site_name] if site_name else []
                if not site_name:
                    continue

                result = self.deploy_cert({'order_id': str(cert['order_id'])})
                results.append({
                    'order_id': cert['order_id'],
                    'cert_name': cert.get('cert_name', ''),
                    'result': result,
                })

            success = [r for r in results if r['result'].get('status')]
            need_key = [r for r in results if r['result'].get('need_key')]
            failed = [r for r in results if not r['result'].get('status') and not r['result'].get('need_key')]

            parts = []
            if success:
                parts.append('%d 成功' % len(success))
            if failed:
                parts.append('%d 失败' % len(failed))
            if need_key:
                names = [r['cert_name'] or str(r['order_id']) for r in need_key]
                parts.append('%d 需要私钥（%s）' % (len(need_key), ', '.join(names)))
            msg = '批量部署：' + '，'.join(parts) if parts else '无可部署的证书'
            if failed or need_key:
                return {'status': False, 'msg': msg, 'data': results}
            return _ok(results, msg=msg)
        except Exception as e:
            return _err('批量部署失败: %s' % str(e))

    def check_cert(self, args=None):
        """检查证书状态，续费后自动更新订单 ID 和域名"""
        try:
            order_id = _get_param(args, 'order_id', '')
            if not order_id:
                return _err('请提供订单 ID')

            order_id = int(order_id)
            cert_entry = self._config.get_cert(order_id)
            if not cert_entry:
                return _err('订单 %s 不存在' % order_id)
            api = self._get_api_for_cert(cert_entry)
            if not api:
                return _err('该证书未配置 API 连接')

            cert_data = api.query_order(order_id)
            result = dict(cert_data)

            # 检查订单 ID 是否变化（续费场景）
            new_id = cert_data.get('order_id')
            if new_id and int(new_id) != order_id:
                self._logger.info("检查发现订单续费，ID 更新: %s → %s", order_id, new_id)
                try:
                    self._config.update_order_id(order_id, int(new_id))
                    order_id = int(new_id)
                    result['_order_updated'] = True
                    # 更新域名
                    domains = self._parse_cert_domains(cert_data)
                    if domains:
                        self._config.update_cert(order_id, {'domains': domains})
                except ValueError as e:
                    self._logger.warning("更新订单 ID 失败: %s", str(e))

            return _ok(result)
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

            from urllib.parse import urlparse, parse_qs
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

            # 构造 api_url，交给项目统一 API 客户端发请求：HTTPS 强制（拒绝 http，仅
            # localhost 例外）+ SSRF/DNS Rebinding 防护 + token 校验，避免绕过统一安全出口。
            # 保留链接路径以兼容反代子路径部署（如 https://host/manager/api/deploy?...），
            # APIClient._build_api_url 对含 /api/ 的 base_url 直接追加后缀，不重复拼路径
            api_url = '%s://%s%s' % (parsed.scheme, parsed.netloc, parsed.path.rstrip('/'))
            try:
                api = APIClient(api_url, token, self._logger)
            except ValueError as e:
                return _err('部署链接不安全: %s' % str(e))

            certs = api.query_batch(order_value)

            # 自动匹配站点（排除已绑定的站点）
            sites = self._site_mgr.get_sites()
            bound_sites = self._config.get_bound_sites()
            available_sites = [s for s in sites if s['name'] not in bound_sites]
            for c in certs:
                domains = self._parse_cert_domains(c)
                c['_domains'] = domains
                c['_matches'] = SiteManager.match_sites_for_cert(domains, available_sites)

            # 暂存 token，前端仅持有 session_id（持久化到磁盘，避免 reload 丢失）
            tokens = self._load_sessions()
            session_id = secrets.token_urlsafe(32)
            tokens[session_id] = {
                'api_url': api_url,
                'api_token': token,
                'created_at': time.time(),
            }
            self._save_sessions(tokens)
            return _ok({
                'api': {
                    'url': api_url,
                    'token_masked': token[:6] + '***' if len(token) > 6 else '***',
                },
                'session_id': session_id,
                'order': order_value,
                'certs': certs,
            })
        except APIError as e:
            self._logger.error("fetch_deploy_url API 错误: %s", str(e))
            return _err('API 错误: %s' % str(e))
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

            # 隐藏 api token
            api = cert_entry.get('api', {})
            token = api.get('token', '')
            if token:
                api['token_masked'] = token[:6] + '***' + token[-4:] if len(token) > 10 else '***'
                api['token'] = ''

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

    def get_site_matches(self, args=None):
        """获取所有站点与指定证书的匹配度"""
        try:
            order_id = _get_param(args, 'order_id', '')
            if not order_id:
                return _err('请提供订单 ID')
            cert_entry = self._config.get_cert(int(order_id))
            if not cert_entry:
                return _err('订单不存在')
            cert_domains = cert_entry.get('domains', [])
            bound = cert_entry.get('site_name', [])
            if isinstance(bound, str):
                bound = [bound] if bound else []
            bound_set = set(bound)
            sites = self._site_mgr.get_sites()
            result = []
            for s in sites:
                name = s['name']
                match = SiteManager.match_domains(cert_domains, s.get('domains', []))
                result.append({
                    'site_name': name,
                    'bound': name in bound_set,
                    'match_type': match['type'] if match else None,
                    'unmatched': match['unmatched'] if match else [],
                })
            return _ok(result)
        except Exception as e:
            return _err('获取站点匹配失败: %s' % str(e))

    def batch_set_renew_policy(self, args=None):
        """批量设置续签策略（一次原子后端操作，逐证书派生）。

        对每个证书按其域名派生 (renew_mode, validation_method)：SAN 含 IP 强制 local/file，
        DNS 证书采用请求值并做兼容性校验（不兼容跳过）。DNS 证书不受混合批次中 IP 证书影响。
        原为 policy_blocked（旧非法 IP 配置）的证书本次设为合法策略后清除阻断终态。
        """
        try:
            mode = _get_param(args, 'renew_mode', '')
            if mode not in ('pull', 'local'):
                return _err('无效的续签模式')
            validation = _get_param(args, 'validation_method', '')
            if mode == 'local' and validation not in ('file', 'delegation'):
                return _err('本机提交模式需指定验证方式')
            certs = self._config.get_certs()
            count = 0
            skipped = []
            for c in certs:
                domains = c.get('domains', [])
                d_mode, d_vm, err = derive_or_validate_renew_policy(
                    domains, mode, validation if mode == 'local' else '')
                if err:
                    skipped.append(c.get('cert_name') or str(c.get('order_id', '')))
                    continue
                updates = {'renew_mode': d_mode}
                if d_mode == 'local':  # pull 模式 validation 由服务端决定，不改写
                    updates['validation_method'] = d_vm
                self._config.update_cert(c['order_id'], updates)
                if c.get('metadata', {}).get('last_issue_state') == ISSUE_STATE_POLICY_BLOCKED:
                    self._config.update_metadata(c['order_id'], {'last_issue_state': ''})
                count += 1
            msg = '已为 %d 个证书设置续签策略' % count
            if skipped:
                msg += '，%d 个因域名限制跳过：%s' % (len(skipped), ', '.join(skipped))
            return _ok(msg=msg)
        except Exception as e:
            return _err('批量设置失败: %s' % str(e))

    def batch_set_renew_mode(self, args=None):
        """批量设置所有证书的续签模式（逐证书派生，SAN 含 IP 强制 local/file）"""
        try:
            mode = _get_param(args, 'renew_mode', '')
            if mode not in ('pull', 'local'):
                return _err('无效的续签模式')
            certs = self._config.get_certs()
            count = 0
            for c in certs:
                d_mode, d_vm, _ = derive_or_validate_renew_policy(c.get('domains', []), mode, '')
                updates = {'renew_mode': d_mode}
                if d_mode == 'local' and d_vm:
                    updates['validation_method'] = d_vm
                self._config.update_cert(c['order_id'], updates)
                count += 1
            return _ok(msg='已将 %d 个证书设为 %s' % (count, '自动签发' if mode == 'pull' else '本机提交'))
        except Exception as e:
            return _err('批量设置失败: %s' % str(e))

    def batch_set_validation_method(self, args=None):
        """批量设置所有证书的验证方式"""
        try:
            method = _get_param(args, 'validation_method', '')
            if method not in ('file', 'delegation'):
                return _err('无效的验证方式')
            from lib.config import validate_validation_method
            certs = self._config.get_certs()
            count = 0
            skipped = []
            for c in certs:
                err_msg = validate_validation_method(c.get('domains', []), method)
                if err_msg:
                    name = c.get('cert_name') or str(c.get('order_id', ''))
                    skipped.append(name)
                    continue
                self._config.update_cert(c['order_id'], {'validation_method': method})
                count += 1
            label = '委托验证' if method == 'delegation' else '文件验证'
            msg = '已将 %d 个证书设为%s' % (count, label)
            if skipped:
                msg += '，%d 个因域名限制跳过：%s' % (len(skipped), ', '.join(skipped))
            return _ok(msg=msg)
        except Exception as e:
            return _err('批量设置失败: %s' % str(e))

    # ==================== 续签 ====================

    def run_renew(self, args=None):
        """手动执行续签检查"""
        try:
            deployer = self._get_deployer()
            file_verifier = FileVerifier(self._site_mgr, self._logger)
            engine = RenewEngine(self._config, self._get_api_for_cert, deployer, self._logger, file_verifier)
            results = engine.check_and_renew_all()
            return _ok(results, msg='续签检查完成')
        except Exception as e:
            self._logger.error("续签检查失败: %s", str(e))
            return _err('续签检查失败: %s' % str(e))

    def run_renew_cron(self, args=None):
        """计划任务调用的续签检查（分散执行）"""
        try:
            deployer = self._get_deployer()
            file_verifier = FileVerifier(self._site_mgr, self._logger)
            engine = RenewEngine(self._config, self._get_api_for_cert, deployer, self._logger, file_verifier)
            results = engine.check_and_renew_all(spread=True)
            return _ok(results, msg='续签检查完成')
        except Exception as e:
            self._logger.error("续签检查失败: %s", str(e))
            return _err('续签检查失败: %s' % str(e))

    def get_renew_status(self, args=None):
        """获取最近一次续签运行状态（供面板展示最近续签信息）"""
        try:
            path = os.path.join(self._data_dir, 'renew_status.json')
            if not os.path.exists(path):
                return _ok(None)
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return _ok(data)
        except Exception as e:
            return _err('获取续签状态失败: %s' % str(e))

    # ==================== 计划任务 ====================

    def setup_cron(self, args=None):
        """设置计划任务"""
        try:
            cron_mgr = CronManager(DATA_DIR, self._logger)
            res = cron_mgr.setup()
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

    def get_cron_status(self, args=None):
        """获取计划任务状态"""
        try:
            cron_mgr = CronManager(DATA_DIR, self._logger)
            return _ok(cron_mgr.get_status())
        except Exception as e:
            return _err('查询失败: %s' % str(e))

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

            checksum = _get_param(args, 'checksum', '')

            updater = Updater(PLUGIN_DIR, self._config, self._logger)
            updater.do_update(
                version=version,
                checksum=checksum,
            )
            return _ok(msg='更新完成')
        except Exception as e:
            self._logger.error("更新失败: %s", str(e))
            return _err('更新失败: %s' % str(e))


# ===== 命令行调试入口 =====
# 用法:
#   btpython /www/server/panel/plugin/sslbt/sslbt_main.py <method> [json_args]
# 示例:
#   btpython sslbt_main.py get_cert_list
#   btpython sslbt_main.py add_cert '{"order_id":"<ID>","site_names":"www.example.com",
#     "api_url":"https://api.example.com","api_token":"<TOKEN>"}'
#   btpython sslbt_main.py deploy_cert '{"order_id":"<ID>"}'
# 输出: 调用结果 JSON + 异常堆栈直接打到 stderr，同时仍写入日志文件
if __name__ == '__main__':
    import json as _json
    import traceback as _tb

    if len(sys.argv) == 2 and sys.argv[1] == '--version':
        try:
            with open(os.path.join(PLUGIN_DIR, 'info.json'), 'r', encoding='utf-8') as _f:
                print(_json.load(_f).get('versions', '0.0.0'))
        except (OSError, ValueError):
            print('0.0.0')
        sys.exit(0)

    # 让 CLI 行为对齐面板进程：补上宝塔的 class 目录，使 crontab/panelSite 等可 import
    _BT_CLASS_DIR = '/www/server/panel/class'
    if os.path.isdir(_BT_CLASS_DIR) and _BT_CLASS_DIR not in sys.path:
        sys.path.insert(0, _BT_CLASS_DIR)

    if len(sys.argv) < 2:
        print('用法: python sslbt_main.py <method> [json_args]', file=sys.stderr)
        print('可用方法见类 sslbt_main 中以非下划线开头的函数', file=sys.stderr)
        sys.exit(2)

    _method = sys.argv[1]
    _raw = sys.argv[2] if len(sys.argv) >= 3 else ''
    _args = None
    if _raw:
        try:
            _args = _json.loads(_raw)
        except ValueError as _e:
            print('JSON 参数解析失败: %s' % _e, file=sys.stderr)
            sys.exit(2)

    try:
        _instance = sslbt_main()
    except Exception as _e:
        print('初始化失败: %s' % _e, file=sys.stderr)
        _tb.print_exc()
        sys.exit(1)

    _fn = getattr(_instance, _method, None)
    if not callable(_fn) or _method.startswith('_'):
        print('方法不存在或不可调用: %s' % _method, file=sys.stderr)
        sys.exit(2)

    print('[debug] 调用 %s(%s)' % (_method, _args), file=sys.stderr)
    try:
        _result = _fn(_args)
    except Exception as _e:
        print('方法执行抛出异常: %s' % _e, file=sys.stderr)
        _tb.print_exc()
        sys.exit(1)

    try:
        print(_json.dumps(_result, ensure_ascii=False, indent=2, default=str))
    except Exception:
        print(repr(_result))
