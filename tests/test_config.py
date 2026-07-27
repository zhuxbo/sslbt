"""配置模块测试"""

import os
import json
import copy
import pytest
from lib.config import (
    ConfigManager, DEFAULT_CONFIG, DEFAULT_CERT_ENTRY, validate_validation_method,
    derive_or_validate_renew_policy, domains_contain_ip,
)


def _write_legacy_config(data_dir, cert_meta, domains=None, renew_mode='',
                         validation_method='', global_mode='pull'):
    """写入含单个 legacy 证书的 config.json（构造后由 ConfigManager 触发迁移）"""
    cfg = {
        'release_url': '', 'upgrade_channel': 'main',
        'schedule': {'renew_mode': global_mode, 'renew_before_days': 14},
        'certificates': [{
            'order_id': 5000, 'cert_name': 'order-5000',
            'domains': domains if domains is not None else ['a.com'], 'enabled': True,
            'renew_mode': renew_mode, 'validation_method': validation_method,
            'api': {'url': 'https://api.example.com', 'token': 'x'},
            'site_name': ['a.com'], 'server_type': 'nginx',
            'metadata': cert_meta,
        }],
    }
    with open(os.path.join(data_dir, 'config.json'), 'w') as f:
        json.dump(cfg, f)


class TestConfigManager:
    def test_get_default_config(self, config_manager):
        cfg = config_manager.get_config()
        assert 'api_url' not in cfg
        assert 'api_token' not in cfg
        assert 'check_interval_hours' not in cfg
        assert cfg['schedule']['renew_before_days'] == 14
        assert cfg['schedule']['renew_mode'] == 'pull'
        assert cfg['upgrade_channel'] == 'main'

    def test_save_and_get_config(self, config_manager):
        config_manager.save_config({
            'release_url': '',
            'upgrade_channel': 'main',
            'schedule': {
                'renew_before_days': 10,
                'renew_mode': 'local',
            },
        })
        cfg = config_manager.get_config()
        assert cfg['schedule']['renew_before_days'] == 10
        assert cfg['schedule']['renew_mode'] == 'local'

    def test_config_deep_copy(self, config_manager):
        """确保返回深拷贝"""
        cfg1 = config_manager.get_config()
        cfg1['schedule']['renew_before_days'] = 999
        cfg2 = config_manager.get_config()
        assert cfg2['schedule']['renew_before_days'] == 14

    def test_config_file_permissions(self, config_manager):
        config_manager.save_config(copy.deepcopy(DEFAULT_CONFIG))
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
        assert days == 14  # 全局默认 schedule.renew_before_days=14

    def test_get_renew_before_days_fallback(self, config_manager):
        """全局 schedule.renew_before_days=0 时，统一回退到 14"""
        cfg = config_manager.get_config()
        cfg['schedule']['renew_before_days'] = 0
        config_manager.save_config(cfg)
        config_manager.add_cert(12345, 'test', ['a.com'], renew_mode='local')
        cert = config_manager.get_cert(12345)
        days = config_manager.get_renew_before_days(cert)
        assert days == 14  # RENEW_DEFAULT_DAYS

    def test_default_config_has_release_url(self, config_manager):
        cfg = config_manager.get_config()
        assert 'release_url' in cfg
        assert cfg['release_url'] == ''

    def test_default_config_has_upgrade_channel(self, config_manager):
        cfg = config_manager.get_config()
        assert 'upgrade_channel' in cfg
        assert cfg['upgrade_channel'] == 'main'
        assert 'update_channel' not in cfg

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
        assert cfg['schedule']['renew_before_days'] == 14

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

    def test_update_order_id(self, config_manager):
        """续费后更新订单 ID"""
        config_manager.add_cert(11111, 'order-11111', ['a.com'])
        config_manager.update_metadata(11111, {'cert_serial': 'ABC', 'last_deploy_at': '2026-01-01'})
        config_manager.update_order_id(11111, 22222)
        assert config_manager.get_cert(11111) is None
        cert = config_manager.get_cert(22222)
        assert cert is not None
        assert cert['cert_name'] == 'order-22222'
        assert cert['domains'] == ['a.com']
        # metadata 保留
        assert cert['metadata']['cert_serial'] == 'ABC'
        assert cert['metadata']['last_deploy_at'] == '2026-01-01'

    def test_update_order_id_conflict(self, config_manager):
        """新订单 ID 已存在时报错"""
        config_manager.add_cert(11111, 'order-11111', ['a.com'])
        config_manager.add_cert(22222, 'order-22222', ['b.com'])
        with pytest.raises(ValueError, match='已存在'):
            config_manager.update_order_id(11111, 22222)
        # 原始配置未变
        assert config_manager.get_cert(11111) is not None

    def test_update_order_id_not_found(self, config_manager):
        """旧订单 ID 不存在时报错"""
        with pytest.raises(ValueError, match='不存在'):
            config_manager.update_order_id(99999, 88888)

    def test_config_manager_with_logger(self, tmp_data_dir):
        """ConfigManager 接受 logger 参数"""
        from unittest.mock import MagicMock
        logger = MagicMock()
        cm = ConfigManager(tmp_data_dir, logger=logger)
        cfg = cm.get_config()
        assert cfg['schedule']['renew_before_days'] == 14

    # ==================== 迁移测试 ====================

    def test_migrate_flat_renew_fields(self, config_manager):
        """旧格式顶层 renew_before_days / renew_mode 迁移到 schedule"""
        import json
        with open(config_manager._config_path, 'w') as f:
            json.dump({
                'renew_before_days': 10,
                'renew_mode': 'local',
                'release_url': '',
                'update_channel': 'main',
            }, f)
        # 重新初始化触发迁移
        cm = ConfigManager(config_manager._data_dir)
        cfg = cm.get_config()
        assert cfg['schedule']['renew_before_days'] == 10
        assert cfg['schedule']['renew_mode'] == 'local'
        assert 'renew_before_days' not in cfg
        assert 'renew_mode' not in cfg

    def test_migrate_update_channel_to_upgrade_channel(self, config_manager):
        """旧格式 update_channel 迁移为 upgrade_channel"""
        import json
        with open(config_manager._config_path, 'w') as f:
            json.dump({
                'release_url': 'https://example.com',
                'update_channel': 'dev',
            }, f)
        cm = ConfigManager(config_manager._data_dir)
        cfg = cm.get_config()
        assert cfg['upgrade_channel'] == 'dev'
        assert 'update_channel' not in cfg

    def test_migrate_removes_check_interval_hours(self, config_manager):
        """旧格式 check_interval_hours 静默移除"""
        import json
        with open(config_manager._config_path, 'w') as f:
            json.dump({
                'check_interval_hours': 6,
                'release_url': '',
            }, f)
        cm = ConfigManager(config_manager._data_dir)
        cfg = cm.get_config()
        assert 'check_interval_hours' not in cfg

    def test_migrate_persists_on_init(self, config_manager):
        """初始化时迁移旧配置并持久化"""
        import json
        with open(config_manager._config_path, 'w') as f:
            json.dump({
                'renew_before_days': 7,
                'renew_mode': 'pull',
                'update_channel': 'main',
            }, f)
        # 重新初始化触发迁移并持久化
        ConfigManager(config_manager._data_dir)
        with open(config_manager._config_path, 'r') as f:
            saved = json.load(f)
        assert 'renew_before_days' not in saved
        assert 'update_channel' not in saved
        assert saved.get('schedule', {}).get('renew_before_days') == 7
        assert saved.get('upgrade_channel') == 'main'

    def test_new_format_config_works(self, config_manager):
        """新格式配置（schedule 嵌套）直接正常工作"""
        config_manager.save_config({
            'release_url': 'https://example.com',
            'upgrade_channel': 'dev',
            'schedule': {
                'renew_mode': 'local',
                'renew_before_days': 7,
            },
        })
        cfg = config_manager.get_config()
        assert cfg['upgrade_channel'] == 'dev'
        assert cfg['schedule']['renew_mode'] == 'local'
        assert cfg['schedule']['renew_before_days'] == 7

    def test_default_renew_before_days_is_14(self, config_manager):
        """默认 renew_before_days 为 14"""
        cfg = config_manager.get_config()
        assert cfg['schedule']['renew_before_days'] == 14


