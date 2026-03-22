"""续签引擎测试"""

import os
import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from lib.config import ConfigManager
from lib.renew import (
    RenewEngine, needs_renewal,
    PULL_RENEW_DEFAULT_DAY, LOCAL_RENEW_DEFAULT_DAY,
    SERVER_AUTO_RENEW_DAYS, MAX_ISSUE_RETRY_COUNT,
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
    def test_pull_needs_renewal(self):
        cert = _make_cert_entry(days_remaining=10)
        assert needs_renewal(cert, PULL_RENEW_DEFAULT_DAY, 'pull') is True

    def test_pull_no_renewal(self):
        cert = _make_cert_entry(days_remaining=30)
        assert needs_renewal(cert, PULL_RENEW_DEFAULT_DAY, 'pull') is False

    def test_pull_boundary(self):
        cert = _make_cert_entry(days_remaining=13)
        assert needs_renewal(cert, PULL_RENEW_DEFAULT_DAY, 'pull') is True

    def test_local_needs_renewal(self):
        """Local 模式：14 < days <= 15"""
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=15, hours=1)
        cert = _make_cert_entry(days_remaining=15)
        cert['metadata']['cert_expires_at'] = expires.strftime('%Y-%m-%dT%H:%M:%SZ')
        assert needs_renewal(cert, LOCAL_RENEW_DEFAULT_DAY, 'local') is True

    def test_local_too_close(self):
        """Local 模式：<= 14 天不触发（服务端自动续签范围）"""
        cert = _make_cert_entry(days_remaining=10)
        assert needs_renewal(cert, LOCAL_RENEW_DEFAULT_DAY, 'local') is False

    def test_local_not_expired_enough(self):
        cert = _make_cert_entry(days_remaining=30)
        assert needs_renewal(cert, LOCAL_RENEW_DEFAULT_DAY, 'local') is False

    def test_local_processing_state(self):
        """Local 模式，processing 状态，只需 <= renew_before_days"""
        cert = _make_cert_entry(days_remaining=10, issue_state='processing')
        assert needs_renewal(cert, LOCAL_RENEW_DEFAULT_DAY, 'local') is True

    def test_no_expires_at(self):
        cert = {'metadata': {}}
        assert needs_renewal(cert, 13, 'pull') is False


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

    def test_retry_count_limit(self, engine):
        cert = _make_cert_entry(15, renew_mode='local', retry_count=MAX_ISSUE_RETRY_COUNT)
        with pytest.raises(RuntimeError, match='上限'):
            engine._renew_local(cert, engine._mock_api)

    @patch('lib.renew.cert_utils.generate_csr')
    def test_local_submit_csr(self, mock_csr, engine, tmp_data_dir):
        """Local 模式：CSR 提交后进入 processing 状态"""
        mock_csr.return_value = ('CSR-PEM', 'KEY-PEM', 'hash123')
        engine._mock_api.submit_csr.return_value = {
            'status': 'processing',
        }
        cert = _make_cert_entry(15, renew_mode='local')
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
        cert = _make_cert_entry(15, renew_mode='local')
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

    def test_local_handle_processing_active(self, engine, tmp_data_dir):
        """Local 模式：processing → active → 部署"""
        cert = _make_cert_entry(15, renew_mode='local', issue_state='processing')
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

    def test_local_handle_processing_timeout(self, engine, tmp_data_dir):
        """CSR pending 超时 → 清除状态"""
        cert = _make_cert_entry(15, renew_mode='local', issue_state='processing')
        # 超时：25 小时前提交
        old_time = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime('%Y-%m-%dT%H:%M:%SZ')
        cert['metadata']['csr_submitted_at'] = old_time

        engine._config.add_cert(
            order_id=cert['order_id'],
            cert_name=cert['cert_name'],
            domains=cert['domains'],
            site_names=cert['site_name'],
        )
        result = engine._handle_processing(cert, engine._mock_api)
        assert result is False
        # 不应该查询 API（超时直接清除）
        engine._mock_api.query_order.assert_not_called()

    def test_retry_count_reset(self, engine, tmp_data_dir):
        """超过 7 天自动重置重试计数"""
        cert = _make_cert_entry(15, renew_mode='local', retry_count=5)
        old_time = (datetime.now(timezone.utc) - timedelta(days=8)).strftime('%Y-%m-%dT%H:%M:%SZ')
        cert['metadata']['csr_submitted_at'] = old_time

        engine._config.add_cert(
            order_id=cert['order_id'],
            cert_name=cert['cert_name'],
            domains=cert['domains'],
            site_names=cert['site_name'],
        )
        engine._check_retry_reset(cert['order_id'], cert['metadata'])
        updated = engine._config.get_cert(cert['order_id'])
        assert updated['metadata']['issue_retry_count'] == 0

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
        logger.warn.assert_called_once()
        assert 'API' in str(logger.warn.call_args) or 'api' in str(logger.warn.call_args).lower()

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
        logger.warn.assert_called_once()
