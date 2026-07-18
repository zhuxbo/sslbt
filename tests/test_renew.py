"""续签引擎测试"""

import os
import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from lib.config import ConfigManager
from lib.renew import (
    RenewEngine, needs_renewal,
    RENEW_DEFAULT_DAYS, MAX_ISSUE_RETRY_COUNT,
    RENEW_SLEEP_MIN, RENEW_SLEEP_MAX, SPREAD_TOTAL_MAX,
    MAX_RENEW_BATCH,
)


def _make_cert_entry(days_remaining, renew_mode='pull', issue_state='', retry_count=0, order_id=12345):
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=days_remaining)
    return {
        'order_id': order_id,
        'cert_name': 'test-cert',
        'domains': ['example.com'],
        'enabled': True,
        'site_name': ['example.com'],
        'renew_mode': renew_mode,
        'metadata': {
            'cert_expires_at': expires.strftime('%Y-%m-%dT%H:%M:%SZ'),
            'last_issue_state': issue_state,
            'issue_retry_count': retry_count,
            'csr_submitted_at': '',
        },
    }


class TestNeedsRenewal:
    def test_needs_renewal(self):
        cert = _make_cert_entry(days_remaining=10)
        assert needs_renewal(cert, RENEW_DEFAULT_DAYS) is True

    def test_no_renewal(self):
        cert = _make_cert_entry(days_remaining=30)
        assert needs_renewal(cert, RENEW_DEFAULT_DAYS) is False

    def test_boundary(self):
        cert = _make_cert_entry(days_remaining=13)
        assert needs_renewal(cert, RENEW_DEFAULT_DAYS) is True

    def test_no_expires_at(self):
        """空 cert_expires_at 视为到期时间未知，需进入 API 查询回填 metadata"""
        cert = {'metadata': {}}
        assert needs_renewal(cert, 13) is True

    def test_unparseable_expires_at(self):
        """无法解析的 cert_expires_at 同样视为未知需处理"""
        cert = {'metadata': {'cert_expires_at': 'not-a-date'}}
        assert needs_renewal(cert, 13) is True

    def test_processing_state_needs_query(self):
        """local 模式 processing 状态即使远未到期也需进入查询流程跟进签发（spec §3.5）"""
        cert = _make_cert_entry(days_remaining=60, renew_mode='local', issue_state='processing')
        assert needs_renewal(cert, RENEW_DEFAULT_DAYS) is True

    def test_processing_but_expired_stops(self):
        """processing 但已过期：停止续签，等待人工处理（spec §3.2/§3.5）"""
        cert = _make_cert_entry(days_remaining=-3, renew_mode='local', issue_state='processing')
        assert needs_renewal(cert, RENEW_DEFAULT_DAYS) is False

    def test_expired_cert_no_renewal(self):
        """已过期证书不再续签"""
        cert = _make_cert_entry(days_remaining=-5)
        assert needs_renewal(cert, RENEW_DEFAULT_DAYS) is False

    def test_expired_long_no_renewal(self):
        """过期很久的证书不再续签"""
        cert = _make_cert_entry(days_remaining=-30)
        assert needs_renewal(cert, RENEW_DEFAULT_DAYS) is False


