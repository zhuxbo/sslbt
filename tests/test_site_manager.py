"""站点管理器域名匹配测试"""

import sqlite3
import pytest
from unittest.mock import patch

from lib.site_manager import SiteManager, SiteQueryError


class TestGetSitesFailureSemantics:
    """get_sites 区分「查询失败」与「确认零站点」（P0）：失败抛 SiteQueryError，绝不与空列表同形"""

    def test_db_missing_raises(self, tmp_path):
        mgr = SiteManager()
        with patch.object(SiteManager, '_get_db_path', return_value=str(tmp_path / 'no-such.db')):
            with pytest.raises(SiteQueryError):
                mgr.get_sites()

    def test_schema_drift_raises(self, tmp_path):
        """表结构漂移（缺 sites 表 → sqlite3.OperationalError）转为 SiteQueryError"""
        db_path = str(tmp_path / 'drifted.db')
        conn = sqlite3.connect(db_path)
        conn.execute('CREATE TABLE unrelated (id INTEGER)')
        conn.commit()
        conn.close()
        mgr = SiteManager()
        with patch.object(SiteManager, '_get_db_path', return_value=db_path):
            with pytest.raises(SiteQueryError):
                mgr.get_sites()

    def test_get_site_propagates_failure(self, tmp_path):
        """get_site 查询失败冒泡 SiteQueryError，而非返回 None（None 仅代表站点不存在）"""
        mgr = SiteManager()
        with patch.object(SiteManager, '_get_db_path', return_value=str(tmp_path / 'no-such.db')):
            with pytest.raises(SiteQueryError):
                mgr.get_site('a.com')

    @staticmethod
    def _make_site_db(tmp_path, rows=()):
        db_path = str(tmp_path / 'site.db')
        conn = sqlite3.connect(db_path)
        conn.execute('CREATE TABLE sites (id INTEGER PRIMARY KEY, name TEXT, path TEXT, status TEXT)')
        conn.execute('CREATE TABLE domain (id INTEGER PRIMARY KEY, pid INTEGER, name TEXT)')
        for r in rows:
            conn.execute('INSERT INTO sites (id, name, path, status) VALUES (?, ?, ?, ?)', r)
        conn.commit()
        conn.close()
        return db_path

    def test_empty_db_returns_empty_list(self, tmp_path):
        """表结构正常但零站点 → 返回空列表（与查询失败区分）"""
        db_path = self._make_site_db(tmp_path)
        mgr = SiteManager()
        with patch.object(SiteManager, '_get_db_path', return_value=db_path):
            assert mgr.get_sites() == []

    def test_normal_db_returns_sites(self, tmp_path):
        db_path = self._make_site_db(tmp_path, rows=[(1, 'a.com', '/www/wwwroot/a.com', '1')])
        mgr = SiteManager()
        with patch.object(SiteManager, '_get_db_path', return_value=db_path):
            sites = mgr.get_sites()
        assert len(sites) == 1
        assert sites[0]['name'] == 'a.com'


class TestDomainMatching:
    def test_full_match(self):
        """证书域名完全覆盖站点域名"""
        result = SiteManager.match_domains(
            ['*.example.com', 'example.com'],
            ['blog.example.com'],
        )
        assert result is not None
        assert result['type'] == 'full'
        assert result['unmatched'] == []

    def test_partial_match(self):
        """证书域名部分覆盖站点域名"""
        result = SiteManager.match_domains(
            ['*.example.com', 'example.com'],
            ['blog.example.com', 'other.com'],
        )
        assert result is not None
        assert result['type'] == 'partial'
        assert result['unmatched'] == ['other.com']

    def test_no_match(self):
        """证书域名与站点域名完全不匹配"""
        result = SiteManager.match_domains(
            ['*.example.com'],
            ['other.com', 'test.org'],
        )
        assert result is None

    def test_wildcard_match(self):
        """通配符匹配多个子域名"""
        result = SiteManager.match_domains(
            ['*.example.com'],
            ['a.example.com', 'b.example.com'],
        )
        assert result is not None
        assert result['type'] == 'full'

    def test_wildcard_not_match_bare(self):
        """通配符不匹配裸域名"""
        result = SiteManager.match_domains(
            ['*.example.com'],
            ['example.com'],
        )
        assert result is None

    def test_exact_match(self):
        """精确匹配"""
        result = SiteManager.match_domains(
            ['example.com'],
            ['example.com'],
        )
        assert result is not None
        assert result['type'] == 'full'


class TestMatchSitesForCert:
    def test_match_sites_for_cert(self):
        """为证书匹配所有站点"""
        sites = [
            {'name': 'site1', 'domains': ['blog.example.com', 'example.com']},
            {'name': 'site2', 'domains': ['api.example.com', 'other.com']},
            {'name': 'site3', 'domains': ['test.org', 'demo.net']},
        ]
        cert_domains = ['*.example.com', 'example.com']

        results = SiteManager.match_sites_for_cert(cert_domains, sites)

        assert len(results) == 2

        # site1: full match
        site1_result = next(r for r in results if r['site_name'] == 'site1')
        assert site1_result['match_type'] == 'full'
        assert site1_result['unmatched'] == []

        # site2: partial match
        site2_result = next(r for r in results if r['site_name'] == 'site2')
        assert site2_result['match_type'] == 'partial'
        assert site2_result['unmatched'] == ['other.com']

        # site3: not in results
        site3_names = [r['site_name'] for r in results]
        assert 'site3' not in site3_names
