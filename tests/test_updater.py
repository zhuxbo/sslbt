"""updater 模块测试"""

import os
import json
import stat
import tempfile
import shutil
import hashlib
import zipfile
from unittest.mock import patch
import pytest


@pytest.fixture
def updater_env(tmp_data_dir):
    """创建 updater 测试环境"""
    plugin_dir = tempfile.mkdtemp(prefix='sslbt_plugin_')
    info = {'name': 'sslbt', 'versions': 'v1.0.0'}
    with open(os.path.join(plugin_dir, 'info.json'), 'w') as f:
        json.dump(info, f)

    from lib.config import ConfigManager
    from lib.logger import Logger
    cfg_mgr = ConfigManager(tmp_data_dir)
    logger = Logger(os.path.join(tmp_data_dir, 'logs'))

    yield {
        'plugin_dir': plugin_dir,
        'data_dir': tmp_data_dir,
        'config': cfg_mgr,
        'logger': logger,
    }
    shutil.rmtree(plugin_dir, ignore_errors=True)


# spec 6.1: 通道做顶层 key，checksums 按文件名索引
SAMPLE_RELEASES = {
    'main': {
        'latest': '2.0.0',
        'versions': [
            {
                'version': '2.0.0',
                'released_at': '2026-03-20',
                'checksums': {'sslbt.zip': 'sha256:abc123'},
            },
        ],
    },
    'dev': {
        'latest': '2.1.0-beta',
        'versions': [
            {
                'version': '2.1.0-beta',
                'released_at': '2026-03-28',
                'checksums': {'sslbt.zip': 'sha256:def456'},
            },
        ],
    },
}


class TestParseReleasesJson:
    def test_has_update_main(self, updater_env):
        from lib.updater import Updater
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])
        result = u._parse_releases(SAMPLE_RELEASES, 'main')
        assert result['has_update'] is True
        assert result['latest_version'] == 'v2.0.0'
        assert result['checksum'] == 'sha256:abc123'

    def test_no_update_same_version(self, updater_env):
        from lib.updater import Updater
        info_path = os.path.join(updater_env['plugin_dir'], 'info.json')
        with open(info_path, 'w') as f:
            json.dump({'name': 'sslbt', 'versions': 'v2.0.0'}, f)
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])
        result = u._parse_releases(SAMPLE_RELEASES, 'main')
        assert result['has_update'] is False

    def test_has_update_dev_channel(self, updater_env):
        from lib.updater import Updater
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])
        result = u._parse_releases(SAMPLE_RELEASES, 'dev')
        assert result['has_update'] is True
        assert result['latest_version'] == 'v2.1.0-beta'
        assert result['checksum'] == 'sha256:def456'

    def test_missing_channel(self, updater_env):
        """不存在的通道返回 no update"""
        from lib.updater import Updater
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])
        result = u._parse_releases(SAMPLE_RELEASES, 'nope')
        assert result['has_update'] is False


class TestVersionCompare:
    def test_newer_version(self):
        from lib.updater import compare_versions
        assert compare_versions('v1.0.0', 'v2.0.0') < 0

    def test_same_version(self):
        from lib.updater import compare_versions
        assert compare_versions('v1.0.0', 'v1.0.0') == 0

    def test_prerelease_lower(self):
        from lib.updater import compare_versions
        assert compare_versions('v1.0.0-beta', 'v1.0.0') < 0

    def test_patch_compare(self):
        from lib.updater import compare_versions
        assert compare_versions('v1.0.1', 'v1.0.0') > 0

    def test_prerelease_numeric_order(self):
        # pre-release 内数字段按数值比较（修复 beta.9 > beta.10 的字典序错误）
        from lib.updater import compare_versions
        assert compare_versions('v0.4.1-beta.9', 'v0.4.1-beta.10') < 0
        assert compare_versions('v0.4.1-beta.10', 'v0.4.1-beta.9') > 0
        assert compare_versions('v1.0.0-beta.2', 'v1.0.0-beta.10') < 0
        assert compare_versions('v1.0.0-rc.10', 'v1.0.0-rc.9') > 0

    def test_prerelease_field_count(self):
        # 共同前缀相等时，字段更多者更高
        from lib.updater import compare_versions
        assert compare_versions('v1.0.0-alpha', 'v1.0.0-alpha.1') < 0
        assert compare_versions('v1.0.0-alpha.1', 'v1.0.0-alpha') > 0

    def test_prerelease_numeric_vs_alpha(self):
        # 数字段低于字母数字段；标签字典序
        from lib.updater import compare_versions
        assert compare_versions('v1.0.0-alpha.1', 'v1.0.0-alpha.beta') < 0
        assert compare_versions('v0.4.1-beta.1', 'v0.4.1-rc.1') < 0

    def test_build_metadata_ignored(self):
        # build metadata 不参与排序
        from lib.updater import compare_versions
        assert compare_versions('v1.0.0+build1', 'v1.0.0+build2') == 0
        assert compare_versions('v1.0.0-beta.1+abc', 'v1.0.0-beta.1+xyz') == 0