class TestRenewEngine:
    @pytest.fixture
    def engine(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        api_factory = MagicMock(return_value=mock_api)
        deployer = MagicMock()
        deployer.deploy_multi.return_value = [{'site_name': 'example.com', 'status': True, 'message': '部署成功'}]
        logger = MagicMock()
        eng = RenewEngine(config, api_factory, deployer, logger)
        eng._mock_api = mock_api
        return eng

    def test_pull_renew_success(self, engine):
        """Pull 模式：active 状态 → 部署成功"""
        engine._mock_api.query_order.return_value = {
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
            'private_key': '---KEY---',
        }
        cert = _make_cert_entry(10)
        result = engine._renew_pull(cert, engine._mock_api)
        assert result is True
        engine._deployer.deploy_multi.assert_called_once()

    def test_pull_renew_not_active(self, engine):
        """Pull 模式：processing 状态 → 等待"""
        engine._mock_api.query_order.return_value = {
            'status': 'processing',
            'certificate': '',
            'ca_certificate': '',
        }
        cert = _make_cert_entry(10)
        result = engine._renew_pull(cert, engine._mock_api)
        assert result is False

    def test_pull_renew_no_ca(self, engine):
        """Pull 模式：缺少中间证书 → 等待"""
        engine._mock_api.query_order.return_value = {
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '',
            'private_key': '---KEY---',
        }
        cert = _make_cert_entry(10)
        result = engine._renew_pull(cert, engine._mock_api)
        assert result is False

    def test_pull_renew_no_site(self, engine):
        """未绑定站点 → 跳过"""
        engine._mock_api.query_order.return_value = {
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
            'private_key': '---KEY---',
        }
        cert = _make_cert_entry(10)
        cert['site_name'] = []
        result = engine._renew_pull(cert, engine._mock_api)
        assert result is False

    def test_check_and_renew_all_empty(self, engine):
        results = engine.check_and_renew_all()
        assert results == []

    def test_empty_expires_at_enters_query(self, engine, tmp_data_dir):
        """空 cert_expires_at 的证书应进入 API 查询流程回填 metadata，而非被静默跳过

        覆盖首次部署遇 processing / metadata 写失败等中间态导致 cron 永不接手的缺陷。
        """
        engine._mock_api.query_order.return_value = {
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
            'private_key': '---KEY---',
        }
        # add_cert 不写 metadata，cert_expires_at 保持空
        engine._config.add_cert(
            order_id=7777, cert_name='order-7777',
            domains=['example.com'], site_names=['example.com'],
        )
        results = engine.check_and_renew_all()
        # 进入了查询流程（而非因空 expires_at 被前置过滤跳过）
        engine._mock_api.query_order.assert_called_once()
        assert len(results) == 1
        assert results[0]['order_id'] == 7777

    def test_check_deploy_results_site_removed_raises(self, engine):
        """站点已删除并解绑（site_removed）首次按失败上报，与部署回调 failure 一致"""
        results = [
            {'site_name': 'live.com', 'status': True, 'message': '部署成功'},
            {'site_name': 'gone.com', 'status': False,
             'message': '站点已删除，已解除绑定', 'site_removed': True},
        ]
        with pytest.raises(RuntimeError, match='已删除'):
            engine._check_deploy_results(results, 123)

    def test_renew_status_failure_on_site_removed(self, engine, tmp_data_dir):
        """部分站点删除场景：renew 结果与 renew_status.json 均记 failure（与回调一致）"""
        engine._mock_api.query_order.return_value = {
            'status': 'active', 'certificate': '---C---',
            'ca_certificate': '---CA---', 'private_key': '---K---',
        }
        engine._deployer.deploy_multi.return_value = [
            {'site_name': 'live.com', 'status': True, 'message': '部署成功'},
            {'site_name': 'gone.com', 'status': False,
             'message': '站点已删除，已解除绑定', 'site_removed': True},
        ]
        cert = _make_cert_entry(10, order_id=9001)
        engine._config.add_cert(order_id=9001, cert_name='order-9001',
                                domains=cert['domains'], site_names=['live.com', 'gone.com'])
        engine._config.update_metadata(9001, cert['metadata'])

        results = engine.check_and_renew_all()
        assert results[0]['status'] == 'failure'
        assert '已删除' in results[0]['message']
        with open(os.path.join(tmp_data_dir, 'renew_status.json')) as f:
            status = json.load(f)
        assert status['failure'] == 1
        assert status['success'] == 0

    def test_writes_renew_status_file(self, engine, tmp_data_dir):
        """续签运行结束写入轻量状态文件（时间戳/成功失败计数，0600 权限）"""
        engine._mock_api.query_order.return_value = {
            'status': 'active', 'certificate': '---C---',
            'ca_certificate': '---CA---', 'private_key': '---K---',
        }
        engine._config.add_cert(order_id=8001, cert_name='order-8001',
                                domains=['example.com'], site_names=['example.com'])
        engine._config.update_metadata(8001, _make_cert_entry(10, order_id=8001)['metadata'])
        engine.check_and_renew_all()
        status_path = os.path.join(tmp_data_dir, 'renew_status.json')
        assert os.path.isfile(status_path)
        with open(status_path) as f:
            data = json.load(f)
        assert data['total'] == 1
        assert data['success'] == 1
        assert 'last_run' in data
        assert (os.stat(status_path).st_mode & 0o777) == 0o600

    def test_retry_count_limit(self, engine):
        """retry_count > MAX_ISSUE_RETRY_COUNT 时拒绝（spec 3.2: > 10）"""
        cert = _make_cert_entry(10, renew_mode='local', retry_count=MAX_ISSUE_RETRY_COUNT + 1)
        with pytest.raises(RuntimeError, match='上限'):
            engine._renew_local(cert, engine._mock_api)

    @patch('lib.renew.cert_utils.generate_csr')
    def test_retry_count_at_limit_still_allowed(self, mock_csr, engine, tmp_data_dir):
        """retry_count == MAX_ISSUE_RETRY_COUNT 时仍允许一次（spec: > 10 才停止）"""
        mock_csr.return_value = ('CSR-PEM', 'KEY-PEM', 'hash123')
        engine._mock_api.submit_csr.return_value = {'status': 'processing'}
        cert = _make_cert_entry(10, renew_mode='local', retry_count=MAX_ISSUE_RETRY_COUNT)
        engine._config.add_cert(
            order_id=cert['order_id'],
            cert_name=cert['cert_name'],
            domains=cert['domains'],
            site_names=cert['site_name'],
        )
        engine._config.update_metadata(cert['order_id'], cert['metadata'])
        # 不应抛出异常
        engine._renew_local(cert, engine._mock_api)

    @patch('lib.renew.cert_utils.generate_csr')
    def test_local_submit_csr(self, mock_csr, engine, tmp_data_dir):
        """Local 模式：CSR 提交后进入 processing 状态"""
        mock_csr.return_value = ('CSR-PEM', 'KEY-PEM', 'hash123')
        engine._mock_api.submit_csr.return_value = {
            'status': 'processing',
        }
        cert = _make_cert_entry(10, renew_mode='local')
        # 先添加证书到 config
        engine._config.add_cert(
            order_id=cert['order_id'],
            cert_name=cert['cert_name'],
            domains=cert['domains'],
            site_names=cert['site_name'],
        )
        result = engine._submit_new_csr(cert, engine._mock_api)
        assert result is False
        engine._mock_api.submit_csr.assert_called_once()
        # pending key 应该存在
        key_path = engine._pending_key_path(cert)
        assert os.path.isfile(key_path)

    @patch('lib.renew.cert_utils.generate_csr')
    def test_local_submit_csr_cleans_stale_pending_key(self, mock_csr, engine, tmp_data_dir):
        """残留的 pending key 不会导致 O_EXCL 失败"""
        mock_csr.return_value = ('CSR-PEM', 'NEW-KEY', 'hash456')
        engine._mock_api.submit_csr.return_value = {'status': 'processing'}
        cert = _make_cert_entry(10, renew_mode='local')
        engine._config.add_cert(
            order_id=cert['order_id'],
            cert_name=cert['cert_name'],
            domains=cert['domains'],
            site_names=cert['site_name'],
        )
        # 模拟残留 pending key
        key_path = engine._pending_key_path(cert)
        os.makedirs(os.path.dirname(key_path), exist_ok=True)
        with open(key_path, 'w') as f:
            f.write('OLD-KEY')
        # 不应抛异常
        result = engine._submit_new_csr(cert, engine._mock_api)
        assert result is False
        # pending key 应该是新内容
        with open(key_path, 'r') as f:
            assert f.read() == 'NEW-KEY'

    @patch('lib.renew.cert_utils.generate_csr')
    def test_local_submit_csr_validation_method_incompatible(self, mock_csr, engine, tmp_data_dir):
        """Local 模式：验证方式与域名不兼容时清理 pending key 并抛异常"""
        mock_csr.return_value = ('CSR-PEM', 'KEY-PEM', 'hash123')
        cert = _make_cert_entry(10, renew_mode='local')
        cert['domains'] = ['*.example.com']
        cert['validation_method'] = 'file'
        engine._config.add_cert(
            order_id=cert['order_id'],
            cert_name=cert['cert_name'],
            domains=cert['domains'],
            site_names=cert['site_name'],
        )
        with pytest.raises(RuntimeError, match='通配符'):
            engine._submit_new_csr(cert, engine._mock_api)
        # pending key 应已被清理
        key_path = engine._pending_key_path(cert)
        assert not os.path.exists(key_path)
        # API 不应被调用
        engine._mock_api.submit_csr.assert_not_called()

    @patch('lib.renew.cert_utils.generate_csr')
    def test_local_submit_csr_ip_rejects_delegation(self, mock_csr, engine, tmp_data_dir):
        """Local 模式：IP 域名拒绝 delegation 验证"""
        mock_csr.return_value = ('CSR-PEM', 'KEY-PEM', 'hash123')
        cert = _make_cert_entry(10, renew_mode='local')
        cert['domains'] = ['1.2.3.4']
        cert['validation_method'] = 'delegation'
        engine._config.add_cert(
            order_id=cert['order_id'],
            cert_name=cert['cert_name'],
            domains=cert['domains'],
            site_names=cert['site_name'],
        )
        with pytest.raises(RuntimeError, match='IP'):
            engine._submit_new_csr(cert, engine._mock_api)
        assert not os.path.exists(engine._pending_key_path(cert))

    def test_local_handle_processing_active(self, engine, tmp_data_dir):
        """Local 模式：processing → active → 部署"""
        cert = _make_cert_entry(10, renew_mode='local', issue_state='processing')
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        cert['metadata']['csr_submitted_at'] = now

        # 先添加证书到 config
        engine._config.add_cert(
            order_id=cert['order_id'],
            cert_name=cert['cert_name'],
            domains=cert['domains'],
            site_names=cert['site_name'],
        )

        # 写入 pending key
        key_path = engine._pending_key_path(cert)
        os.makedirs(os.path.dirname(key_path), exist_ok=True)
        with open(key_path, 'w') as f:
            f.write('KEY-PEM')

        engine._mock_api.query_order.return_value = {
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
        }
        result = engine._handle_processing(cert, engine._mock_api)
        assert result is True
        engine._deployer.deploy_multi.assert_called_once()

    def test_local_handle_processing_always_queries_api(self, engine, tmp_data_dir):
        """processing 状态下始终查询 API，无超时自动清除"""
        cert = _make_cert_entry(10, renew_mode='local', issue_state='processing')
        # 即使提交已过很久，也继续等待
        old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime('%Y-%m-%dT%H:%M:%SZ')
        cert['metadata']['csr_submitted_at'] = old_time

        engine._config.add_cert(
            order_id=cert['order_id'],
            cert_name=cert['cert_name'],
            domains=cert['domains'],
            site_names=cert['site_name'],
        )
        engine._mock_api.query_order.return_value = {'status': 'processing'}
        result = engine._handle_processing(cert, engine._mock_api)
        assert result is False
        # 仍然查询 API（无超时逻辑）
        engine._mock_api.query_order.assert_called_once()

    def test_retry_count_no_auto_reset(self, engine, tmp_data_dir):
        """重试次数超限后等待人工处理，不自动重置"""
        cert = _make_cert_entry(10, renew_mode='local', retry_count=MAX_ISSUE_RETRY_COUNT + 1)
        engine._config.add_cert(
            order_id=cert['order_id'],
            cert_name=cert['cert_name'],
            domains=cert['domains'],
            site_names=cert['site_name'],
        )
        # 超过上限直接抛出，不自动重置
        with pytest.raises(RuntimeError, match='上限'):
            engine._renew_local(cert, engine._mock_api)

    def test_front_filter_skips_retry_exceeded(self, tmp_data_dir):
        """前置过滤阶段跳过 local 模式重试超限的证书（spec 3.2）"""
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        api_factory = MagicMock(return_value=mock_api)
        deployer = MagicMock()
        logger = MagicMock()
        engine = RenewEngine(config, api_factory, deployer, logger)

        cert = _make_cert_entry(10, renew_mode='local', retry_count=MAX_ISSUE_RETRY_COUNT + 1)
        config.add_cert(
            order_id=cert['order_id'],
            cert_name=cert['cert_name'],
            domains=cert['domains'],
            site_names=cert['site_name'],
            renew_mode='local',
        )
        config.update_metadata(cert['order_id'], cert['metadata'])
        results = engine.check_and_renew_all()
        assert results == []
        # 不应创建 API 客户端（前置过滤即跳过）
        api_factory.assert_not_called()

    def test_check_and_renew_all_api_none(self, tmp_data_dir):
        """api_factory 返回 None 时证书被跳过且记录 warn"""
        config = ConfigManager(tmp_data_dir)
        api_factory = MagicMock(return_value=None)
        deployer = MagicMock()
        logger = MagicMock()
        engine = RenewEngine(config, api_factory, deployer, logger)

        cert = _make_cert_entry(10)
        config.add_cert(
            order_id=cert['order_id'],
            cert_name=cert['cert_name'],
            domains=cert['domains'],
            site_names=cert['site_name'],
        )
        # add_cert 不写 metadata，需手动补上过期时间以触发 needs_renewal
        config.update_metadata(cert['order_id'], cert['metadata'])
        results = engine.check_and_renew_all()
        assert results == []
        logger.warning.assert_called_once()
        assert 'API' in str(logger.warning.call_args) or 'api' in str(logger.warning.call_args).lower()

    @patch('lib.renew.time.sleep')
    def test_spread_adds_delay(self, mock_sleep, tmp_data_dir):
        """spread=True 时多证书间加随机延迟"""
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        mock_api.query_order.return_value = {
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
            'private_key': '---KEY---',
        }
        api_factory = MagicMock(return_value=mock_api)
        deployer = MagicMock()
        deployer.deploy_multi.return_value = [{'site_name': 'a.com', 'status': True, 'message': 'ok'}]
        logger = MagicMock()
        engine = RenewEngine(config, api_factory, deployer, logger)

        for oid in (2001, 2002, 2003):
            cert = _make_cert_entry(10, order_id=oid)
            config.add_cert(order_id=oid, cert_name='c%d' % oid, domains=cert['domains'],
                            site_names=['a.com'])
            config.update_metadata(oid, cert['metadata'])

        results = engine.check_and_renew_all(spread=True)
        assert len(results) == 3
        # 第一个不 sleep，后面两个各 sleep 一次
        assert mock_sleep.call_count == 2

    @patch('lib.renew.time.sleep')
    def test_no_spread_no_delay(self, mock_sleep, tmp_data_dir):
        """spread=False 时不加延迟"""
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        mock_api.query_order.return_value = {
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
            'private_key': '---KEY---',
        }
        api_factory = MagicMock(return_value=mock_api)
        deployer = MagicMock()
        deployer.deploy_multi.return_value = [{'site_name': 'a.com', 'status': True, 'message': 'ok'}]
        logger = MagicMock()
        engine = RenewEngine(config, api_factory, deployer, logger)

        for oid in (3001, 3002):
            cert = _make_cert_entry(10, order_id=oid)
            config.add_cert(order_id=oid, cert_name='c%d' % oid, domains=cert['domains'],
                            site_names=['a.com'])
            config.update_metadata(oid, cert['metadata'])

        results = engine.check_and_renew_all(spread=False)
        assert len(results) == 2
        mock_sleep.assert_not_called()

    def test_summary_log(self, tmp_data_dir):
        """续签完成后输出汇总日志"""
        config = ConfigManager(tmp_data_dir)
        api_factory = MagicMock(return_value=None)
        deployer = MagicMock()
        logger = MagicMock()
        engine = RenewEngine(config, api_factory, deployer, logger)

        # 无证书需要续签
        engine.check_and_renew_all()
        logger.info.assert_called()
        last_call = logger.info.call_args_list[-1]
        assert '无需续签' in str(last_call)

    def test_check_and_renew_all_mixed(self, tmp_data_dir):
        """两个证书：一个 api=None 被跳过，另一个正常续签"""
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        mock_api.query_order.return_value = {
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
            'private_key': '---KEY---',
        }
        # 第一个证书返回 None，第二个返回 mock_api
        api_factory = MagicMock(side_effect=[None, mock_api])
        deployer = MagicMock()
        deployer.deploy_multi.return_value = [{'site_name': 'b.com', 'status': True, 'message': '部署成功'}]
        logger = MagicMock()
        engine = RenewEngine(config, api_factory, deployer, logger)

        cert1 = _make_cert_entry(10, order_id=1001)
        cert2 = _make_cert_entry(10, order_id=1002)
        config.add_cert(order_id=1001, cert_name='cert1', domains=cert1['domains'], site_names=['a.com'])
        config.update_metadata(1001, cert1['metadata'])
        config.add_cert(order_id=1002, cert_name='cert2', domains=cert2['domains'], site_names=['b.com'])
        config.update_metadata(1002, cert2['metadata'])

        results = engine.check_and_renew_all()
        # cert1 被跳过（api=None），cert2 正常续签
        assert len(results) == 1
        assert results[0]['order_id'] == 1002
        assert results[0]['status'] == 'success'
        logger.warning.assert_called_once()

    def test_expired_cert_skipped(self, tmp_data_dir):
        """已过期证书不触发续签"""
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        api_factory = MagicMock(return_value=mock_api)
        deployer = MagicMock()
        logger = MagicMock()
        engine = RenewEngine(config, api_factory, deployer, logger)

        cert = _make_cert_entry(-5, order_id=4001)
        config.add_cert(order_id=4001, cert_name='expired', domains=cert['domains'],
                        site_names=['a.com'])
        config.update_metadata(4001, cert['metadata'])

        results = engine.check_and_renew_all()
        assert results == []
        mock_api.query_order.assert_not_called()

    @patch('lib.renew.os.remove', side_effect=OSError('permission denied'))
    @patch('lib.renew.os.path.isfile', return_value=True)
    def test_cleanup_pending_key_failure_logs_error(self, mock_isfile, mock_remove, engine):
        """cleanup 失败记录 error 日志"""
        cert = _make_cert_entry(10)
        engine._cleanup_pending_key(cert)
        engine._logger.error.assert_called()
        assert 'pending key' in str(engine._logger.error.call_args).lower()

    @patch('lib.renew.time.sleep')
    def test_batch_limit(self, mock_sleep, tmp_data_dir):
        """超过 MAX_RENEW_BATCH 时截断处理"""
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        mock_api.query_order.return_value = {
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
            'private_key': '---KEY---',
        }
        api_factory = MagicMock(return_value=mock_api)
        deployer = MagicMock()
        deployer.deploy_multi.return_value = [{'site_name': 'a.com', 'status': True, 'message': 'ok'}]
        logger = MagicMock()
        engine = RenewEngine(config, api_factory, deployer, logger)

        total = MAX_RENEW_BATCH + 5
        for oid in range(5001, 5001 + total):
            cert = _make_cert_entry(10, order_id=oid)
            config.add_cert(order_id=oid, cert_name='c%d' % oid, domains=cert['domains'],
                            site_names=['a.com'])
            config.update_metadata(oid, cert['metadata'])

        results = engine.check_and_renew_all(spread=False)
        assert len(results) == MAX_RENEW_BATCH
        # 应记录截断警告
        warn_calls = [str(c) for c in logger.warning.call_args_list]
        assert any('截断' in s or '上限' in s for s in warn_calls)

    def test_pull_renew_updates_renew_before_days(self, engine, tmp_data_dir):
        """Pull 模式：query_order 返回 renew_before_days 时更新全局配置"""
        mock_api = MagicMock()
        mock_api.last_renew_before_days = 21
        mock_api.query_order.return_value = {
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
            'private_key': '---KEY---',
        }
        cert = _make_cert_entry(10)
        engine._config.add_cert(
            order_id=cert['order_id'],
            cert_name=cert['cert_name'],
            domains=cert['domains'],
            site_names=cert['site_name'],
        )
        engine._renew_pull(cert, mock_api)
        cfg = engine._config.get_config()
        assert cfg['schedule']['renew_before_days'] == 21

    @patch('lib.renew.cert_utils.generate_csr')
    def test_local_submit_csr_updates_renew_before_days(self, mock_csr, engine, tmp_data_dir):
        """Local 模式：submit_csr 返回 renew_before_days 时更新全局配置"""
        mock_csr.return_value = ('CSR-PEM', 'KEY-PEM', 'hash123')
        mock_api = MagicMock()
        mock_api.last_renew_before_days = 30
        mock_api.submit_csr.return_value = {'status': 'processing'}
        cert = _make_cert_entry(10, renew_mode='local')
        engine._config.add_cert(
            order_id=cert['order_id'],
            cert_name=cert['cert_name'],
            domains=cert['domains'],
            site_names=cert['site_name'],
        )
        engine._submit_new_csr(cert, mock_api)
        cfg = engine._config.get_config()
        assert cfg['schedule']['renew_before_days'] == 30

    def test_update_renew_before_days_zero_ignored(self, engine):
        """last_renew_before_days 为 0 时不更新配置"""
        mock_api = MagicMock()
        mock_api.last_renew_before_days = 0
        engine._config.save_config({'schedule': {'renew_before_days': 14, 'renew_mode': 'pull'}})
        engine._update_renew_before_days(mock_api)
        cfg = engine._config.get_config()
        assert cfg['schedule']['renew_before_days'] == 14


class TestOrderUpdate:
    """续费 order_id 更新测试"""

    @pytest.fixture
    def engine(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        api_factory = MagicMock(return_value=mock_api)
        deployer = MagicMock()
        deployer.deploy_multi.return_value = [{'site_name': 'example.com', 'status': True, 'message': '部署成功'}]
        logger = MagicMock()
        eng = RenewEngine(config, api_factory, deployer, logger)
        eng._mock_api = mock_api
        return eng

    def test_pull_renew_updates_order_id(self, engine, tmp_data_dir):
        """Pull 模式：API 返回新 order_id 时更新配置并用新 ID 部署"""
        engine._mock_api.query_order.return_value = {
            'order_id': 99999,
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
            'private_key': '---KEY---',
        }
        cert = _make_cert_entry(10, order_id=12345)
        engine._config.add_cert(
            order_id=12345, cert_name='order-12345',
            domains=cert['domains'], site_names=cert['site_name'],
        )
        result = engine._renew_pull(cert, engine._mock_api)
        assert result is True
        assert engine._config.get_cert(12345) is None
        assert engine._config.get_cert(99999) is not None
        assert engine._config.get_cert(99999)['cert_name'] == 'order-99999'
        # deploy_multi 使用了新 order_id
        assert engine._deployer.deploy_multi.call_args[1]['order_id'] == 99999

    def test_no_order_id_in_response(self, engine, tmp_data_dir):
        """API 响应无 order_id 字段时不更新"""
        engine._mock_api.query_order.return_value = {
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
            'private_key': '---KEY---',
        }
        cert = _make_cert_entry(10, order_id=12345)
        engine._config.add_cert(
            order_id=12345, cert_name='order-12345',
            domains=cert['domains'], site_names=cert['site_name'],
        )
        result = engine._renew_pull(cert, engine._mock_api)
        assert result is True
        assert engine._config.get_cert(12345) is not None

    def test_handle_processing_updates_order_id(self, engine, tmp_data_dir):
        """Local 模式 processing→active：更新 order_id 并重命名 pending key"""
        cert = _make_cert_entry(10, renew_mode='local', issue_state='processing', order_id=12345)
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        cert['metadata']['csr_submitted_at'] = now
        cert['cert_name'] = 'order-12345'
        engine._config.add_cert(
            order_id=12345, cert_name='order-12345',
            domains=cert['domains'], site_names=cert['site_name'],
        )
        # 写入 pending key
        key_dir = os.path.join(tmp_data_dir, 'pending-keys', 'order-12345')
        os.makedirs(key_dir, exist_ok=True)
        with open(os.path.join(key_dir, 'pending-key.pem'), 'w') as f:
            f.write('KEY-PEM')

        engine._mock_api.query_order.return_value = {
            'order_id': 99999,
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
        }
        result = engine._handle_processing(cert, engine._mock_api)
        assert result is True
        assert engine._config.get_cert(12345) is None
        assert engine._config.get_cert(99999) is not None
        # 旧 pending key 目录已不存在
        assert not os.path.isdir(key_dir)
        # deploy_multi 使用了新 order_id
        assert engine._deployer.deploy_multi.call_args[1]['order_id'] == 99999

    @patch('lib.renew.cert_utils.generate_csr')
    def test_submit_csr_updates_order_id(self, mock_csr, engine, tmp_data_dir):
        """Local 模式 submit_csr：API 返回新 order_id 时更新配置"""
        mock_csr.return_value = ('CSR-PEM', 'KEY-PEM', 'hash123')
        engine._mock_api.submit_csr.return_value = {
            'order_id': 99999,
            'status': 'processing',
        }
        cert = _make_cert_entry(10, renew_mode='local', order_id=12345)
        cert['cert_name'] = 'order-12345'
        engine._config.add_cert(
            order_id=12345, cert_name='order-12345',
            domains=cert['domains'], site_names=cert['site_name'],
        )
        result = engine._submit_new_csr(cert, engine._mock_api)
        assert result is False
        assert engine._config.get_cert(12345) is None
        new_cert = engine._config.get_cert(99999)
        assert new_cert is not None
        assert new_cert['cert_name'] == 'order-99999'

    def test_order_update_conflict_uses_old_id(self, engine, tmp_data_dir):
        """新 order_id 已存在时继续使用旧 ID"""
        engine._mock_api.query_order.return_value = {
            'order_id': 22222,
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
            'private_key': '---KEY---',
        }
        cert = _make_cert_entry(10, order_id=11111)
        engine._config.add_cert(order_id=11111, cert_name='order-11111',
                                domains=['a.com'], site_names=['a.com'])
        engine._config.add_cert(order_id=22222, cert_name='order-22222',
                                domains=['b.com'], site_names=['b.com'])
        result = engine._renew_pull(cert, engine._mock_api)
        assert result is True
        # 旧 ID 仍然存在
        assert engine._config.get_cert(11111) is not None
        # deploy_multi 使用了旧 order_id
        assert engine._deployer.deploy_multi.call_args[1]['order_id'] == 11111
        engine._logger.warning.assert_called()


class TestCalcSpreadDelay:
    """动态分散延迟计算"""

    def test_single_cert(self):
        s_min, s_max = RenewEngine._calc_spread_delay(1)
        assert s_min == RENEW_SLEEP_MIN
        assert s_max == RENEW_SLEEP_MAX

    def test_few_certs(self):
        """5 个证书，间隔仍在默认范围"""
        s_min, s_max = RenewEngine._calc_spread_delay(5)
        assert s_min >= RENEW_SLEEP_MIN
        assert s_max <= RENEW_SLEEP_MAX

    def test_many_certs_shrinks_delay(self):
        """50 个证书，间隔应缩短以控制总时长"""
        s_min, s_max = RenewEngine._calc_spread_delay(50)
        assert s_max < RENEW_SLEEP_MAX
        # 总延迟上限: 49 gaps * s_max <= SPREAD_TOTAL_MAX
        assert (50 - 1) * s_max <= SPREAD_TOTAL_MAX

    def test_100_certs(self):
        """100 个证书"""
        s_min, s_max = RenewEngine._calc_spread_delay(100)
        assert s_min >= RENEW_SLEEP_MIN
        assert s_max >= RENEW_SLEEP_MIN
        assert (100 - 1) * s_max <= SPREAD_TOTAL_MAX

    def test_zero_certs(self):
        s_min, s_max = RenewEngine._calc_spread_delay(0)
        assert s_min == RENEW_SLEEP_MIN
        assert s_max == RENEW_SLEEP_MAX


class TestFileVerifyIntegration:
    """续签引擎文件验证集成测试"""

    @pytest.fixture
    def engine_with_verifier(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        api_factory = MagicMock(return_value=mock_api)
        deployer = MagicMock()
        deployer.deploy_multi.return_value = [{'site_name': 'example.com', 'status': True, 'message': '部署成功'}]
        logger = MagicMock()
        file_verifier = MagicMock()
        file_verifier.place_file.return_value = ['/www/wwwroot/example.com/.well-known/acme-challenge/token']
        eng = RenewEngine(config, api_factory, deployer, logger, file_verifier)
        eng._mock_api = mock_api
        return eng

    @patch('lib.renew.cert_utils.generate_csr')
    def test_submit_csr_places_verify_file(self, mock_csr, engine_with_verifier, tmp_data_dir):
        """CSR 提交返回 file 字段时放置验证文件"""
        mock_csr.return_value = ('CSR-PEM', 'KEY-PEM', 'hash123')
        engine = engine_with_verifier
        engine._mock_api.submit_csr.return_value = {
            'status': 'processing',
            'file': {'path': '.well-known/acme-challenge/token', 'content': 'verify'},
        }
        cert = _make_cert_entry(10, renew_mode='local')
        cert['validation_method'] = 'file'
        engine._config.add_cert(
            order_id=cert['order_id'], cert_name=cert['cert_name'],
            domains=cert['domains'], site_names=cert['site_name'],
        )
        result = engine._submit_new_csr(cert, engine._mock_api)
        assert result is False
        engine._file_verifier.place_file.assert_called_once()
        # validation_method 应传给 submit_csr
        call_kwargs = engine._mock_api.submit_csr.call_args
        assert call_kwargs[1].get('validation_method') == 'file' or \
               (len(call_kwargs[0]) > 3 and call_kwargs[0][3] == 'file')

    @patch('lib.renew.cert_utils.generate_csr')
    def test_submit_csr_no_file_field(self, mock_csr, engine_with_verifier, tmp_data_dir):
        """CSR 提交无 file 字段时不放置验证文件"""
        mock_csr.return_value = ('CSR-PEM', 'KEY-PEM', 'hash123')
        engine = engine_with_verifier
        engine._mock_api.submit_csr.return_value = {'status': 'processing'}
        cert = _make_cert_entry(10, renew_mode='local')
        engine._config.add_cert(
            order_id=cert['order_id'], cert_name=cert['cert_name'],
            domains=cert['domains'], site_names=cert['site_name'],
        )
        engine._submit_new_csr(cert, engine._mock_api)
        engine._file_verifier.place_file.assert_not_called()

    def test_handle_processing_active_cleans_verify_files(self, engine_with_verifier, tmp_data_dir):
        """证书签发成功后清理验证文件"""
        engine = engine_with_verifier
        cert = _make_cert_entry(10, renew_mode='local', issue_state='processing')
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        cert['metadata']['csr_submitted_at'] = now
        cert['metadata']['pending_verify_paths'] = ['/www/wwwroot/example.com/.well-known/acme-challenge/token']

        engine._config.add_cert(
            order_id=cert['order_id'], cert_name=cert['cert_name'],
            domains=cert['domains'], site_names=cert['site_name'],
        )
        # 写入 pending key
        key_path = engine._pending_key_path(cert)
        os.makedirs(os.path.dirname(key_path), exist_ok=True)
        with open(key_path, 'w') as f:
            f.write('KEY-PEM')

        engine._mock_api.query_order.return_value = {
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
        }
        result = engine._handle_processing(cert, engine._mock_api)
        assert result is True
        engine._file_verifier.cleanup_files.assert_called_once()

    def test_handle_processing_replaces_verify_file_on_change(self, engine_with_verifier, tmp_data_dir):
        """API 返回新的验证文件时重新放置"""
        engine = engine_with_verifier
        cert = _make_cert_entry(10, renew_mode='local', issue_state='processing')
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        cert['metadata']['csr_submitted_at'] = now
        # 旧的验证文件信息
        cert['metadata']['pending_file_verify'] = {'path': '.well-known/acme-challenge/old-token', 'content': 'old'}
        cert['metadata']['pending_verify_paths'] = ['/www/wwwroot/example.com/.well-known/acme-challenge/old-token']

        engine._config.add_cert(
            order_id=cert['order_id'], cert_name=cert['cert_name'],
            domains=cert['domains'], site_names=cert['site_name'],
        )

        # API 返回新的验证文件
        engine._mock_api.query_order.return_value = {
            'status': 'processing',
            'file': {'path': '.well-known/acme-challenge/new-token', 'content': 'new'},
        }
        result = engine._handle_processing(cert, engine._mock_api)
        assert result is False
        # 应先清理旧文件，再放置新文件
        engine._file_verifier.cleanup_files.assert_called()
        engine._file_verifier.place_file.assert_called()

    def test_handle_processing_no_timeout_no_cleanup(self, engine_with_verifier, tmp_data_dir):
        """processing 状态无超时，不会因时间而自动清理验证文件"""
        engine = engine_with_verifier
        cert = _make_cert_entry(10, renew_mode='local', issue_state='processing')
        old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime('%Y-%m-%dT%H:%M:%SZ')
        cert['metadata']['csr_submitted_at'] = old_time
        cert['metadata']['pending_verify_paths'] = ['/www/wwwroot/example.com/.well-known/acme-challenge/token']

        engine._config.add_cert(
            order_id=cert['order_id'], cert_name=cert['cert_name'],
            domains=cert['domains'], site_names=cert['site_name'],
        )
        engine._mock_api.query_order.return_value = {'status': 'processing'}
        result = engine._handle_processing(cert, engine._mock_api)
        assert result is False
        # 仍在 processing，不清理验证文件（无超时）
        engine._file_verifier.cleanup_files.assert_not_called()

    def test_submit_csr_without_file_verifier(self, tmp_data_dir):
        """file_verifier=None 时文件验证代码不执行"""
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        mock_api.submit_csr.return_value = {
            'status': 'processing',
            'file': {'path': '.well-known/acme-challenge/token', 'content': 'c'},
        }
        deployer = MagicMock()
        logger = MagicMock()
        # 不传 file_verifier
        engine = RenewEngine(config, MagicMock(return_value=mock_api), deployer, logger)

        cert = _make_cert_entry(10, renew_mode='local')
        config.add_cert(order_id=cert['order_id'], cert_name=cert['cert_name'],
                        domains=cert['domains'], site_names=cert['site_name'])

        with patch('lib.renew.cert_utils.generate_csr', return_value=('CSR', 'KEY', 'hash')):
            result = engine._submit_new_csr(cert, mock_api)
        assert result is False
        # 不应报错，file 字段被忽略


class TestLocalUnknownExpiryRefill:
    """Local 模式到期时间未知：先查询 API 回填元数据再判定，不盲目提交 CSR"""

    @pytest.fixture
    def engine(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        # 与真实 APIClient 一致：未返回新阈值时为 0（避免 MagicMock.__int__ 默认返回 1
        # 污染全局 renew_before_days）
        mock_api.last_renew_before_days = 0
        api_factory = MagicMock(return_value=mock_api)
        deployer = MagicMock()
        deployer.deploy_multi.return_value = [
            {'site_name': 'example.com', 'status': True, 'message': '部署成功'}]
        logger = MagicMock()
        eng = RenewEngine(config, api_factory, deployer, logger)
        eng._mock_api = mock_api
        return eng

    def _add_local_cert_empty_meta(self, engine, order_id=7001):
        # add_cert 不写 metadata，cert_expires_at 保持空（到期时间未知）
        engine._config.add_cert(
            order_id=order_id, cert_name='order-%d' % order_id,
            domains=['example.com'], site_names=['example.com'], renew_mode='local',
        )
        return engine._config.get_cert(order_id)

    @patch('lib.renew.cert_utils.parse_cert_info')
    def test_unknown_expiry_queries_api_once(self, mock_parse, engine):
        """local + 空 metadata → 先查询 API（query_calls=1），不直接提交 CSR"""
        mock_parse.return_value = {
            'not_after': datetime.now(timezone.utc) + timedelta(days=60), 'serial': 'AA'}
        engine._mock_api.query_order.return_value = {
            'status': 'active', 'certificate': '---CERT---', 'ca_certificate': '---CA---'}
        cert = self._add_local_cert_empty_meta(engine)
        result = engine._renew_local(cert, engine._mock_api)
        engine._mock_api.query_order.assert_called_once()
        engine._mock_api.submit_csr.assert_not_called()
        assert result is False

    @patch('lib.renew.cert_utils.parse_cert_info')
    def test_unknown_expiry_far_future_no_submit(self, mock_parse, engine):
        """回填后远期 → 不提交 CSR，并写回 cert_expires_at"""
        far = datetime.now(timezone.utc) + timedelta(days=80)
        mock_parse.return_value = {'not_after': far, 'serial': 'BB'}
        engine._mock_api.query_order.return_value = {
            'status': 'active', 'certificate': '---CERT---', 'ca_certificate': '---CA---'}
        cert = self._add_local_cert_empty_meta(engine, order_id=7002)
        result = engine._renew_local(cert, engine._mock_api)
        assert result is False
        engine._mock_api.submit_csr.assert_not_called()
        # 元数据已回填，下轮无需再查询
        saved = engine._config.get_cert(7002)
        assert saved['metadata']['cert_expires_at'] != ''
        assert saved['metadata']['cert_serial'] == 'BB'

    @patch('lib.renew.cert_utils.generate_csr')
    @patch('lib.renew.cert_utils.parse_cert_info')
    def test_unknown_expiry_near_expiry_submits(self, mock_parse, mock_csr, engine):
        """回填后临期 → 正常走 CSR 提交"""
        near = datetime.now(timezone.utc) + timedelta(days=3)
        mock_parse.return_value = {'not_after': near, 'serial': 'CC'}
        mock_csr.return_value = ('CSR-PEM', 'KEY-PEM', 'hash123')
        engine._mock_api.query_order.return_value = {
            'status': 'active', 'certificate': '---CERT---', 'ca_certificate': '---CA---'}
        engine._mock_api.submit_csr.return_value = {'status': 'processing'}
        cert = self._add_local_cert_empty_meta(engine, order_id=7003)
        result = engine._renew_local(cert, engine._mock_api)
        engine._mock_api.query_order.assert_called_once()
        engine._mock_api.submit_csr.assert_called_once()
        assert result is False  # 提交后进入 processing

    def test_unknown_expiry_query_failure_no_submit(self, engine):
        """查询失败 → 不提交 CSR（按失败处理，不盲目重签）"""
        from lib.api_client import APIError
        engine._mock_api.query_order.side_effect = APIError('查询失败')
        cert = self._add_local_cert_empty_meta(engine, order_id=7004)
        with pytest.raises(APIError):
            engine._renew_local(cert, engine._mock_api)
        engine._mock_api.submit_csr.assert_not_called()

    def test_unknown_expiry_no_cert_content_no_submit(self, engine):
        """服务端未返回证书内容（processing）→ 本轮跳过，不提交 CSR"""
        engine._mock_api.query_order.return_value = {'status': 'processing', 'certificate': ''}
        cert = self._add_local_cert_empty_meta(engine, order_id=7005)
        result = engine._renew_local(cert, engine._mock_api)
        assert result is False
        engine._mock_api.submit_csr.assert_not_called()

    @patch('lib.renew.cert_utils.parse_cert_info')
    def test_unknown_expiry_parse_failure_no_submit(self, mock_parse, engine):
        """回填时证书解析失败 → 本轮跳过，不提交 CSR"""
        mock_parse.return_value = None
        engine._mock_api.query_order.return_value = {
            'status': 'active', 'certificate': '---CERT---', 'ca_certificate': '---CA---'}
        cert = self._add_local_cert_empty_meta(engine, order_id=7006)
        result = engine._renew_local(cert, engine._mock_api)
        assert result is False
        engine._mock_api.submit_csr.assert_not_called()

    @patch('lib.renew.cert_utils.generate_csr')
    def test_known_near_expiry_skips_refill_query(self, mock_csr, engine):
        """到期时间已知且临期 → 直接提交，不做回填查询"""
        mock_csr.return_value = ('CSR-PEM', 'KEY-PEM', 'hash123')
        engine._mock_api.submit_csr.return_value = {'status': 'processing'}
        engine._config.add_cert(
            order_id=7007, cert_name='order-7007',
            domains=['example.com'], site_names=['example.com'], renew_mode='local')
        cert = _make_cert_entry(5, renew_mode='local', order_id=7007)
        cert['cert_name'] = 'order-7007'
        engine._config.update_metadata(7007, cert['metadata'])
        engine._renew_local(engine._config.get_cert(7007), engine._mock_api)
        engine._mock_api.query_order.assert_not_called()
        engine._mock_api.submit_csr.assert_called_once()