class TestValidateValidationMethod:
    def test_empty_method_always_passes(self):
        assert validate_validation_method(['example.com'], '') == ''
        assert validate_validation_method(['*.example.com'], '') == ''
        assert validate_validation_method(['1.2.3.4'], '') == ''

    def test_normal_domain_allows_both(self):
        assert validate_validation_method(['example.com'], 'file') == ''
        assert validate_validation_method(['example.com'], 'delegation') == ''

    def test_wildcard_allows_delegation(self):
        assert validate_validation_method(['*.example.com'], 'delegation') == ''

    def test_wildcard_rejects_file(self):
        assert validate_validation_method(['*.example.com'], 'file') != ''

    def test_ipv4_allows_file(self):
        assert validate_validation_method(['192.168.1.1'], 'file') == ''

    def test_ipv4_rejects_delegation(self):
        assert validate_validation_method(['192.168.1.1'], 'delegation') != ''

    def test_ipv6_rejects_delegation(self):
        assert validate_validation_method(['::1'], 'delegation') != ''
        assert validate_validation_method(['2001:db8::1'], 'delegation') != ''

    def test_ipv6_allows_file(self):
        assert validate_validation_method(['::1'], 'file') == ''

    def test_mixed_domains_with_ip_rejects_delegation(self):
        """混合域名列表中含 IP 时，delegation 应被拒绝"""
        assert validate_validation_method(['example.com', '1.2.3.4'], 'delegation') != ''

    def test_mixed_domains_with_wildcard_rejects_file(self):
        """混合域名列表中含通配符时，file 应被拒绝"""
        assert validate_validation_method(['example.com', '*.example.com'], 'file') != ''