class TestSafeExtract:
    def test_skip_data_dir(self, updater_env):
        from lib.updater import Updater
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])

        zip_path = os.path.join(updater_env['data_dir'], 'test.zip')
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr('info.json', '{"versions": "v2.0.0"}')
            zf.writestr('lib/test.py', 'pass')
            zf.writestr('data/config.json', '{"should": "skip"}')

        u._safe_extract(zip_path, updater_env['plugin_dir'])

        assert os.path.exists(os.path.join(updater_env['plugin_dir'], 'info.json'))
        assert os.path.exists(os.path.join(updater_env['plugin_dir'], 'lib', 'test.py'))
        assert not os.path.exists(os.path.join(updater_env['plugin_dir'], 'data', 'config.json'))

    def test_reject_path_traversal(self, updater_env):
        from lib.updater import Updater
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])

        zip_path = os.path.join(updater_env['data_dir'], 'evil.zip')
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr('../../../etc/passwd', 'hacked')

        with pytest.raises(ValueError, match='unsafe'):
            u._safe_extract(zip_path, updater_env['plugin_dir'])

    def test_reject_dotdot_in_middle(self, updater_env):
        from lib.updater import Updater
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])

        zip_path = os.path.join(updater_env['data_dir'], 'evil2.zip')
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr('foo/../../etc/passwd', 'hacked')

        with pytest.raises(ValueError, match='unsafe'):
            u._safe_extract(zip_path, updater_env['plugin_dir'])

    def test_clears_pycache(self, updater_env):
        from lib.updater import Updater
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])

        cache_dir = os.path.join(updater_env['plugin_dir'], 'lib', '__pycache__')
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, 'test.pyc'), 'w') as f:
            f.write('cached')

        zip_path = os.path.join(updater_env['data_dir'], 'test.zip')
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr('info.json', '{"versions": "v2.0.0"}')

        u._safe_extract(zip_path, updater_env['plugin_dir'])
        assert not os.path.exists(cache_dir)


class TestVerifyChecksum:
    def test_checksum_pass(self, updater_env):
        from lib.updater import Updater
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])

        test_file = os.path.join(updater_env['data_dir'], 'test.bin')
        with open(test_file, 'wb') as f:
            f.write(b'hello')

        expected = 'sha256:' + hashlib.sha256(b'hello').hexdigest()
        assert u._verify_checksum(test_file, expected) is True

    def test_checksum_fail(self, updater_env):
        from lib.updater import Updater
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])

        test_file = os.path.join(updater_env['data_dir'], 'test.bin')
        with open(test_file, 'wb') as f:
            f.write(b'hello')

        assert u._verify_checksum(test_file, 'sha256:wrong') is False

    def test_no_checksum_rejects(self, updater_env):
        """无 checksum 时返回 False 并记录错误"""
        from lib.updater import Updater
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])

        test_file = os.path.join(updater_env['data_dir'], 'test.bin')
        with open(test_file, 'wb') as f:
            f.write(b'hello')

        assert u._verify_checksum(test_file, '') is False
        assert u._verify_checksum(test_file, None) is False
        content = updater_env['logger'].get_logs()
        assert '校验和' in content


class TestHTTPSEnforcement:
    """spec 10.5: 升级下载必须 HTTPS"""

    def test_https_url_passes(self):
        from lib.updater import _enforce_https
        _enforce_https('https://release.example.com/releases.json')

    def test_http_localhost_passes(self):
        from lib.updater import _enforce_https
        _enforce_https('http://localhost/releases.json')
        _enforce_https('http://127.0.0.1/releases.json')
        _enforce_https('http://[::1]/releases.json')

    def test_http_remote_rejected(self):
        from lib.updater import _enforce_https
        with pytest.raises(ValueError, match='HTTPS'):
            _enforce_https('http://release.example.com/releases.json')

    def test_non_http_rejected(self):
        from lib.updater import _enforce_https
        with pytest.raises(ValueError, match='HTTPS'):
            _enforce_https('ftp://release.example.com/file.zip')


