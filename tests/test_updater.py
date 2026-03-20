"""updater 模块测试"""

import os
import json
import tempfile
import shutil
import hashlib
import zipfile
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


SAMPLE_RELEASES = {
    'latest_main': 'v2.0.0',
    'latest_dev': 'v2.1.0-beta',
    'channels': {
        'main': {
            'latest': 'v2.0.0',
            'versions': [{
                'version': 'v2.0.0',
                'date': '2026-03-18',
                'notes': '新功能更新',
                'path': 'main/v2.0.0',
            }],
        },
        'dev': {
            'latest': 'v2.1.0-beta',
            'versions': [{
                'version': 'v2.1.0-beta',
                'date': '2026-03-18',
                'notes': '测试版',
                'path': 'dev/v2.1.0-beta',
            }],
        },
    },
    'versions': {
        'v2.0.0': {'checksums': {'sslbt.zip': 'sha256:abc123'}},
        'v2.1.0-beta': {'checksums': {'sslbt.zip': 'sha256:def456'}},
    },
}


class TestParseReleasesJson:
    def test_has_update_main(self, updater_env):
        from lib.updater import Updater
        u = Updater(updater_env['plugin_dir'], updater_env['config'], updater_env['logger'])
        result = u._parse_releases(SAMPLE_RELEASES, 'main')
        assert result['has_update'] is True
        assert result['latest_version'] == 'v2.0.0'
        assert result['notes'] == '新功能更新'

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