class TestDomainsContainIp:
    def test_ipv4(self):
        assert domains_contain_ip(['1.2.3.4']) is True

    def test_ipv6(self):
        assert domains_contain_ip(['a.com', '2001:db8::1']) is True

    def test_dns_only(self):
        assert domains_contain_ip(['a.com', '*.a.com']) is False

    def test_empty(self):
        assert domains_contain_ip([]) is False


class TestDeriveOrValidateRenewPolicy:
    """唯一权威的续签策略派生（deploy-spec §5.2）"""

    def test_ip_forces_local_file_over_pull(self):
        assert derive_or_validate_renew_policy(['1.2.3.4'], 'pull', '') == ('local', 'file', '')

    def test_ip_forces_local_file_over_delegation(self):
        assert derive_or_validate_renew_policy(['1.2.3.4'], 'local', 'delegation') == ('local', 'file', '')

    def test_ipv6_forces_local_file(self):
        assert derive_or_validate_renew_policy(['2001:db8::1'], '', '') == ('local', 'file', '')

    def test_mixed_dns_ip_forces_local_file(self):
        """DNS + IP 混合仍视为含 IP，强制 local/file"""
        assert derive_or_validate_renew_policy(['a.com', '1.2.3.4'], 'pull', '') == ('local', 'file', '')

    def test_dns_pull_unchanged(self):
        assert derive_or_validate_renew_policy(['a.com'], 'pull', '') == ('pull', '', '')

    def test_dns_local_file_valid(self):
        assert derive_or_validate_renew_policy(['a.com'], 'local', 'file') == ('local', 'file', '')

    def test_wildcard_file_returns_error(self):
        mode, vm, err = derive_or_validate_renew_policy(['*.a.com'], 'local', 'file')
        assert err != ''

    def test_wildcard_delegation_valid(self):
        assert derive_or_validate_renew_policy(['*.a.com'], 'local', 'delegation') == ('local', 'delegation', '')


