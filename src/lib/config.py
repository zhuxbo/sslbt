"""配置管理模块。对标 sslctl pkg/config"""

import os
import json
import copy
import fcntl
import shutil
import ipaddress

# 续签模式与验证方式（deploy-spec §1.2/§1.3）
RENEW_MODE_PULL = 'pull'
RENEW_MODE_LOCAL = 'local'
VALIDATION_METHOD_FILE = 'file'
VALIDATION_METHOD_DELEGATION = 'delegation'

# 签发/部署尝试上限（deploy-spec §11：各自 >= 10 触顶）。renew.py 复用同一常量
MAX_ISSUE_RETRY_COUNT = 10
MAX_DEPLOY_ATTEMPT_COUNT = 10

# last_issue_state 取值（deploy-spec §1.5）
ISSUE_STATE_PROCESSING = 'processing'
ISSUE_STATE_CAPPED = 'CAPPED'            # 触顶静默，记录阶段：issue/deploy/legacy
ISSUE_STATE_EXPIRED = 'EXPIRED'          # 已过期静默
ISSUE_STATE_POLICY_BLOCKED = 'policy_blocked_needs_setup'  # 旧非法 IP 配置，待重新 setup
# CAPPED 触顶阶段（记录到 metadata.cap_stage）
CAP_STAGE_ISSUE = 'issue'
CAP_STAGE_DEPLOY = 'deploy'
CAP_STAGE_LEGACY = 'legacy'
# 触顶/过期/policy 阻断为终态：不再启动新动作、不发回调、迁移不再改写
TERMINAL_ISSUE_STATES = (ISSUE_STATE_CAPPED, ISSUE_STATE_EXPIRED, ISSUE_STATE_POLICY_BLOCKED)


def domains_contain_ip(domains):
    """判断域名列表是否含 IP（IPv4/IPv6）"""
    if not domains:
        return False
    for d in domains:
        try:
            ipaddress.ip_address(d)
            return True
        except (ValueError, TypeError):
            continue
    return False


def validate_validation_method(domains, method):
    """校验域名列表与验证方式的兼容性，返回错误信息或空串"""
    if not method:
        return ''
    for d in domains:
        try:
            ipaddress.ip_address(d)
            is_ip = True
        except (ValueError, TypeError):
            is_ip = False
        is_wildcard = len(d) > 2 and d[:2] == '*.'
        if is_ip and method == VALIDATION_METHOD_DELEGATION:
            return 'IP 地址不支持委托验证'
        if is_wildcard and method == VALIDATION_METHOD_FILE:
            return '通配符域名不支持文件验证'
    return ''


def derive_or_validate_renew_policy(domains, renew_mode='', validation_method=''):
    """派生或校验证书续签策略（deploy-spec §5.2，唯一权威）。

    返回 (renew_mode, validation_method, error)：
    - SAN 含 IP：强制 renew_mode=local + validation_method=file（覆盖入参）；
      local 走 CSR 提交，pull 不带 CSR 的差异由续签引擎按模式处理。IP 永不返回 error。
    - 非 IP：沿用入参 renew_mode；validation_method 经域名兼容性校验，不兼容返回 error。
    """
    if domains_contain_ip(domains):
        return RENEW_MODE_LOCAL, VALIDATION_METHOD_FILE, ''
    err = validate_validation_method(domains, validation_method)
    if err:
        return renew_mode, validation_method, err
    return renew_mode, validation_method, ''


DEFAULT_CONFIG = {
    'release_url': '',
    'upgrade_channel': 'main',
    'schedule': {
        'renew_mode': 'pull',
        'renew_before_days': 14,
    },
    'certificates': [],
}

DEFAULT_CERT_ENTRY = {
    'order_id': 0,
    'cert_name': '',
    'domains': [],
    'enabled': True,
    'renew_mode': '',
    'validation_method': '',
    'api': {
        'url': '',
        'token': '',
    },
    'site_name': [],
    'server_type': 'nginx',
    'metadata': {
        'last_deploy_at': '',
        'cert_expires_at': '',
        'cert_serial': '',
        'csr_submitted_at': '',
        'last_csr_hash': '',
        'last_issue_state': '',
        'issue_retry_count': 0,
        'deploy_attempt_count': 0,
    },
}

