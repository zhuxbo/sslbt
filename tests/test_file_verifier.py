"""文件验证模块测试"""

import os
import pytest
from unittest.mock import MagicMock

from lib.file_verifier import FileVerifier


@pytest.fixture
def mock_site_mgr():
    mgr = MagicMock()
    mgr.get_site.side_effect = lambda name: {
        'test.example.com': {
            'name': 'test.example.com',
            'path': '',  # 动态设置
        },
        'demo.example.com': {
            'name': 'demo.example.com',
            'path': '',
        },
    }.get(name)
    return mgr


@pytest.fixture
def verifier(mock_site_mgr):
    return FileVerifier(mock_site_mgr)


class TestIsSafePath:
    def test_valid_path(self):
        assert FileVerifier._is_safe_path('.well-known/acme-challenge/token123')

    def test_reject_dotdot(self):
        assert not FileVerifier._is_safe_path('.well-known/../etc/passwd')

    def test_reject_non_wellknown(self):
        assert not FileVerifier._is_safe_path('etc/passwd')

    def test_reject_absolute(self):
        assert not FileVerifier._is_safe_path('/etc/passwd')

    def test_reject_dotdot_prefix(self):
        assert not FileVerifier._is_safe_path('../.well-known/acme-challenge/token')


class TestPlaceFile:
    def test_place_single_site(self, tmp_path, mock_site_mgr):
        """正常放置到单个站点"""
        site_root = str(tmp_path / 'wwwroot' / 'test.example.com')
        os.makedirs(site_root, exist_ok=True)
        mock_site_mgr.get_site.side_effect = None
        mock_site_mgr.get_site.return_value = {'name': 'test.example.com', 'path': site_root}

        verifier = FileVerifier(mock_site_mgr)
        file_info = {'path': '.well-known/acme-challenge/token123', 'content': 'verify-content'}
        placed = verifier.place_file(file_info, ['test.example.com'])

        assert len(placed) == 1
        assert os.path.isfile(placed[0])
        with open(placed[0]) as f:
            assert f.read() == 'verify-content'

    def test_place_multiple_sites(self, tmp_path, mock_site_mgr):
        """放置到多个站点"""
        roots = {}
        for name in ['test.example.com', 'demo.example.com']:
            root = str(tmp_path / 'wwwroot' / name)
            os.makedirs(root, exist_ok=True)
            roots[name] = root

        mock_site_mgr.get_site.side_effect = lambda n: {'name': n, 'path': roots[n]} if n in roots else None

        verifier = FileVerifier(mock_site_mgr)
        file_info = {'path': '.well-known/acme-challenge/token', 'content': 'content'}
        placed = verifier.place_file(file_info, ['test.example.com', 'demo.example.com'])

        assert len(placed) == 2
        for p in placed:
            assert os.path.isfile(p)

    def test_reject_unsafe_path(self, mock_site_mgr):
        """拒绝不安全路径"""
        verifier = FileVerifier(mock_site_mgr)
        file_info = {'path': '.well-known/../etc/passwd', 'content': 'hack'}
        placed = verifier.place_file(file_info, ['test.example.com'])
        assert placed == []

    def test_skip_missing_site(self, mock_site_mgr):
        """站点不存在时跳过"""
        mock_site_mgr.get_site.return_value = None
        verifier = FileVerifier(mock_site_mgr)
        file_info = {'path': '.well-known/acme-challenge/token', 'content': 'c'}
        placed = verifier.place_file(file_info, ['nonexist.com'])
        assert placed == []

    def test_empty_file_info(self, mock_site_mgr):
        """file_info 为空"""
        verifier = FileVerifier(mock_site_mgr)
        assert verifier.place_file(None, ['test.example.com']) == []
        assert verifier.place_file({}, ['test.example.com']) == []
        assert verifier.place_file({'path': '', 'content': ''}, ['test.example.com']) == []

    def test_place_file_write_error(self, tmp_path, mock_site_mgr):
        """写入失败时记录日志并跳过"""
        # 创建只读目录
        readonly_dir = str(tmp_path / 'readonly')
        os.makedirs(readonly_dir)
        os.chmod(readonly_dir, 0o444)

        mock_site_mgr.get_site.side_effect = None
        mock_site_mgr.get_site.return_value = {'name': 'test.example.com', 'path': readonly_dir}

        logger = MagicMock()
        verifier = FileVerifier(mock_site_mgr, logger)
        file_info = {'path': '.well-known/acme-challenge/token', 'content': 'c'}
        placed = verifier.place_file(file_info, ['test.example.com'])

        assert placed == []
        logger.error.assert_called()
        # 恢复权限以便 tmp_path 清理
        os.chmod(readonly_dir, 0o755)


class TestCleanupFiles:
    def test_cleanup_files(self, tmp_path):
        """正常清理文件和空目录"""
        challenge_dir = tmp_path / 'wwwroot' / '.well-known' / 'acme-challenge'
        challenge_dir.mkdir(parents=True)
        token_file = challenge_dir / 'token123'
        token_file.write_text('content')

        verifier = FileVerifier(MagicMock())
        verifier.cleanup_files([str(token_file)])

        assert not token_file.exists()
        assert not challenge_dir.exists()  # 空目录被清理

    def test_cleanup_nonexist_file(self, tmp_path):
        """文件不存在时不报错"""
        verifier = FileVerifier(MagicMock())
        verifier.cleanup_files([str(tmp_path / 'nonexist')])  # 不抛异常

    def test_cleanup_empty_list(self):
        """空列表不报错"""
        verifier = FileVerifier(MagicMock())
        verifier.cleanup_files([])
        verifier.cleanup_files(None)

    def test_cleanup_preserves_nonempty_dir(self, tmp_path):
        """非空目录不被清理"""
        challenge_dir = tmp_path / 'wwwroot' / '.well-known' / 'acme-challenge'
        challenge_dir.mkdir(parents=True)
        token1 = challenge_dir / 'token1'
        token2 = challenge_dir / 'token2'
        token1.write_text('c1')
        token2.write_text('c2')

        verifier = FileVerifier(MagicMock())
        verifier.cleanup_files([str(token1)])

        assert not token1.exists()
        assert token2.exists()
        assert challenge_dir.exists()  # 目录非空，保留