class TestSemanticMigration:
    """计算型语义迁移：pending 归一、legacy 触顶、policy 阻断、部署计数从零（deploy-spec §3.4/§5.2）"""

    def test_deploy_attempt_count_default_zero(self, tmp_data_dir):
        """新字段 deploy_attempt_count 由默认值补 0（不从旧混合计数推断）"""
        _write_legacy_config(tmp_data_dir, {'cert_expires_at': '', 'issue_retry_count': 7})
        cm = ConfigManager(tmp_data_dir)
        assert cm.get_cert(5000)['metadata']['deploy_attempt_count'] == 0

    @pytest.mark.parametrize('count,state,expected_state,expected_stage', [
        (0, '', '', None),
        (1, '', '', None),
        (5, '', '', None),
        (0, 'pending', 'processing', None),
        (5, 'pending', 'processing', None),
        (1, 'processing', 'processing', None),
        (5, 'active', 'active', None),
        (10, '', 'CAPPED', 'legacy'),
        (11, '', 'CAPPED', 'legacy'),
        (10, 'pending', 'CAPPED', 'legacy'),
        (11, 'processing', 'CAPPED', 'legacy'),
        (10, 'active', 'CAPPED', 'legacy'),
    ])
    def test_count_state_matrix(self, tmp_data_dir, count, state, expected_state, expected_stage):
        """计数 0/1/5/10/11 × 状态 空/pending/processing/active 表驱动迁移"""
        _write_legacy_config(tmp_data_dir, {
            'cert_expires_at': '', 'issue_retry_count': count, 'last_issue_state': state})
        cm = ConfigManager(tmp_data_dir)
        meta = cm.get_cert(5000)['metadata']
        assert meta['last_issue_state'] == expected_state
        if expected_stage:
            assert meta.get('capped_phase') == expected_stage
        # 部署计数始终从零，不从旧签发计数推断
        assert meta['deploy_attempt_count'] == 0

    def test_legacy_cap_is_idempotent(self, tmp_data_dir):
        """已 CAPPED(legacy) 的证书再次加载不改写、不补发历史"""
        _write_legacy_config(tmp_data_dir, {
            'cert_expires_at': '', 'issue_retry_count': 12, 'last_issue_state': ''})
        ConfigManager(tmp_data_dir)
        cm2 = ConfigManager(tmp_data_dir)  # 二次加载
        meta = cm2.get_cert(5000)['metadata']
        assert meta['last_issue_state'] == 'CAPPED'
        assert meta['capped_phase'] == 'legacy'

    @pytest.mark.parametrize('domains,mode,vm,expected', [
        (['1.2.3.4'], 'pull', '', 'policy_blocked_needs_setup'),          # IP + pull
        (['1.2.3.4'], 'local', 'delegation', 'policy_blocked_needs_setup'),  # IP + delegation
        (['2001:db8::1'], 'pull', '', 'policy_blocked_needs_setup'),      # IPv6 + pull
        (['1.2.3.4'], '', '', 'policy_blocked_needs_setup'),              # IP + 继承全局 pull
        (['1.2.3.4'], 'local', 'file', ''),                              # IP + local/file 合法
        (['a.com'], 'pull', '', ''),                                     # DNS + pull 合法
        (['a.com'], 'local', 'delegation', ''),                         # DNS + local/delegation 合法
    ])
    def test_policy_blocked_matrix(self, tmp_data_dir, domains, mode, vm, expected):
        """旧非法 IP 配置（IP+pull / IP+delegation）进入 policy_blocked_needs_setup，不自动改配置"""
        _write_legacy_config(tmp_data_dir, {
            'cert_expires_at': '', 'issue_retry_count': 0, 'last_issue_state': ''},
            domains=domains, renew_mode=mode, validation_method=vm)
        cm = ConfigManager(tmp_data_dir)
        cert = cm.get_cert(5000)
        assert cert['metadata']['last_issue_state'] == expected
        # 不自动改配置：renew_mode / validation_method 保持原值
        assert cert['renew_mode'] == mode
        assert cert['validation_method'] == vm

    def test_policy_blocked_not_applied_when_capped(self, tmp_data_dir):
        """已因计数触顶 CAPPED 的 IP+pull 证书不再叠加 policy_blocked（CAPPED 优先，终态）"""
        _write_legacy_config(tmp_data_dir, {
            'cert_expires_at': '', 'issue_retry_count': 11, 'last_issue_state': ''},
            domains=['1.2.3.4'], renew_mode='pull')
        cm = ConfigManager(tmp_data_dir)
        assert cm.get_cert(5000)['metadata']['last_issue_state'] == 'CAPPED'


