"""宝塔站点管理模块"""

import os
import sqlite3


# 新版宝塔将 sites/domain 表迁移到 data/db/site.db
DB_PATH_NEW = '/www/server/panel/data/db/site.db'
DB_PATH_OLD = '/www/server/panel/data/default.db'


class SiteManager:
    """通过宝塔数据库和配置获取站点信息"""

    def __init__(self, logger=None):
        self._logger = logger

    @staticmethod
    def _get_db_path():
        """获取站点数据库路径，优先新版分片路径"""
        if os.path.exists(DB_PATH_NEW):
            return DB_PATH_NEW
        return DB_PATH_OLD

    def get_sites(self):
        """获取所有站点列表，返回 [{name, path, status, domains, ssl}]"""
        try:
            db_path = self._get_db_path()
            if not os.path.exists(db_path):
                if self._logger:
                    self._logger.error("宝塔数据库不存在: %s", db_path)
                return []

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute('SELECT id, name, path, status FROM sites ORDER BY id DESC')
            rows = cursor.fetchall()

            # 获取域名绑定
            cursor.execute('SELECT pid, name FROM domain')
            domain_rows = cursor.fetchall()
            conn.close()

            # 按站点 ID 分组域名
            domain_map = {}
            for d in domain_rows:
                pid = d['pid']
                if pid not in domain_map:
                    domain_map[pid] = []
                domain_map[pid].append(d['name'])

            sites = []
            for row in rows:
                site_id = row['id']
                domains = domain_map.get(site_id, [])
                if not domains and row['name']:
                    domains = [row['name']]

                sites.append({
                    'name': row['name'] or '',
                    'path': row['path'] or '',
                    'status': '运行中' if str(row['status']) == '1' else '已停止',
                    'domains': domains,
                    'ssl': self._check_ssl(row['name']),
                })
            return sites
        except Exception as e:
            if self._logger:
                self._logger.error("获取站点列表失败: %s", str(e))
            return []

    def get_site(self, site_name):
        """获取指定站点信息"""
        sites = self.get_sites()
        for s in sites:
            if s['name'] == site_name:
                return s
        return None

    def get_site_domains(self, site_name):
        """获取站点绑定的域名列表"""
        site = self.get_site(site_name)
        if site:
            return site.get('domains', [])
        return []

    def _check_ssl(self, site_name):
        """检查站点是否已启用 SSL"""
        if not site_name:
            return False
        # 检查 nginx SSL 证书文件
        cert_paths = [
            '/www/server/panel/vhost/cert/%s/fullchain.pem' % site_name,
            '/www/server/panel/vhost/ssl/%s/fullchain.pem' % site_name,
        ]
        for p in cert_paths:
            if os.path.exists(p):
                return True
        return False

    def detect_server_type(self):
        """检测宝塔安装的 Web 服务器类型"""
        if os.path.exists('/www/server/nginx/sbin/nginx'):
            return 'nginx'
        if os.path.exists('/www/server/apache/bin/httpd'):
            return 'apache'
        return 'nginx'

    @staticmethod
    def _domain_covered(domain, cert_domains):
        """检查 domain 是否被 cert_domains 中的某个覆盖

        - Exact match: domain == cert_domain (大小写不敏感)
        - Wildcard: *.example.com matches a.example.com but NOT example.com, NOT a.b.example.com
        """
        domain_lower = domain.lower()
        for cert_domain in cert_domains:
            cd = cert_domain.lower()
            if cd == domain_lower:
                return True
            if cd.startswith('*.'):
                # 通配符匹配：*.example.com 匹配 a.example.com
                # 但不匹配 example.com（裸域名）和 a.b.example.com（多级子域名）
                wildcard_base = cd[2:]  # example.com
                if domain_lower.endswith('.' + wildcard_base):
                    # 确保只有一级子域名：domain 去掉 .example.com 后不含点号
                    prefix = domain_lower[:-len(wildcard_base) - 1]
                    if '.' not in prefix and prefix:
                        return True
        return False

    @staticmethod
    def match_domains(cert_domains, site_domains):
        """计算证书域名与站点域名的匹配关系

        Returns: dict {'type': 'full'|'partial', 'unmatched': [...]} or None (no match)
        """
        unmatched = []
        matched_count = 0
        for domain in site_domains:
            if SiteManager._domain_covered(domain, cert_domains):
                matched_count += 1
            else:
                unmatched.append(domain)

        if matched_count == 0:
            return None

        if not unmatched:
            return {'type': 'full', 'unmatched': []}
        return {'type': 'partial', 'unmatched': unmatched}

    @staticmethod
    def match_sites_for_cert(cert_domains, sites):
        """为证书匹配所有站点

        Returns: [{'site_name': str, 'match_type': 'full'|'partial', 'unmatched': [...]}]
        """
        results = []
        for site in sites:
            site_domains = site.get('domains', [])
            match = SiteManager.match_domains(cert_domains, site_domains)
            if match:
                results.append({
                    'site_name': site['name'],
                    'match_type': match['type'],
                    'unmatched': match['unmatched'],
                })
        return results
