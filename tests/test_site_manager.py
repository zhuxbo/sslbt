"""站点管理器域名匹配测试"""

import pytest
from lib.site_manager import SiteManager


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