class TestLegacyMergeDataLoss:
    """旧文件合并的删除判据（F1/A1）

    核心风险：merged_files.append 曾在合并判断之外，"读失败"和"目标已有数据故未合并"
    两种情况都会连同数据一起被删；而写入失败被 except OSError 吞掉后同样照删，
    结果是两个文件都没有证书配置，且全程零异常。
    """

    @staticmethod
    def _write(path, data):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def _legacy_certs(self, data_dir, order_id=111):
        path = os.path.join(data_dir, 'certs.json')
        self._write(path, {'certificates': [{
            'order_id': order_id, 'cert_name': 'order-%d' % order_id,
            'domains': ['legacy.com'], 'site_name': [], 'metadata': {},
        }]})
        return path

    def test_merged_then_removed(self, tmp_data_dir):
        """正常合并：内容并入且写入成功，旧文件删除"""
        legacy = self._legacy_certs(tmp_data_dir)
        cfg = ConfigManager(tmp_data_dir)
        assert [c['order_id'] for c in cfg.get_certs()] == [111]
        assert not os.path.exists(legacy)

    def test_not_merged_is_kept_as_orphan(self, tmp_data_dir):
        """目标已有 certificates 故未合并：旧文件绝不能被删，改名保留"""
        self._write(os.path.join(tmp_data_dir, 'config.json'), {
            'release_url': '', 'upgrade_channel': 'main',
            'schedule': {'renew_mode': 'pull', 'renew_before_days': 14},
            'certificates': [{
                'order_id': 999, 'cert_name': 'order-999',
                'domains': ['a.com'], 'site_name': [], 'metadata': {},
            }],
        })
        legacy = self._legacy_certs(tmp_data_dir, order_id=111)

        cfg = ConfigManager(tmp_data_dir)
        assert [c['order_id'] for c in cfg.get_certs()] == [999]
        assert not os.path.exists(legacy), 'certs.json 不应残留'
        assert os.path.exists(legacy + '.orphan'), '未合并的旧文件必须保留为 .orphan'
        with open(legacy + '.orphan') as f:
            assert json.load(f)['certificates'][0]['order_id'] == 111

    def test_corrupt_legacy_is_kept_as_orphan(self, tmp_data_dir):
        """旧文件本身损坏：读不出内容不代表没有价值，同样保留"""
        legacy = os.path.join(tmp_data_dir, 'certs.json')
        with open(legacy, 'w') as f:
            f.write('{ broken json')

        ConfigManager(tmp_data_dir)
        assert not os.path.exists(legacy)
        assert os.path.exists(legacy + '.orphan')

    def test_corrupt_legacy_does_not_degrade_plugin(self, tmp_data_dir):
        """遗留文件损坏不得让插件进入只读降级——否则用户连删证书都做不到"""
        with open(os.path.join(tmp_data_dir, 'certs.json'), 'w') as f:
            f.write('{ broken json')

        cfg = ConfigManager(tmp_data_dir)
        assert cfg.is_degraded() is False
        cfg.add_cert(order_id=1, cert_name='order-1', domains=['a.com'], site_names=[])
        assert len(cfg.get_certs()) == 1

    def test_write_failure_keeps_legacy_file(self, tmp_data_dir, monkeypatch):
        """写入失败：已合并的内容并未落盘，删除旧文件会让数据两头皆空"""
        legacy = self._legacy_certs(tmp_data_dir)

        def boom(self, path, data):
            raise OSError('disk full')

        monkeypatch.setattr(ConfigManager, '_write_json', boom)
        ConfigManager(tmp_data_dir)

        assert os.path.exists(legacy) or os.path.exists(legacy + '.orphan'), \
            '写入失败时旧文件必须以某种形式保留'
        surviving = legacy if os.path.exists(legacy) else legacy + '.orphan'
        with open(surviving) as f:
            assert json.load(f)['certificates'][0]['order_id'] == 111

    def test_write_failure_on_non_oserror_does_not_break_plugin(self, tmp_data_dir, monkeypatch):
        """json.dump 对不可序列化对象抛 TypeError，穿透出去会让插件整体不可用"""
        self._legacy_certs(tmp_data_dir)

        def boom(self, path, data):
            raise TypeError('not JSON serializable')

        monkeypatch.setattr(ConfigManager, '_write_json', boom)
        ConfigManager(tmp_data_dir)  # 不得抛出

    def test_orphan_collision_keeps_original(self, tmp_data_dir):
        """已有同名 .orphan 时保留原件，不覆盖上一次的备份"""
        legacy = os.path.join(tmp_data_dir, 'certs.json')
        with open(legacy, 'w') as f:
            f.write('{ broken')
        with open(legacy + '.orphan', 'w') as f:
            f.write('{"certificates": [{"order_id": 1}]}')

        ConfigManager(tmp_data_dir)
        assert os.path.exists(legacy), '同名 orphan 已存在时应保留原件'
        with open(legacy + '.orphan') as f:
            assert json.load(f)['certificates'][0]['order_id'] == 1, '旧 orphan 不得被覆盖'


