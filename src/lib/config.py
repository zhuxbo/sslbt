"""配置管理模块。对标 sslctl pkg/config"""

import os
import json
import copy
import fcntl
import shutil

DEFAULT_CONFIG = {
    'check_interval_hours': 6,
    'renew_before_days': 13,
    'renew_mode': 'pull',
    'release_url': '',
    'update_channel': 'main',
}

DEFAULT_CERT_ENTRY = {
    'order_id': 0,
    'cert_name': '',
    'domains': [],
    'enabled': True,
    'renew_mode': '',
    'validation_method': '',
    'api_url': '',
    'api_token': '',
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
    },
}


class ConfigManager:
    """配置读写管理，文件锁保护"""

    def __init__(self, data_dir, logger=None):
        self._data_dir = data_dir
        self._logger = logger
        self._config_path = os.path.join(data_dir, 'config.json')
        self._certs_path = os.path.join(data_dir, 'certs.json')
        os.makedirs(data_dir, exist_ok=True)

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
            # 备份损坏文件
            try:
                shutil.copy2(path, path + '.bak')
            except OSError:
                pass
            return copy.deepcopy(default)
        except OSError:
            return copy.deepcopy(default)

    def _write_json(self, path, data):
        # 拒绝写入符号链接目标
        if os.path.islink(path):
            raise OSError("refusing to write to symlink: %s" % path)
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
            raise OSError("refusing to write to symlink: %s" % path)
        lock_path = path + '.lock'
        with open(lock_path, 'w') as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                # 读取当前数据
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
                # 写入 tmp 然后 replace
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

    # --- 全局配置 ---

    # 废弃字段，读取时自动清除
    _DEPRECATED_KEYS = {'api_url', 'api_token', 'version'}

    def get_config(self):
        """返回全局配置，自动清除废弃字段"""
        cfg = self._read_json(self._config_path, DEFAULT_CONFIG)
        merged = copy.deepcopy(DEFAULT_CONFIG)
        for k, v in cfg.items():
            if k not in self._DEPRECATED_KEYS:
                merged[k] = v
        return merged

    def save_config(self, cfg):
        # 写入前清除废弃字段
        clean = {k: v for k, v in cfg.items() if k not in self._DEPRECATED_KEYS}
        self._write_json(self._config_path, clean)

    # --- 证书列表 ---

    @staticmethod
    def _normalize_certs(certs):
        """规范化证书列表：site_name 字符串转列表"""
        for cert in certs:
            site_name = cert.get('site_name', [])
            if isinstance(site_name, str):
                cert['site_name'] = [site_name] if site_name else []
        return certs

    def get_certs(self):
        """返回证书列表的深拷贝"""
        data = self._read_json(self._certs_path, {'certificates': []})
        certs = copy.deepcopy(data.get('certificates', []))
        return self._normalize_certs(certs)

    def save_certs(self, certs):
        self._write_json(self._certs_path, {'certificates': certs})

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
                 api_url='', api_token='', site_names=None, validation_method=''):
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
            entry['api_url'] = api_url
            entry['api_token'] = api_token
            certs.append(entry)
            data['certificates'] = certs
            return data

        result = self._update_json(self._certs_path, updater, {'certificates': []})
        # 返回最后添加的条目
        certs = result.get('certificates', [])
        return certs[-1] if certs else None

    def get_cert_api(self, cert_entry):
        """获取证书的 API 配置"""
        url = cert_entry.get('api_url', '')
        token = cert_entry.get('api_token', '')
        return url, token

    def update_cert(self, order_id, updates):
        """更新指定证书的字段（原子操作）"""
        order_id = int(order_id)

        def updater(data):
            certs = self._normalize_certs(data.get('certificates', []))
            for i, c in enumerate(certs):
                if c.get('order_id') == order_id:
                    # 如果更新 site_name，过滤已被其他证书绑定的站点
                    if 'site_name' in updates:
                        bound = self._collect_bound_sites(certs, exclude_order_id=order_id)
                        requested = updates['site_name']
                        if isinstance(requested, str):
                            requested = [requested] if requested else []
                        updates['site_name'] = [s for s in requested if s not in bound]
                    for k, v in updates.items():
                        if k == 'metadata' and isinstance(v, dict):
                            if 'metadata' not in c:
                                c['metadata'] = copy.deepcopy(DEFAULT_CERT_ENTRY['metadata'])
                            c['metadata'].update(v)
                        else:
                            c[k] = v
                    certs[i] = c
                    data['certificates'] = certs
                    return data
            raise ValueError("订单 %d 不存在" % order_id)

        result = self._update_json(self._certs_path, updater, {'certificates': []})
        # 返回更新后的证书条目
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

        self._update_json(self._certs_path, updater, {'certificates': []})

    def remove_cert(self, order_id):
        """删除证书条目（原子操作）"""
        order_id = int(order_id)

        def updater(data):
            certs = data.get('certificates', [])
            data['certificates'] = [c for c in certs if c.get('order_id') != order_id]
            return data

        self._update_json(self._certs_path, updater, {'certificates': []})

    def update_metadata(self, order_id, meta_updates):
        """更新指定证书的 metadata 字段"""
        return self.update_cert(order_id, {'metadata': meta_updates})

    def get_renew_mode(self, cert_entry):
        """获取证书的续签模式，优先证书级别，回退全局配置"""
        mode = cert_entry.get('renew_mode', '')
        if mode:
            return mode
        cfg = self.get_config()
        return cfg.get('renew_mode', 'pull')

    def get_renew_before_days(self, cert_entry):
        """获取提前续签天数"""
        cfg = self.get_config()
        days = cfg.get('renew_before_days', 0)
        if days > 0:
            return days
        return 13  # RENEW_DEFAULT_DAYS