# ==================== 迁移规则（数据驱动） ====================
# 添加新的迁移只需在此追加条目，不需要修改迁移逻辑
#
# 规则格式:
#   'old_key': ('delete',)                    静默移除
#   'old_key': ('rename', 'new_key')          重命名
#   'old_key': ('move', 'parent', 'child')    扁平字段移入子对象
#
# spread 操作通过 _SPREAD_RULES 独立定义（全局字段分发到数组元素）:
#   ('source_key', 'array_key', 'target_key') — 顶层 source_key → 每个 array_key[] 元素的 target_key

# 全局字段迁移
_GLOBAL_FIELD_RULES = {
    'renew_before_days':    ('move', 'schedule', 'renew_before_days'),
    'renew_mode':           ('move', 'schedule', 'renew_mode'),
    'update_channel':       ('rename', 'upgrade_channel'),
    'api_url':              ('delete',),
    'api_token':            ('delete',),
    'version':              ('delete',),
    'check_interval_hours': ('delete',),
}

# 证书字段迁移
_CERT_FIELD_RULES = {
    'api_url':   ('move', 'api', 'url'),
    'api_token': ('move', 'api', 'token'),
}

# 旧文件合并：旧文件名 → 合并到哪个字段
_OLD_FILE_MERGES = {
    'certs.json': 'certificates',
}

# spread 规则：顶层字段分发到数组元素（仅补全缺失字段）
# ('source_key', 'array_key', 'target_key')
_SPREAD_RULES = []


# ==================== 通用迁移引擎 ====================

def _apply_field_rules(data, rules):
    """对 dict 就地执行字段迁移规则，返回 changed 标志"""
    changed = False
    moves = []
    renames = []
    deletes = []

    for key in list(data.keys()):
        rule = rules.get(key)
        if not rule:
            continue
        action = rule[0]
        if action == 'delete':
            deletes.append(key)
        elif action == 'rename':
            renames.append((key, rule[1]))
        elif action == 'move':
            moves.append((key, rule[1], rule[2]))

    for key in deletes:
        del data[key]
        changed = True

    for old_key, new_key in renames:
        if new_key not in data:
            data[new_key] = data[old_key]
            changed = True
        del data[old_key]

    for old_key, parent, child in moves:
        if parent not in data or not isinstance(data[parent], dict):
            data[parent] = {}
        data[parent].setdefault(child, data[old_key])
        del data[old_key]
        changed = True

    return changed


def _apply_spread_rules(data, rules):
    """将顶层字段分发到数组元素（仅补全缺失字段），返回 changed 标志"""
    changed = False
    for source_key, array_key, target_key in rules:
        value = data.get(source_key)
        items = data.get(array_key)
        if value is None or not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if target_key not in item:
                item[target_key] = copy.deepcopy(value) if isinstance(value, (dict, list)) else value
                changed = True
    return changed


def _fill_defaults(data, defaults):
    """根据 defaults 结构递归补齐缺失字段、校正类型，返回是否有变化

    - 缺失字段：填入默认值
    - 默认 dict 但实际非 dict：替换为默认值
    - 默认 list 但实际 str：转换为 [str] 或 []
    """
    changed = False
    for key, default_value in defaults.items():
        if key not in data:
            data[key] = copy.deepcopy(default_value) if isinstance(default_value, (dict, list)) else default_value
            changed = True
        elif isinstance(default_value, dict):
            if not isinstance(data[key], dict):
                data[key] = copy.deepcopy(default_value)
                changed = True
            else:
                changed = _fill_defaults(data[key], default_value) or changed
        elif isinstance(default_value, list) and isinstance(data[key], str):
            data[key] = [data[key]] if data[key] else []
            changed = True
    return changed