class TestConfigDegradedMode:
    """主配置损坏后的只读降级（F2/A2）

    核心风险：损坏后第一次写操作会经 _update_json 的"解析失败 → 备份 → 回落默认值 →
    照常写回"路径，把用户的全部证书配置替换成空配置，而面板显示"暂无证书"、
    renew_status 写出新鲜 last_run + 全 0，与一台干净安装完全同形。
    """

    @staticmethod
    def _corrupt(data_dir, content='{ THIS IS NOT JSON'):
        path = os.path.join(data_dir, 'config.json')
        with open(path, 'w') as f:
            f.write(content)
        return path

    def test_corrupt_config_enters_degraded(self, tmp_data_dir):
        path = self._corrupt(tmp_data_dir)
        cfg = ConfigManager(tmp_data_dir)
        assert cfg.is_degraded() is True
        with open(path) as f:
            assert f.read() == '{ THIS IS NOT JSON', '构造阶段不得改动损坏文件'

    def test_write_is_refused_and_file_untouched(self, tmp_data_dir):
        """核心断言：任何写入都被拒绝，损坏文件原样保留"""
        from lib.config import ConfigDegradedError

        path = self._corrupt(tmp_data_dir)
        cfg = ConfigManager(tmp_data_dir)

        with pytest.raises(ConfigDegradedError):
            cfg.add_cert(order_id=1, cert_name='order-1', domains=['a.com'], site_names=[])
        with pytest.raises(ConfigDegradedError):
            cfg.update_metadata(1, {'cert_expires_at': 'x'})
        with pytest.raises(ConfigDegradedError):
            cfg.save_config({'schedule': {'renew_mode': 'pull'}})

        with open(path) as f:
            assert f.read() == '{ THIS IS NOT JSON'

    def test_backup_not_overwritten_by_second_corruption(self, tmp_data_dir):
        """第二次损坏不得毁掉第一份可恢复副本"""
        path = self._corrupt(tmp_data_dir, '{ first corruption')
        ConfigManager(tmp_data_dir)
        with open(path + '.bak') as f:
            assert f.read() == '{ first corruption'

        with open(path, 'w') as f:
            f.write('{ second corruption')
        ConfigManager(tmp_data_dir)
        with open(path + '.bak') as f:
            assert f.read() == '{ first corruption', '.bak 不得被第二次损坏覆盖'

    def test_healthy_config_is_not_degraded(self, tmp_data_dir):
        cfg = ConfigManager(tmp_data_dir)
        assert cfg.is_degraded() is False
        cfg.add_cert(order_id=1, cert_name='order-1', domains=['a.com'], site_names=[])
        assert len(cfg.get_certs()) == 1

    def test_recovers_after_manual_fix(self, tmp_data_dir):
        """降级随进程重建：用户修好文件后下一次请求即自动恢复"""
        path = self._corrupt(tmp_data_dir)
        assert ConfigManager(tmp_data_dir).is_degraded() is True

        with open(path, 'w') as f:
            json.dump(copy.deepcopy(DEFAULT_CONFIG), f)
        cfg = ConfigManager(tmp_data_dir)
        assert cfg.is_degraded() is False
        cfg.add_cert(order_id=1, cert_name='order-1', domains=['a.com'], site_names=[])
        assert len(cfg.get_certs()) == 1


