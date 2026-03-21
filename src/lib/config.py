"""配置管理模块。对标 sslctl pkg/config"""

import os
import json
import copy
import fcntl

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

    def __init__(self, data_dir):
        self._data_dir = data_dir
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
        except (json.JSONDecodeError, OSError):
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

    def get_certs(self):
        """返回证书列表的深拷贝"""
        data = self._read_json(self._certs_path, {'certificates': []})
        certs = copy.deepcopy(data.get('certificates', []))
        for cert in certs:
            site_name = cert.get('site_name', [])
            if isinstance(site_name, str):
                cert['site_name'] = [site_name] if site_name else []
        return certs

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

    def add_cert(self, order_id, cert_name, domains, site_name='', renew_mode='',
                 api_url='', api_token='', site_names=None):
        """添加证书条目，自动排除已被其他证书绑定的站点"""
        order_id = int(order_id)
        certs = self.get_certs()
        for c in certs:
            if c.get('order_id') == order_id:
                raise ValueError("订单 %d 已存在" % order_id)

        # 过滤已绑定站点
        requested = site_names if site_names is not None else ([site_name] if site_name else [])
        bound = set()
        for c in certs:
            for s in c.get('site_name', []):
                if s:
                    bound.add(s)
        available = [s for s in requested if s not in bound]

        entry = copy.deepcopy(DEFAULT_CERT_ENTRY)
        entry['order_id'] = order_id
        entry['cert_name'] = cert_name or ('order-%d' % order_id)
        entry['domains'] = domains if isinstance(domains, list) else [domains]
        entry['site_name'] = available
        entry['renew_mode'] = renew_mode
        entry['api_url'] = api_url
        entry['api_token'] = api_token
        certs.append(entry)
        self.save_certs(certs)
        return entry

    def get_cert_api(self, cert_entry):
        """获取证书的 API 配置"""
        url = cert_entry.get('api_url', '')
        token = cert_entry.get('api_token', '')
        return url, token

    def update_cert(self, order_id, updates):
        """更新指定证书的字段"""
        order_id = int(order_id)
        certs = self.get_certs()
        for i, c in enumerate(certs):
            if c.get('order_id') == order_id:
                for k, v in updates.items():
                    if k == 'metadata' and isinstance(v, dict):
                        if 'metadata' not in c:
                            c['metadata'] = copy.deepcopy(DEFAULT_CERT_ENTRY['metadata'])
                        c['metadata'].update(v)
                    else:
                        c[k] = v
                certs[i] = c
                self.save_certs(certs)
                return c
        raise ValueError("订单 %d 不存在" % order_id)

    def remove_cert(self, order_id):
        """删除证书条目"""
        order_id = int(order_id)
        certs = self.get_certs()
        certs = [c for c in certs if c.get('order_id') != order_id]
        self.save_certs(certs)

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
        mode = self.get_renew_mode(cert_entry)
        days = cfg.get('renew_before_days', 0)
        if days > 0:
            return days
        if mode == 'local':
            return 15  # LOCAL_RENEW_DEFAULT_DAY
        return 13  # PULL_RENEW_DEFAULT_DAY