def _migrate_cert_semantics(cert, global_renew_mode):
    """对单个证书应用计算型语义迁移（幂等），返回 changed 标志（deploy-spec §3.4/§5.2）。

    - 旧 `pending` 归一为 `processing`（只查询、不重复提交、不增计数、不重生 CSR）
    - 旧计数触顶（issue/deploy >= 10）升级后立即进入 CAPPED(legacy) 静默，不补发历史事件
    - 旧非法 IP 配置（IP + pull 或 IP + delegation）进入 policy_blocked_needs_setup：
      不自动改配置、不计数、不回调，等待重新 setup
    部署计数从零开始由默认值补 0 保证，不从旧混合计数推断。触顶/过期/policy 均为终态，
    迁移只置状态、不产生任何回调。
    """
    meta = cert.get('metadata')
    if not isinstance(meta, dict):
        return False
    changed = False
    state = meta.get('last_issue_state', '')

    # 1. 旧 pending 归一 processing
    if state == 'pending':
        state = ISSUE_STATE_PROCESSING
        meta['last_issue_state'] = state
        changed = True

    # 2. 旧计数触顶 → CAPPED(legacy)，不补发历史
    if state not in TERMINAL_ISSUE_STATES:
        try:
            issue_count = int(meta.get('issue_retry_count', 0) or 0)
        except (TypeError, ValueError):
            issue_count = 0
        try:
            deploy_count = int(meta.get('deploy_attempt_count', 0) or 0)
        except (TypeError, ValueError):
            deploy_count = 0
        if issue_count >= MAX_ISSUE_RETRY_COUNT or deploy_count >= MAX_DEPLOY_ATTEMPT_COUNT:
            meta['last_issue_state'] = ISSUE_STATE_CAPPED
            meta['cap_stage'] = CAP_STAGE_LEGACY
            state = ISSUE_STATE_CAPPED
            changed = True

    # 3. 旧非法 IP 配置 → policy_blocked_needs_setup（不改 renew_mode/validation_method）
    if state not in TERMINAL_ISSUE_STATES and domains_contain_ip(cert.get('domains', [])):
        effective_mode = cert.get('renew_mode', '') or global_renew_mode
        if effective_mode == RENEW_MODE_PULL or cert.get('validation_method', '') == VALIDATION_METHOD_DELEGATION:
            meta['last_issue_state'] = ISSUE_STATE_POLICY_BLOCKED
            changed = True

    return changed