class TestMetadataFieldMigration:
    """metadata 层字段迁移（此前迁移引擎只覆盖全局层与证书层，够不到 cert['metadata']）"""

    def test_cap_stage_renamed_to_capped_phase(self, tmp_data_dir):
        """cap_stage → capped_phase：v0.3.9 已发布旧名，线上盘上确有该键

        不迁移的表现是面板丢掉具体触顶阶段（退回泛化文案「重试超限」），
        且旧键永久滞留成死数据。
        """
        _write_legacy_config(tmp_data_dir, {
            'cert_expires_at': '', 'last_issue_state': 'CAPPED', 'cap_stage': 'stalled',
        })
        cm = ConfigManager(tmp_data_dir)
        meta = cm.get_cert(5000)['metadata']
        assert meta['capped_phase'] == 'stalled', '旧值必须搬到新键'
        assert 'cap_stage' not in meta, '旧键必须移除，不留死数据'

    def test_migration_persists_to_disk(self, tmp_data_dir):
        """迁移结果要落盘，否则每次加载都重算、且外部读 config.json 仍是旧名"""
        _write_legacy_config(tmp_data_dir, {
            'cert_expires_at': '', 'last_issue_state': 'CAPPED', 'cap_stage': 'issue',
        })
        ConfigManager(tmp_data_dir)
        with open(os.path.join(tmp_data_dir, 'config.json')) as f:
            raw = json.load(f)
        meta = raw['certificates'][0]['metadata']
        assert meta['capped_phase'] == 'issue'
        assert 'cap_stage' not in meta

    def test_migration_is_idempotent(self, tmp_data_dir):
        """重复加载不产生副作用（迁移引擎的幂等性要求）"""
        _write_legacy_config(tmp_data_dir, {
            'cert_expires_at': '', 'last_issue_state': 'CAPPED', 'cap_stage': 'deploy',
        })
        ConfigManager(tmp_data_dir)
        cm = ConfigManager(tmp_data_dir)
        assert cm.get_cert(5000)['metadata']['capped_phase'] == 'deploy'

    def test_new_value_wins_when_both_keys_present(self, tmp_data_dir):
        """新旧键并存（部分升级/回滚过的盘）：保留新值，不被旧值覆盖"""
        _write_legacy_config(tmp_data_dir, {
            'cert_expires_at': '', 'last_issue_state': 'CAPPED',
            'cap_stage': 'legacy', 'capped_phase': 'stalled',
        })
        cm = ConfigManager(tmp_data_dir)
        meta = cm.get_cert(5000)['metadata']
        assert meta['capped_phase'] == 'stalled'
        assert 'cap_stage' not in meta

    def test_absent_old_key_is_noop(self, tmp_data_dir):
        """全新安装无旧键：不得凭空造出 capped_phase"""
        _write_legacy_config(tmp_data_dir, {'cert_expires_at': ''})
        cm = ConfigManager(tmp_data_dir)
        assert 'capped_phase' not in cm.get_cert(5000)['metadata']
