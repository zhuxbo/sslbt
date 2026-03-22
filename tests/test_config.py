"""配置模块测试"""

import os
import json
import copy
import pytest
from lib.config import ConfigManager, DEFAULT_CONFIG, DEFAULT_CERT_ENTRY


class TestConfigManager:
    def test_get_default_config(self, config_manager):
        cfg = config_manager.get_config()
        assert 'api_url' not in cfg
        assert 'api_token' not in cfg
        assert cfg['check_interval_hours'] == 6
        assert cfg['renew_before_days'] == 13
        assert cfg['renew_mode'] == 'pull'

    def test_save_and_get_config(self, config_manager):
        config_manager.save_config({
            'check_interval_hours': 12,
            'renew_before_days': 15,
            'renew_mode': 'local',
        })
        cfg = config_manager.get_config()
        assert cfg['check_interval_hours'] == 12
        assert cfg['renew_mode'] == 'local'

    def test_config_deep_copy(self, config_manager):
        """确保返回深拷贝"""
        cfg1 = config_manager.get_config()
        cfg1['check_interval_hours'] = 999
        cfg2 = config_manager.get_config()
        assert cfg2['check_interval_hours'] == 6

    def test_config_file_permissions(self, config_manager):
        config_manager.save_config({**DEFAULT_CONFIG, 'check_interval_hours': 12})
        path = config_manager._config_path
        mode = oct(os.stat(path).st_mode)[-3:]
        assert mode == '600'

    def test_refuse_symlink(self, config_manager, tmp_data_dir):
        real_file = os.path.join(tmp_data_dir, 'real.json')
        with open(real_file, 'w') as f:
            json.dump({}, f)
        link_path = config_manager._config_path
        if os.path.exists(link_path):
            os.remove(link_path)
        os.symlink(real_file, link_path)
        with pytest.raises(OSError, match='symlink'):
            config_manager.save_config({'test': True})

    def test_get_empty_certs(self, config_manager):
        certs = config_manager.get_certs()
        assert certs == []

    def test_add_cert(self, config_manager):
        entry = config_manager.add_cert(
            order_id=12345,
            cert_name='test-cert',
            domains=['example.com', '*.example.com'],
            site_name='example.com',
        )
        assert entry['order_id'] == 12345
        assert entry['cert_name'] == 'test-cert'
        assert entry['domains'] == ['example.com', '*.example.com']
        assert entry['site_name'] == ['example.com']
        assert entry['enabled'] is True

    def test_add_duplicate_cert(self, config_manager):
        config_manager.add_cert(12345, 'test', ['a.com'])
        with pytest.raises(ValueError, match='已存在'):
            config_manager.add_cert(12345, 'test2', ['b.com'])

    def test_update_cert(self, config_manager):
        config_manager.add_cert(12345, 'test', ['a.com'])
        updated = config_manager.update_cert(12345, {'site_name': ['new-site']})
        assert updated['site_name'] == ['new-site']

    def test_update_metadata(self, config_manager):
        config_manager.add_cert(12345, 'test', ['a.com'])
        updated = config_manager.update_metadata(12345, {
            'last_deploy_at': '2026-01-01T00:00:00Z',
            'cert_serial': 'ABC123',
        })
        assert updated['metadata']['last_deploy_at'] == '2026-01-01T00:00:00Z'
        assert updated['metadata']['cert_serial'] == 'ABC123'

    def test_remove_cert(self, config_manager):
        config_manager.add_cert(12345, 'test', ['a.com'])
        config_manager.remove_cert(12345)
        assert config_manager.get_cert(12345) is None

    def test_get_renew_mode(self, config_manager):
        config_manager.add_cert(12345, 'test', ['a.com'], renew_mode='local')
        cert = config_manager.get_cert(12345)
        assert config_manager.get_renew_mode(cert) == 'local'

    def test_get_renew_mode_fallback(self, config_manager):
        config_manager.add_cert(12345, 'test', ['a.com'])
        cert = config_manager.get_cert(12345)
        assert config_manager.get_renew_mode(cert) == 'pull'

    def test_get_renew_before_days_pull_default(self, config_manager):
        config_manager.add_cert(12345, 'test', ['a.com'])
        cert = config_manager.get_cert(12345)
        days = config_manager.get_renew_before_days(cert)
        assert days == 13  # 全局默认 renew_before_days=13

    def test_get_renew_before_days_fallback(self, config_manager):
        """全局 renew_before_days=0 时，统一回退到 13"""
        config_manager.save_config({
            **config_manager.get_config(),
            'renew_before_days': 0,
        })
        config_manager.add_cert(12345, 'test', ['a.com'], renew_mode='local')
        cert = config_manager.get_cert(12345)
        days = config_manager.get_renew_before_days(cert)
        assert days == 13  # RENEW_DEFAULT_DAYS

    def test_default_config_has_release_url(self, config_manager):
        cfg = config_manager.get_config()
        assert 'release_url' in cfg
        assert cfg['release_url'] == ''

    def test_default_config_has_update_channel(self, config_manager):
        cfg = config_manager.get_config()
        assert 'update_channel' in cfg
        assert cfg['update_channel'] == 'main'

    def test_site_name_migration_string_to_list(self, config_manager):
        """旧格式 site_name 字符串自动迁移为列表"""
        # 直接写入旧格式数据（site_name 为字符串）
        old_entry = copy.deepcopy(DEFAULT_CERT_ENTRY)
        old_entry['order_id'] = 99999
        old_entry['cert_name'] = 'legacy-cert'
        old_entry['domains'] = ['legacy.com']
        old_entry['site_name'] = 'legacy.com'  # 旧格式：字符串
        config_manager.save_certs([old_entry])
        # 通过 get_certs 读取，应自动迁移为列表
        certs = config_manager.get_certs()
        assert len(certs) == 1
        assert certs[0]['site_name'] == ['legacy.com']

    def test_site_name_migration_empty_string_to_list(self, config_manager):
        """旧格式空字符串 site_name 迁移为空列表"""
        old_entry = copy.deepcopy(DEFAULT_CERT_ENTRY)
        old_entry['order_id'] = 99998
        old_entry['cert_name'] = 'empty-site'
        old_entry['domains'] = ['empty.com']
        old_entry['site_name'] = ''  # 旧格式：空字符串
        config_manager.save_certs([old_entry])
        certs = config_manager.get_certs()
        assert len(certs) == 1
        assert certs[0]['site_name'] == []

    def test_site_name_already_list(self, config_manager):
        """已经是列表格式的 site_name 不做转换"""
        entry = copy.deepcopy(DEFAULT_CERT_ENTRY)
        entry['order_id'] = 99997
        entry['cert_name'] = 'list-cert'
        entry['domains'] = ['list.com']
        entry['site_name'] = ['site-a.com', 'site-b.com']
        config_manager.save_certs([entry])
        certs = config_manager.get_certs()
        assert len(certs) == 1
        assert certs[0]['site_name'] == ['site-a.com', 'site-b.com']

    def test_add_cert_site_names_list(self, config_manager):
        """add_cert 接受 site_names 列表参数"""
        entry = config_manager.add_cert(
            order_id=88888,
            cert_name='multi-site',
            domains=['multi.com'],
            site_names=['site-a.com', 'site-b.com'],
        )
        assert entry['site_name'] == ['site-a.com', 'site-b.com']
        # 从存储中重新读取验证
        cert = config_manager.get_cert(88888)
        assert cert['site_name'] == ['site-a.com', 'site-b.com']

    def test_add_cert_site_names_default_empty(self, config_manager):
        """add_cert 不传 site_names 时默认为空列表"""
        entry = config_manager.add_cert(
            order_id=88887,
            cert_name='no-site',
            domains=['nosite.com'],
        )
        assert entry['site_name'] == []
        cert = config_manager.get_cert(88887)
        assert cert['site_name'] == []

    def test_corrupted_config_returns_default(self, config_manager):
        """损坏的 JSON 返回默认值"""
        with open(config_manager._config_path, 'w') as f:
            f.write('{invalid json!!!')
        cfg = config_manager.get_config()
        assert cfg['renew_before_days'] == 13

    def test_corrupted_config_creates_backup(self, config_manager):
        """损坏的 JSON 创建 .bak 备份"""
        with open(config_manager._config_path, 'w') as f:
            f.write('{bad}')
        config_manager.get_config()
        assert os.path.isfile(config_manager._config_path + '.bak')

    def test_corrupted_config_logs_error(self, tmp_data_dir):
        """损坏的 JSON 记录 error 日志"""
        from unittest.mock import MagicMock
        logger = MagicMock()
        config_manager = ConfigManager(tmp_data_dir, logger=logger)
        with open(config_manager._config_path, 'w') as f:
            f.write('{broken')
        config_manager.get_config()
        logger.error.assert_called_once()
        assert 'JSON' in str(logger.error.call_args)

    def test_update_cert_filters_bound_sites(self, config_manager):
        """update_cert 时排除已被其他证书绑定的站点"""
        config_manager.add_cert(11111, 'cert1', ['a.com'], site_names=['site-a.com'])
        config_manager.add_cert(22222, 'cert2', ['b.com'], site_names=['site-b.com'])
        # 尝试把 site-a.com（已被 cert1 绑定）分配给 cert2
        updated = config_manager.update_cert(22222, {'site_name': ['site-a.com', 'site-c.com']})
        assert 'site-a.com' not in updated['site_name']
        assert 'site-c.com' in updated['site_name']

    def test_concurrent_update_metadata(self, tmp_data_dir):
        """两个线程同时 update_metadata 不丢数据"""
        import threading
        config = ConfigManager(tmp_data_dir)
        config.add_cert(10001, 'test', ['a.com'])

        errors = []

        def update_field(field, value):
            try:
                config.update_metadata(10001, {field: value})
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=update_field, args=('last_deploy_at', '2026-01-01T00:00:00Z'))
        t2 = threading.Thread(target=update_field, args=('cert_serial', 'ABC123'))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors
        cert = config.get_cert(10001)
        # 两个字段都应该被更新（原子操作保证不互相覆盖）
        assert cert['metadata']['last_deploy_at'] == '2026-01-01T00:00:00Z'
        assert cert['metadata']['cert_serial'] == 'ABC123'

    def test_config_manager_with_logger(self, tmp_data_dir):
        """ConfigManager 接受 logger 参数"""
        from unittest.mock import MagicMock
        logger = MagicMock()
        cm = ConfigManager(tmp_data_dir, logger=logger)
        cfg = cm.get_config()
        assert cfg['renew_before_days'] == 13