class ConfigManager:
    """配置读写管理，文件锁保护"""

    def __init__(self, data_dir, logger=None):
        self._data_dir = data_dir
        self._logger = logger
        self._config_path = os.path.join(data_dir, 'config.json')
        os.makedirs(data_dir, exist_ok=True)
        self._ensure_config()

    def _ensure_config(self):
        """启动时校验配置：合并旧文件、执行迁移、持久化"""
        raw = self._read_json(self._config_path, DEFAULT_CONFIG)
        changed = False

        # 合并旧文件（先记录，写入成功后再删除）
        merged_files = []
        for old_name, field in _OLD_FILE_MERGES.items():
            old_path = os.path.join(self._data_dir, old_name)
            if not os.path.isfile(old_path):
                continue
            try:
                old_data = self._read_json(old_path, {})
                items = old_data.get(field, [])
                if items and not raw.get(field):
                    raw[field] = items
                    changed = True
                merged_files.append(old_path)
            except OSError:
                pass

        # 全局字段迁移
        changed = _apply_field_rules(raw, _GLOBAL_FIELD_RULES) or changed
        changed = _apply_spread_rules(raw, _SPREAD_RULES) or changed
        changed = _fill_defaults(raw, DEFAULT_CONFIG) or changed

        # 证书字段迁移 + 计算型语义迁移（pending 归一、legacy 触顶、policy 阻断）
        global_renew_mode = raw.get('schedule', {}).get('renew_mode', RENEW_MODE_PULL)
        for cert in raw.get('certificates', []):
            changed = _apply_field_rules(cert, _CERT_FIELD_RULES) or changed
            changed = _fill_defaults(cert, DEFAULT_CERT_ENTRY) or changed
            changed = _migrate_cert_semantics(cert, global_renew_mode) or changed

        if changed:
            try:
                self._write_json(self._config_path, raw)
            except OSError:
                pass

        # 写入成功后再删除旧文件
        for path in merged_files:
            try:
                os.remove(path)
            except OSError:
                pass

    # ==================== JSON 读写 ====================

    def _read_json(self, path, default):
        if not os.path.isfile(path):
            return copy.deepcopy(default)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                fcntl.flock(f, fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
            return data
        except json.JSONDecodeError:
            if self._logger:
                self._logger.error("配置文件 JSON 解析失败: %s", path)
            try:
                shutil.copy2(path, path + '.bak')
            except OSError:
                pass
            return copy.deepcopy(default)
        except OSError:
            return copy.deepcopy(default)

    def _write_json(self, path, data):
        if os.path.islink(path):
            raise OSError("refusing to write to symlink: %s" % os.path.basename(path))
        tmp_path = path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)

    def _update_json(self, path, updater_fn, default):
        """原子读-改-写: 在排他锁保护下执行 updater_fn(data) -> data"""
        if os.path.islink(path):
            raise OSError("refusing to write to symlink: %s" % os.path.basename(path))
        lock_path = path + '.lock'
        with open(lock_path, 'w') as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                if os.path.isfile(path):
                    try:
                        with open(path, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                    except json.JSONDecodeError:
                        if self._logger:
                            self._logger.error("配置文件 JSON 解析失败: %s", path)
                        try:
                            shutil.copy2(path, path + '.bak')
                        except OSError:
                            pass
                        data = copy.deepcopy(default)
                    except OSError:
                        data = copy.deepcopy(default)
                else:
                    data = copy.deepcopy(default)
                result = updater_fn(data)
                tmp_path = path + '.tmp'
                with open(tmp_path, 'w', encoding='utf-8') as tf:
                    json.dump(result, tf, indent=2, ensure_ascii=False)
                    tf.flush()
                    os.fsync(tf.fileno())
                os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, path)
                return result
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    # ==================== 全局配置 ====================

    def get_config(self):
        """返回全局配置（不含 certificates）"""
        raw = self._read_json(self._config_path, DEFAULT_CONFIG)
        _apply_field_rules(raw, _GLOBAL_FIELD_RULES)
        _fill_defaults(raw, DEFAULT_CONFIG)
        return {k: v for k, v in raw.items() if k != 'certificates'}

    def save_config(self, cfg):
        """保存全局配置字段，保留 certificates 不变"""
        def updater(data):
            clean = copy.deepcopy(cfg)
            _apply_field_rules(clean, _GLOBAL_FIELD_RULES)
            for k, v in clean.items():
                if k != 'certificates':
                    data[k] = v
            return data

        self._update_json(self._config_path, updater, DEFAULT_CONFIG)

    # ==================== 证书列表 ====================

    @staticmethod
    def _normalize_certs(certs):
        """对证书列表执行迁移和默认值填充"""
        for cert in certs:
            _apply_field_rules(cert, _CERT_FIELD_RULES)
            _fill_defaults(cert, DEFAULT_CERT_ENTRY)
        return certs

    def get_certs(self):
        """返回证书列表的深拷贝"""
        data = self._read_json(self._config_path, DEFAULT_CONFIG)
        certs = copy.deepcopy(data.get('certificates', []))
        return self._normalize_certs(certs)

    def save_certs(self, certs):
        def updater(data):
            data['certificates'] = certs
            return data

        self._update_json(self._config_path, updater, DEFAULT_CONFIG)

    def get_cert(self, order_id):
        """按 order_id 查找证书配置"""
        order_id = int(order_id)
        for cert in self.get_certs():
            if cert.get('order_id') == order_id:
                return cert
        return None

    def get_bound_sites(self):
        """获取所有已绑定站点的集合"""
        bound = set()
        for cert in self.get_certs():
            for s in cert.get('site_name', []):
                if s:
                    bound.add(s)
        return bound

    @staticmethod
    def _collect_bound_sites(certs, exclude_order_id=None):
        """收集已绑定站点集合，可排除指定 order_id"""
        bound = set()
        for c in certs:
            if exclude_order_id is not None and c.get('order_id') == exclude_order_id:
                continue
            for s in c.get('site_name', []):
                if s:
                    bound.add(s)
        return bound

    def add_cert(self, order_id, cert_name, domains, site_name='', renew_mode='',
                 api_url='', api_token='', site_names=None, validation_method='',
                 api=None):
        """添加证书条目，自动排除已被其他证书绑定的站点"""
        order_id = int(order_id)
        requested = site_names if site_names is not None else ([site_name] if site_name else [])

        def updater(data):
            certs = self._normalize_certs(data.get('certificates', []))
            for c in certs:
                if c.get('order_id') == order_id:
                    raise ValueError("订单 %d 已存在" % order_id)
            bound = self._collect_bound_sites(certs)
            available = [s for s in requested if s not in bound]
            entry = copy.deepcopy(DEFAULT_CERT_ENTRY)
            entry['order_id'] = order_id
            entry['cert_name'] = cert_name or ('order-%d' % order_id)
            entry['domains'] = domains if isinstance(domains, list) else [domains]
            entry['site_name'] = available
            entry['renew_mode'] = renew_mode
            entry['validation_method'] = validation_method
            if api:
                entry['api'] = {'url': api.get('url', ''), 'token': api.get('token', '')}
            else:
                entry['api'] = {'url': api_url, 'token': api_token}
            certs.append(entry)
            data['certificates'] = certs
            return data

        result = self._update_json(self._config_path, updater, DEFAULT_CONFIG)
        certs = result.get('certificates', [])
        return certs[-1] if certs else None

    def get_cert_api(self, cert_entry):
        """获取证书的 API 配置"""
        api = cert_entry.get('api', {})
        return api.get('url', ''), api.get('token', '')

    def update_cert(self, order_id, updates):
        """更新指定证书的字段（原子操作）"""
        order_id = int(order_id)

        def updater(data):
            certs = self._normalize_certs(data.get('certificates', []))
            for i, c in enumerate(certs):
                if c.get('order_id') == order_id:
                    if 'site_name' in updates:
                        bound = self._collect_bound_sites(certs, exclude_order_id=order_id)
                        requested = updates['site_name']
                        if isinstance(requested, str):
                            requested = [requested] if requested else []
                        updates['site_name'] = [s for s in requested if s not in bound]
                    for k, v in updates.items():
                        if k in ('metadata', 'api') and isinstance(v, dict):
                            if k not in c or not isinstance(c.get(k), dict):
                                c[k] = copy.deepcopy(DEFAULT_CERT_ENTRY.get(k, {}))
                            c[k].update(v)
                        else:
                            c[k] = v
                    certs[i] = c
                    data['certificates'] = certs
                    return data
            raise ValueError("订单 %d 不存在" % order_id)

        result = self._update_json(self._config_path, updater, DEFAULT_CONFIG)
        for c in result.get('certificates', []):
            if c.get('order_id') == order_id:
                return c
        return None

    def update_order_id(self, old_order_id, new_order_id):
        """更新证书的订单 ID（续费场景，原子操作）"""
        old_order_id = int(old_order_id)
        new_order_id = int(new_order_id)

        def updater(data):
            certs = self._normalize_certs(data.get('certificates', []))
            for c in certs:
                if c.get('order_id') == new_order_id:
                    raise ValueError("订单 %d 已存在" % new_order_id)
            for c in certs:
                if c.get('order_id') == old_order_id:
                    c['order_id'] = new_order_id
                    c['cert_name'] = 'order-%d' % new_order_id
                    data['certificates'] = certs
                    return data
            raise ValueError("订单 %d 不存在" % old_order_id)

        self._update_json(self._config_path, updater, DEFAULT_CONFIG)

    def remove_cert(self, order_id):
        """删除证书条目（原子操作）"""
        order_id = int(order_id)

        def updater(data):
            certs = data.get('certificates', [])
            data['certificates'] = [c for c in certs if c.get('order_id') != order_id]
            return data

        self._update_json(self._config_path, updater, DEFAULT_CONFIG)

    def update_metadata(self, order_id, meta_updates):
        """更新指定证书的 metadata 字段"""
        return self.update_cert(order_id, {'metadata': meta_updates})

    def get_renew_mode(self, cert_entry):
        """获取证书的续签模式，优先证书级别，回退全局配置"""
        mode = cert_entry.get('renew_mode', '')
        if mode:
            return mode
        cfg = self.get_config()
        return cfg.get('schedule', {}).get('renew_mode', 'pull')

    def get_renew_before_days(self, cert_entry):
        """获取提前续签天数"""
        cfg = self.get_config()
        days = cfg.get('schedule', {}).get('renew_before_days', 0)
        if days > 0:
            return days
        return 14  # RENEW_DEFAULT_DAYS