class TestChannelWhitelist:
    """spec 10.5: 通道白名单"""

    def test_main_dev_allowed(self):
        from lib.updater import _validate_channel
        _validate_channel('main')
        _validate_channel('dev')

    def test_invalid_channel_rejected(self):
        from lib.updater import _validate_channel
        with pytest.raises(ValueError, match='无效'):
            _validate_channel('../../etc')

    def test_empty_channel_rejected(self):
        from lib.updater import _validate_channel
        with pytest.raises(ValueError, match='无效'):
            _validate_channel('')

    def test_check_update_validates_channel(self, updater_env):
        from lib.updater import Updater
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])
        cfg = updater_env['config'].get_config()
        cfg['release_url'] = 'https://release.example.com'
        cfg['upgrade_channel'] = '../../etc'
        updater_env['config'].save_config(cfg)
        with pytest.raises(ValueError, match='无效'):
            u.check_update()

    def test_do_update_validates_channel_param(self, updater_env):
        from lib.updater import Updater
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])
        with pytest.raises(ValueError, match='无效'):
            u.do_update('1.0.0', release_url='https://example.com', channel='../evil')


class TestSSRFProtection:
    """spec 10.1: SSRF/DNS Rebinding 防护"""

    def test_opener_has_safe_handlers(self, updater_env):
        from lib.updater import Updater
        from lib.api_client import _SafeHTTPHandler, _SafeHTTPSHandler
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])
        handler_types = [type(h) for h in u._opener.handlers]
        assert _SafeHTTPHandler in handler_types
        assert _SafeHTTPSHandler in handler_types

    @patch('lib.updater.check_ssrf', return_value='禁止访问内网地址: 10.0.0.1 (evil.com)')
    def test_fetch_releases_rejects_internal_ip(self, mock_ssrf, updater_env):
        from lib.updater import Updater
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])
        with pytest.raises(ValueError, match='不安全'):
            u._fetch_releases('https://evil.com')

    @patch('lib.updater.check_ssrf', return_value='禁止访问内网地址: 192.168.1.1')
    def test_do_update_rejects_internal_ip(self, mock_ssrf, updater_env):
        from lib.updater import Updater
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])
        with pytest.raises(ValueError, match='不安全'):
            u.do_update('2.0.0', release_url='https://evil.com', channel='main')


class TestSafeExtractSecurity:
    """spec 10.2: 符号链接防护 + 文件权限"""

    def test_reject_symlink_in_zip(self, updater_env):
        from lib.updater import Updater
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])

        zip_path = os.path.join(updater_env['data_dir'], 'symlink.zip')
        with zipfile.ZipFile(zip_path, 'w') as zf:
            # 创建符号链接条目：external_attr 高 4 位 = 0xA
            info = zipfile.ZipInfo('evil_link')
            info.external_attr = 0xA0000000 | 0o777 << 16
            zf.writestr(info, '/etc/passwd')

        with pytest.raises(ValueError, match='符号链接'):
            u._safe_extract(zip_path, updater_env['plugin_dir'])

    def test_extracted_files_have_0600(self, updater_env):
        from lib.updater import Updater
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])

        zip_path = os.path.join(updater_env['data_dir'], 'perm.zip')
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr('test_file.py', 'pass')

        u._safe_extract(zip_path, updater_env['plugin_dir'])
        fpath = os.path.join(updater_env['plugin_dir'], 'test_file.py')
        mode = stat.S_IMODE(os.stat(fpath).st_mode)
        assert mode == 0o600

    def test_extracted_dirs_have_0700(self, updater_env):
        from lib.updater import Updater
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])

        zip_path = os.path.join(updater_env['data_dir'], 'perm.zip')
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.writestr('subdir/', '')
            zf.writestr('subdir/file.py', 'pass')

        u._safe_extract(zip_path, updater_env['plugin_dir'])
        dpath = os.path.join(updater_env['plugin_dir'], 'subdir')
        mode = stat.S_IMODE(os.stat(dpath).st_mode)
        assert mode == 0o700
