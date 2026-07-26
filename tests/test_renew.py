"""续签引擎测试"""

import os
import json
import fcntl
import time
import threading
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from lib.config import (
    ConfigManager, MAX_NO_PROGRESS_DAYS, MAX_BLOCK_REPORT_COUNT,
    ISSUE_STATE_CAPPED, CAPPED_PHASE_STALLED, CAPPED_PHASE_ISSUE,
    classify_order_status, DEPLOY_SUCCESS_RESET_KEYS,
)
from lib.api_client import APIError
from lib.renew import (
    RenewEngine, needs_renewal, _normalize_issue_status,
    RENEW_DEFAULT_DAYS, MAX_ISSUE_RETRY_COUNT, MAX_DEPLOY_ATTEMPT_COUNT,
    RENEW_SLEEP_MIN, RENEW_SLEEP_MAX, SPREAD_TOTAL_MAX,
    MAX_RENEW_BATCH, CALLBACK_BREAKER_THRESHOLD,
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
        engine._config.add_cert(
            order_id=cert['order_id'], cert_name=cert['cert_name'],
            domains=cert['domains'], site_names=cert['site_name'],
        )
        engine._config.update_metadata(cert['order_id'], cert['metadata'])
        result = engine._renew_pull(engine._config.get_cert(cert['order_id']), engine._mock_api)
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
        """服务端已签发但无绑定站点 → 记录阻断原因并按失败上报

        此前只记 warning 并返回 False，本轮被计为「等待签发」，服务端零回调、
        面板对已有到期时间的证书显示「正常」——两侧都看不出证书压根没部署上。
        """
        engine._mock_api.query_order.return_value = {
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
            'private_key': '---KEY---',
        }
        cert = _make_cert_entry(10)
        cert['site_name'] = []
        engine._config.add_cert(
            order_id=cert['order_id'], cert_name=cert['cert_name'],
            domains=cert['domains'], site_names=[],
        )
        engine._config.update_metadata(cert['order_id'], cert['metadata'])
        with pytest.raises(RuntimeError, match='未绑定站点'):
            engine._renew_pull(engine._config.get_cert(cert['order_id']), engine._mock_api)

        engine._mock_api.callback.assert_called_once()
        assert engine._mock_api.callback.call_args[1]['status'] == 'failure'
        meta = engine._config.get_cert(cert['order_id'])['metadata']
        assert '未绑定任何站点' in meta['last_deploy_block_reason']
        # 不计数：否则 10 轮后静默 CAPPED，用户绑好站点还要人工解除
        assert meta.get('deploy_attempt_count', 0) == 0

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

    def test_check_deploy_results_site_missing_raises(self, engine):
        """站点疑似删除（首轮缺失，site_missing）按失败上报，与回调 failure 一致"""
        results = [
            {'site_name': 'live.com', 'status': True, 'message': '部署成功'},
            {'site_name': 'gone.com', 'status': False,
             'message': '站点疑似已删除，待下一轮确认（本轮暂不解绑）', 'site_missing': True},
        ]
        with pytest.raises(RuntimeError, match='疑似'):
            engine._check_deploy_results(results, 123)

    def test_check_deploy_results_site_remove_failed_raises(self, engine):
        """解绑持久化失败必须按失败上报，不得宣称已经解除绑定"""
        results = [
            {'site_name': 'live.com', 'status': True, 'message': '部署成功'},
            {'site_name': 'gone.com', 'status': False,
             'message': '站点连续两轮缺失，但解除绑定持久化失败',
             'site_remove_failed': True},
        ]
        with pytest.raises(RuntimeError, match='持久化失败'):
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

    def test_issue_count_at_cap_enters_capped(self, engine, tmp_data_dir):
        """签发计数 >= 10（第 10 次后）：进入 CAPPED(issue) 静默，不提交、无第 11 次（spec §3.2）"""
        cert = _make_cert_entry(10, renew_mode='local', retry_count=MAX_ISSUE_RETRY_COUNT)
        engine._config.add_cert(
            order_id=cert['order_id'], cert_name=cert['cert_name'],
            domains=cert['domains'], site_names=cert['site_name'], renew_mode='local')
        engine._config.update_metadata(cert['order_id'], cert['metadata'])
        result = engine._renew_local(engine._config.get_cert(cert['order_id']), engine._mock_api)
        assert result is False
        engine._mock_api.submit_csr.assert_not_called()
        meta = engine._config.get_cert(cert['order_id'])['metadata']
        assert meta['last_issue_state'] == 'CAPPED'
        assert meta['capped_phase'] == 'issue'

    @patch('lib.renew.cert_utils.generate_csr')
    def test_issue_count_below_cap_still_submits(self, mock_csr, engine, tmp_data_dir):
        """签发计数 = 9（< 10）：仍允许提交第 10 次，提交后计数递增为 10（spec: >= 10 才停止）"""
        mock_csr.return_value = ('CSR-PEM', 'KEY-PEM', 'hash123')
        engine._mock_api.submit_csr.return_value = {'status': 'processing'}
        cert = _make_cert_entry(10, renew_mode='local', retry_count=MAX_ISSUE_RETRY_COUNT - 1)
        engine._config.add_cert(
            order_id=cert['order_id'], cert_name=cert['cert_name'],
            domains=cert['domains'], site_names=cert['site_name'], renew_mode='local')
        engine._config.update_metadata(cert['order_id'], cert['metadata'])
        result = engine._renew_local(engine._config.get_cert(cert['order_id']), engine._mock_api)
        assert result is False
        engine._mock_api.submit_csr.assert_called_once()
        assert engine._config.get_cert(cert['order_id'])['metadata']['issue_retry_count'] == 10

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
    def test_local_submit_csr_ip_derives_file(self, mock_csr, engine, tmp_data_dir):
        """Local 模式：IP 域名自动派生 file 验证并提交（覆盖入参 delegation，spec §5.2）"""
        mock_csr.return_value = ('CSR-PEM', 'KEY-PEM', 'hash123')
        engine._mock_api.submit_csr.return_value = {'status': 'processing'}
        cert = _make_cert_entry(10, renew_mode='local')
        cert['domains'] = ['1.2.3.4']
        cert['validation_method'] = 'delegation'  # 非法组合，应被派生覆盖为 file
        engine._config.add_cert(
            order_id=cert['order_id'],
            cert_name=cert['cert_name'],
            domains=cert['domains'],
            site_names=cert['site_name'],
            renew_mode='local',
        )
        engine._submit_new_csr(cert, engine._mock_api)
        engine._mock_api.submit_csr.assert_called_once()
        call = engine._mock_api.submit_csr.call_args
        submitted_vm = call[1].get('validation_method') if call[1].get('validation_method') is not None \
            else (call[0][3] if len(call[0]) > 3 else None)
        assert submitted_vm == 'file'

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

    def test_issue_cap_no_auto_reset(self, engine, tmp_data_dir):
        """签发触顶后进入 CAPPED 静默等待人工，不自动重置计数、不提交、不发回调"""
        cert = _make_cert_entry(10, renew_mode='local', retry_count=MAX_ISSUE_RETRY_COUNT)
        engine._config.add_cert(
            order_id=cert['order_id'], cert_name=cert['cert_name'],
            domains=cert['domains'], site_names=cert['site_name'], renew_mode='local')
        engine._config.update_metadata(cert['order_id'], cert['metadata'])
        result = engine._renew_local(engine._config.get_cert(cert['order_id']), engine._mock_api)
        assert result is False
        meta = engine._config.get_cert(cert['order_id'])['metadata']
        assert meta['issue_retry_count'] == MAX_ISSUE_RETRY_COUNT  # 未自动重置
        assert meta['last_issue_state'] == 'CAPPED'
        engine._mock_api.submit_csr.assert_not_called()
        engine._mock_api.callback.assert_not_called()

    def test_front_filter_caps_issue_retry_exceeded(self, tmp_data_dir):
        """前置过滤：local 签发计数 >= 10 → 进入 CAPPED(issue) 静默跳过，不建 API、不发回调（spec §3.2）"""
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        api_factory = MagicMock(return_value=mock_api)
        deployer = MagicMock()
        logger = MagicMock()
        engine = RenewEngine(config, api_factory, deployer, logger)

        cert = _make_cert_entry(10, renew_mode='local', retry_count=MAX_ISSUE_RETRY_COUNT)
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
        # 不应创建 API 客户端（前置过滤即跳过），不发任何回调
        api_factory.assert_not_called()
        mock_api.callback.assert_not_called()
        # 已置 CAPPED(issue) 终态
        meta = config.get_cert(cert['order_id'])['metadata']
        assert meta['last_issue_state'] == 'CAPPED'
        assert meta['capped_phase'] == 'issue'

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
    @patch('lib.renew.os.path.lexists', return_value=True)
    def test_cleanup_pending_key_failure_logs_error(self, mock_lexists, mock_remove, engine):
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

    def test_update_renew_before_days_over_cap_ignored(self, engine):
        """超过上限 30 视为服务端异常值，拒绝并保留本地配置"""
        mock_api = MagicMock()
        mock_api.last_renew_before_days = 31
        engine._config.save_config({'schedule': {'renew_before_days': 14, 'renew_mode': 'pull'}})
        engine._update_renew_before_days(mock_api)
        cfg = engine._config.get_config()
        assert cfg['schedule']['renew_before_days'] == 14

    def test_update_renew_before_days_at_cap_updates(self, engine):
        """上限值 30 本身有效，正常更新"""
        mock_api = MagicMock()
        mock_api.last_renew_before_days = 30
        engine._config.save_config({'schedule': {'renew_before_days': 14, 'renew_mode': 'pull'}})
        engine._update_renew_before_days(mock_api)
        cfg = engine._config.get_config()
        assert cfg['schedule']['renew_before_days'] == 30


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
        mock_parse.assert_called_once_with(
            '---CERT---', logger=engine._logger)
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


class TestDeployFailureRetainsPendingKey:
    """部署全失败时保留 pending 私钥（spec §3.8），仅成功才清理"""

    @pytest.fixture
    def engine(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        mock_api.last_renew_before_days = 0
        deployer = MagicMock()
        logger = MagicMock()
        eng = RenewEngine(config, MagicMock(return_value=mock_api), deployer, logger)
        eng._mock_api = mock_api
        return eng

    def _setup_processing_cert(self, engine, order_id=8101, site_names=None):
        site_names = site_names or ['example.com']
        cert = _make_cert_entry(10, renew_mode='local', issue_state='processing', order_id=order_id)
        cert['cert_name'] = 'order-%d' % order_id
        engine._config.add_cert(order_id=order_id, cert_name=cert['cert_name'],
                                domains=cert['domains'], site_names=site_names)
        engine._config.update_metadata(order_id, cert['metadata'])
        # 写入唯一 pending key
        key_path = engine._pending_key_path(cert)
        os.makedirs(os.path.dirname(key_path), exist_ok=True)
        with open(key_path, 'w') as f:
            f.write('PENDING-KEY')
        return engine._config.get_cert(order_id), key_path

    def test_handle_processing_all_fail_retains_pending_key(self, engine):
        """processing→active 但 deploy_multi 全失败 → pending key 保留供重试"""
        cert, key_path = self._setup_processing_cert(engine, 8101)
        engine._mock_api.query_order.return_value = {
            'status': 'active', 'certificate': '---CERT---', 'ca_certificate': '---CA---'}
        engine._deployer.deploy_multi.return_value = [
            {'site_name': 'example.com', 'status': False, 'message': '部署超时'}]
        with pytest.raises(RuntimeError, match='部署失败'):
            engine._handle_processing(cert, engine._mock_api)
        assert os.path.isfile(key_path)  # 全失败保留 pending key

    def test_handle_processing_success_cleans_pending_key(self, engine):
        """processing→active 且部署成功 → 清理 pending key（现行为）"""
        cert, key_path = self._setup_processing_cert(engine, 8102)
        engine._mock_api.query_order.return_value = {
            'status': 'active', 'certificate': '---CERT---', 'ca_certificate': '---CA---'}
        engine._deployer.deploy_multi.return_value = [
            {'site_name': 'example.com', 'status': True, 'message': '部署成功'}]
        result = engine._handle_processing(cert, engine._mock_api)
        assert result is True
        assert not os.path.exists(key_path)  # 成功后清理

    def test_handle_processing_partial_success_cleans_pending_key(self, engine):
        """部分成功：本轮按失败上报（与回调口径一致），但私钥已被消费必须清理

        清理判据是「私钥是否已写入站点」，与 _check_deploy_results 是否抛错解耦——
        任一站点成功即已消费，不清理就是泄漏。
        """
        cert, key_path = self._setup_processing_cert(
            engine, 8103, site_names=['example.com', 's2.example.com'])
        engine._mock_api.query_order.return_value = {
            'status': 'active', 'certificate': '---CERT---', 'ca_certificate': '---CA---'}
        engine._deployer.deploy_multi.return_value = [
            {'site_name': 'example.com', 'status': True, 'message': '部署成功'},
            {'site_name': 's2.example.com', 'status': False, 'message': '超时'}]
        with pytest.raises(RuntimeError, match='部分站点部署失败'):
            engine._handle_processing(cert, engine._mock_api)
        assert not os.path.exists(key_path)

    def test_handle_processing_partial_success_with_missing_cleans_pending_key(self, engine):
        """部分成功+另一站点疑似缺失：仍抛错上报，但私钥已被消费必须清理（不泄漏）"""
        cert, key_path = self._setup_processing_cert(
            engine, 8106, site_names=['example.com', 'gone.example.com'])
        engine._mock_api.query_order.return_value = {
            'status': 'active', 'certificate': '---CERT---', 'ca_certificate': '---CA---'}
        engine._deployer.deploy_multi.return_value = [
            {'site_name': 'example.com', 'status': True, 'message': '部署成功'},
            {'site_name': 'gone.example.com', 'status': False,
             'message': '站点疑似已删除，待下一轮确认（本轮暂不解绑）', 'site_missing': True}]
        with pytest.raises(RuntimeError, match='疑似'):
            engine._handle_processing(cert, engine._mock_api)
        assert not os.path.exists(key_path)  # 私钥已写入成功站点：抛错上报不影响清理


def _backdate_site_missing(config, order_id, hours=13):
    """回拨站点缺失跟踪的上次计入时间戳，模拟距上次缺失已超过最小确认间隔"""
    cert = config.get_cert(order_id)
    counts = cert['metadata'].get('site_missing_counts', {})
    old = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
    for sn in counts:
        if isinstance(counts[sn], dict):
            counts[sn]['last_at'] = old
    config.update_metadata(order_id, {'site_missing_counts': counts})


class TestProcessingOrphanConvergence:
    """processing 证书绑定站点全部删除确认解绑后，签发状态收敛不留孤儿"""

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_all_sites_deleted_then_converges(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        """三轮链路：疑似 → 确认解绑 → 无绑定站点收敛（清状态+清私钥+failure 回调）"""
        from lib.deployer import Deployer
        config = ConfigManager(tmp_data_dir)
        api = MagicMock()
        api.last_renew_before_days = 0
        api.callback.return_value = {'code': 1}
        api.query_order.return_value = {
            'status': 'active', 'certificate': '---CERT---', 'ca_certificate': '---CA---'}
        site_mgr = MagicMock()
        site_mgr.get_sites.return_value = [{'name': 'other.com', 'path': '/w'}]
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_set_ssl.return_value = {'status': True}
        deployer = Deployer(config, api, MagicMock(), site_mgr)
        engine = RenewEngine(config, MagicMock(return_value=api), deployer, MagicMock())

        config.add_cert(order_id=9301, cert_name='order-9301', domains=['example.com'],
                        site_names=['gone.com'], renew_mode='local')
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        config.update_metadata(9301, {'last_issue_state': 'processing', 'csr_submitted_at': now})
        key_path = engine._pending_key_path(config.get_cert(9301))
        os.makedirs(os.path.dirname(key_path), exist_ok=True)
        with open(key_path, 'w') as f:
            f.write('PENDING-KEY')

        # 轮 1：站点首轮缺失（疑似）→ 失败，不解绑，pending key 保留
        with pytest.raises(RuntimeError):
            engine._renew_local(config.get_cert(9301), api)
        assert config.get_cert(9301)['site_name'] == ['gone.com']
        assert os.path.isfile(key_path)

        # 跨最小间隔后轮 2：连续缺失确认删除并解绑（site_name 清空）
        _backdate_site_missing(config, 9301)
        with pytest.raises(RuntimeError):
            engine._renew_local(config.get_cert(9301), api)
        assert config.get_cert(9301)['site_name'] == []
        assert os.path.isfile(key_path)

        # 轮 3：无绑定站点可部署 → 按失败收敛：清签发状态、清 pending key、failure 回调
        with pytest.raises(RuntimeError, match='无绑定站点'):
            engine._renew_local(config.get_cert(9301), api)
        meta = config.get_cert(9301)['metadata']
        assert meta['last_issue_state'] == ''
        assert not os.path.exists(key_path)
        kwargs = api.callback.call_args.kwargs
        assert kwargs['status'] == 'failure'
        assert '无绑定站点' in kwargs.get('message', '')

        # 轮 4：不再进入 processing 分支（走未知到期回填路径），不盲目提交 CSR
        result = engine._renew_local(config.get_cert(9301), api)
        assert result is False
        api.submit_csr.assert_not_called()


class TestRenewStatusFileHardening:
    """续签状态临时文件加固：随机名 + 原子替换，抵御预置符号链接覆盖任意文件"""

    def _make_engine(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        return RenewEngine(config, MagicMock(), MagicMock(), MagicMock())

    def test_symlink_tmp_not_followed(self, tmp_data_dir):
        """预置 renew_status.json.tmp 符号链接 → 目标文件不被覆盖"""
        engine = self._make_engine(tmp_data_dir)
        victim = os.path.join(tmp_data_dir, 'victim.json')
        with open(victim, 'w') as f:
            f.write('IMPORTANT-DATA')
        # 预置固定名 .tmp 符号链接指向 victim（旧实现会经 O_TRUNC 覆盖）
        os.symlink(victim, os.path.join(tmp_data_dir, 'renew_status.json.tmp'))

        engine._write_renew_status([{'status': 'success'}])

        # victim 未被覆盖
        with open(victim) as f:
            assert f.read() == 'IMPORTANT-DATA'
        # 状态文件正常写入且为常规文件（非符号链接）
        status_path = os.path.join(tmp_data_dir, 'renew_status.json')
        assert not os.path.islink(status_path)
        with open(status_path) as f:
            data = json.load(f)
        assert data['total'] == 1 and data['success'] == 1
        assert (os.stat(status_path).st_mode & 0o777) == 0o600

    def test_normal_write_still_works(self, tmp_data_dir):
        """无攻击场景下正常写入状态文件"""
        engine = self._make_engine(tmp_data_dir)
        engine._write_renew_status([
            {'status': 'success'}, {'status': 'failure'}, {'status': 'pending'}])
        with open(os.path.join(tmp_data_dir, 'renew_status.json')) as f:
            data = json.load(f)
        assert data['total'] == 3
        assert data['success'] == 1 and data['failure'] == 1 and data['pending'] == 1
        # 不残留临时文件
        leftovers = [n for n in os.listdir(tmp_data_dir) if n.endswith('.tmp')]
        assert leftovers == []


class TestDeployCountAndCap:
    """部署计数分离、触顶 CAPPED(deploy) 静默、第 10 次失败标注（deploy-spec §3.2/§5.1）"""

    @pytest.fixture
    def engine(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        mock_api.last_renew_before_days = 0
        deployer = MagicMock()
        logger = MagicMock()
        eng = RenewEngine(config, MagicMock(return_value=mock_api), deployer, logger)
        eng._mock_api = mock_api
        return eng

    def _add_pull_cert(self, engine, order_id=6001, days=10, deploy_count=0, issue_count=0):
        cert = _make_cert_entry(days, order_id=order_id)
        engine._config.add_cert(order_id=order_id, cert_name=cert['cert_name'],
                                domains=cert['domains'], site_names=cert['site_name'])
        meta = dict(cert['metadata'])
        meta['deploy_attempt_count'] = deploy_count
        meta['issue_retry_count'] = issue_count
        engine._config.update_metadata(order_id, meta)
        return engine._config.get_cert(order_id)

    def _active(self):
        return {'status': 'active', 'certificate': '---C---',
                'ca_certificate': '---CA---', 'private_key': '---K---'}

    def test_deploy_uses_orchestrator_callback(self, engine):
        """自动续签：deploy_multi 以 send_callback=False 调用，回调由编排层发一次"""
        self._add_pull_cert(engine, 6001)
        engine._mock_api.query_order.return_value = self._active()
        engine._deployer.deploy_multi.return_value = [
            {'site_name': 'example.com', 'status': True, 'message': 'ok'}]
        result = engine._renew_pull(engine._config.get_cert(6001), engine._mock_api)
        assert result is True
        assert engine._deployer.deploy_multi.call_args[1]['send_callback'] is False
        engine._mock_api.callback.assert_called_once()
        assert engine._mock_api.callback.call_args[1]['status'] == 'success'

    def test_deploy_cap_front_filter_capped_no_callback(self, engine):
        """部署计数 >= 10：前置过滤进入 CAPPED(deploy) 静默，不查询、不部署、不发回调"""
        self._add_pull_cert(engine, 6002, deploy_count=MAX_DEPLOY_ATTEMPT_COUNT)
        results = engine.check_and_renew_all()
        assert results == []
        engine._mock_api.query_order.assert_not_called()
        engine._mock_api.callback.assert_not_called()
        meta = engine._config.get_cert(6002)['metadata']
        assert meta['last_issue_state'] == 'CAPPED'
        assert meta['capped_phase'] == 'deploy'

    def test_deploy_failure_increments_deploy_count(self, engine):
        """部署失败递增 deploy_attempt_count，签发计数不受污染（计数分离）"""
        self._add_pull_cert(engine, 6004, deploy_count=0, issue_count=3)
        engine._mock_api.query_order.return_value = self._active()
        engine._deployer.deploy_multi.return_value = [
            {'site_name': 'example.com', 'status': False, 'message': 'x'}]
        with pytest.raises(RuntimeError):
            engine._renew_pull(engine._config.get_cert(6004), engine._mock_api)
        meta = engine._config.get_cert(6004)['metadata']
        assert meta['deploy_attempt_count'] == 1  # 部署计数递增
        assert meta['issue_retry_count'] == 3      # 签发计数不变

    def test_deploy_intent_persist_failure_blocks_external_action(self, engine):
        """部署意图未持久化时不得调用面板部署或发送回调。"""
        self._add_pull_cert(engine, 6005)
        engine._mock_api.query_order.return_value = self._active()
        engine._config.update_metadata = MagicMock(side_effect=OSError('disk full'))

        with pytest.raises(OSError, match='disk full'):
            engine._renew_pull(engine._config.get_cert(6005), engine._mock_api)

        engine._deployer.deploy_multi.assert_not_called()
        engine._mock_api.callback.assert_not_called()

    def test_deploy_result_persist_failure_keeps_started_and_skips_callback(self, engine):
        """明确失败结果未落盘时保留 started 供重放，且不得提前发送回调。"""
        self._add_pull_cert(engine, 6006)
        engine._mock_api.query_order.return_value = self._active()
        engine._deployer.deploy_multi.return_value = [
            {'site_name': 'example.com', 'status': False, 'message': 'x'}]
        real_update = engine._config.update_metadata

        def fail_when_concluding(order_id, updates):
            if updates == {'deploy_started': False}:
                raise OSError('disk full')
            return real_update(order_id, updates)

        engine._config.update_metadata = MagicMock(side_effect=fail_when_concluding)

        with pytest.raises(OSError, match='disk full'):
            engine._renew_pull(engine._config.get_cert(6006), engine._mock_api)

        meta = engine._config.get_cert(6006)['metadata']
        assert meta['deploy_attempt_count'] == 1
        assert meta['deploy_started'] is True
        engine._mock_api.callback.assert_not_called()

    def test_last_deploy_failure_annotated_and_failure_callback(self, engine):
        """第 10 次（最后一次）部署失败：编排层发 failure 回调并标注'已达重试上限'"""
        self._add_pull_cert(engine, 6003, deploy_count=MAX_DEPLOY_ATTEMPT_COUNT - 1)
        engine._mock_api.query_order.return_value = self._active()
        engine._deployer.deploy_multi.return_value = [
            {'site_name': 'example.com', 'status': False, 'message': '部署超时'}]
        with pytest.raises(RuntimeError):
            engine._renew_pull(engine._config.get_cert(6003), engine._mock_api)
        cb = engine._mock_api.callback.call_args
        assert cb.kwargs['status'] == 'failure'
        assert '已达重试上限' in cb.kwargs['message']
        assert '部署超时' in cb.kwargs['message']
        assert engine._config.get_cert(6003)['metadata']['deploy_attempt_count'] == MAX_DEPLOY_ATTEMPT_COUNT

    def test_ten_deploy_failures_then_cap_no_eleventh(self, engine):
        """部署连续失败 10 轮：deploy_attempt_count 递增至 10，第 11 轮 CAPPED(deploy) 不再部署（§4.1.2）"""
        self._add_pull_cert(engine, 7400)
        engine._mock_api.query_order.return_value = self._active()
        engine._deployer.deploy_multi.return_value = [
            {'site_name': 'example.com', 'status': False, 'message': 'x'}]
        for _ in range(10):
            engine.check_and_renew_all()
        assert engine._deployer.deploy_multi.call_count == 10
        assert engine._config.get_cert(7400)['metadata']['deploy_attempt_count'] == MAX_DEPLOY_ATTEMPT_COUNT
        engine.check_and_renew_all()  # 第 11 轮
        assert engine._deployer.deploy_multi.call_count == 10  # 无第 11 次部署
        meta = engine._config.get_cert(7400)['metadata']
        assert meta['last_issue_state'] == 'CAPPED'
        assert meta['capped_phase'] == 'deploy'

    def test_deploy_started_replay_no_double_increment(self, engine):
        """崩溃恢复：deploy_started 已置位 → 重放同一部署意图不再自增计数（spec §4.1.2）"""
        self._add_pull_cert(engine, 6301, deploy_count=5)
        engine._config.update_metadata(6301, {'deploy_started': True})
        engine._mock_api.query_order.return_value = self._active()
        engine._deployer.deploy_multi.return_value = [
            {'site_name': 'example.com', 'status': False, 'message': 'x'}]
        with pytest.raises(RuntimeError):
            engine._renew_pull(engine._config.get_cert(6301), engine._mock_api)
        meta = engine._config.get_cert(6301)['metadata']
        assert meta['deploy_attempt_count'] == 5   # 复用同一尝试，未自增为 6
        assert meta['deploy_started'] is False       # 结束后清除 started

    def test_expired_transitions_expired_no_callback(self, engine):
        """已过期 → 转 EXPIRED 静默，不查询、不部署、不发回调"""
        cert = _make_cert_entry(-2, order_id=6201)
        engine._config.add_cert(order_id=6201, cert_name=cert['cert_name'],
                                domains=cert['domains'], site_names=cert['site_name'])
        engine._config.update_metadata(6201, cert['metadata'])
        results = engine.check_and_renew_all()
        assert results == []
        engine._mock_api.query_order.assert_not_called()
        engine._mock_api.callback.assert_not_called()
        assert engine._config.get_cert(6201)['metadata']['last_issue_state'] == 'EXPIRED'

    def test_safety_margin_skips_near_expiry_no_action(self, engine):
        """剩余有效期 < 24h 但未过期 → 本轮不启动新动作，不查询/不部署/不回调，状态不变"""
        cert = _make_cert_entry(0.5, order_id=6205)  # 约 12 小时
        engine._config.add_cert(order_id=6205, cert_name=cert['cert_name'],
                                domains=cert['domains'], site_names=cert['site_name'])
        engine._config.update_metadata(6205, cert['metadata'])
        results = engine.check_and_renew_all()
        assert results == []
        engine._mock_api.query_order.assert_not_called()
        engine._mock_api.callback.assert_not_called()
        # 未过期，不转 EXPIRED
        assert engine._config.get_cert(6205)['metadata']['last_issue_state'] == ''

    def test_terminal_capped_skipped_silently(self, engine):
        """已 CAPPED 的证书前置过滤直接跳过，不查询、不部署、不回调"""
        self._add_pull_cert(engine, 6206)
        engine._config.update_metadata(6206, {'last_issue_state': 'CAPPED', 'capped_phase': 'deploy'})
        results = engine.check_and_renew_all()
        assert results == []
        engine._mock_api.query_order.assert_not_called()
        engine._mock_api.callback.assert_not_called()

    def test_policy_blocked_skipped_silently(self, engine):
        """policy_blocked_needs_setup 的证书前置过滤跳过，不计数、不回调"""
        self._add_pull_cert(engine, 6207)
        engine._config.update_metadata(6207, {'last_issue_state': 'policy_blocked_needs_setup'})
        results = engine.check_and_renew_all()
        assert results == []
        engine._mock_api.query_order.assert_not_called()
        engine._mock_api.callback.assert_not_called()
        # 计数不变
        assert engine._config.get_cert(6207)['metadata']['deploy_attempt_count'] == 0

    def test_processing_cert_at_issue_cap_still_polls(self, engine):
        """签发计数 == 10 但已 processing（CSR 已被接受）→ 继续轮询签发，不因签发触顶停止"""
        cert = _make_cert_entry(10, renew_mode='local', issue_state='processing',
                                retry_count=MAX_ISSUE_RETRY_COUNT, order_id=6210)
        engine._config.add_cert(order_id=6210, cert_name=cert['cert_name'],
                                domains=cert['domains'], site_names=cert['site_name'], renew_mode='local')
        engine._config.update_metadata(6210, cert['metadata'])
        engine._mock_api.query_order.return_value = {'status': 'processing'}
        engine.check_and_renew_all()
        # 进入了查询流程（未被签发触顶前置拦截），状态仍 processing、未置 CAPPED
        engine._mock_api.query_order.assert_called_once()
        assert engine._config.get_cert(6210)['metadata']['last_issue_state'] == 'processing'


class TestIssueDeployCountSeparation:
    """签发计数与部署计数严格分离"""

    @pytest.fixture
    def engine(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        mock_api.last_renew_before_days = 0
        deployer = MagicMock()
        eng = RenewEngine(config, MagicMock(return_value=mock_api), deployer, MagicMock())
        eng._mock_api = mock_api
        return eng

    @patch('lib.renew.cert_utils.generate_csr')
    def test_issue_submit_does_not_touch_deploy_count(self, mock_csr, engine):
        """CSR 提交（签发尝试）只递增 issue_retry_count，不动 deploy_attempt_count"""
        mock_csr.return_value = ('CSR', 'KEY', 'hash')
        engine._mock_api.submit_csr.return_value = {'status': 'processing'}
        engine._config.add_cert(order_id=7201, cert_name='order-7201', domains=['a.com'],
                                site_names=['a.com'], renew_mode='local')
        engine._submit_new_csr(engine._config.get_cert(7201), engine._mock_api)
        meta = engine._config.get_cert(7201)['metadata']
        assert meta['issue_retry_count'] == 1
        assert meta['deploy_attempt_count'] == 0


class TestResponseLossRecovery:
    """CSR 提交响应丢失恢复：保留 pending 作为在途标记，下轮只查询订单恢复，绝不重复 POST"""

    @pytest.fixture
    def engine(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        mock_api.last_renew_before_days = 0
        deployer = MagicMock()
        deployer.deploy_multi.return_value = [
            {'site_name': 'a.com', 'status': True, 'message': 'ok'}]
        eng = RenewEngine(config, MagicMock(return_value=mock_api), deployer, MagicMock())
        eng._mock_api = mock_api
        return eng

    def _add_local(self, engine, order_id):
        engine._config.add_cert(order_id=order_id, cert_name='order-%d' % order_id,
                                domains=['a.com'], site_names=['a.com'], renew_mode='local')
        return engine._config.get_cert(order_id)

    @patch('lib.renew.cert_utils.generate_csr')
    def test_transport_failure_keeps_pending_state_empty(self, mock_csr, engine):
        """POST 传输不确定（transport）→ 保留 pending key + CSR，state 仍 ''，计数=1（本次意图）"""
        from lib.api_client import APIError
        mock_csr.return_value = ('CSR-PEM', 'KEY-PEM', 'hash')
        engine._mock_api.submit_csr.side_effect = APIError('网络超时', transport=True)
        cert = self._add_local(engine, 7101)
        result = engine._submit_new_csr(cert, engine._mock_api)
        assert result is False
        assert engine._has_pending_csr(engine._config.get_cert(7101))
        meta = engine._config.get_cert(7101)['metadata']
        assert meta['issue_retry_count'] == 1
        assert meta['last_issue_state'] == ''

    @patch('lib.renew.cert_utils.generate_csr')
    def test_definitive_rejection_cleans_pending(self, mock_csr, engine):
        """明确业务拒绝（非 transport）→ 清理 pending key + CSR 并抛出"""
        from lib.api_client import APIError
        mock_csr.return_value = ('CSR-PEM', 'KEY-PEM', 'hash')
        engine._mock_api.submit_csr.side_effect = APIError('订单状态非法', code=40001)
        cert = self._add_local(engine, 7104)
        with pytest.raises(APIError):
            engine._submit_new_csr(cert, engine._mock_api)
        assert not engine._has_pending_csr(engine._config.get_cert(7104))

    def test_recovery_normalizes_processing_no_resubmit(self, engine):
        """在途 CSR + 服务端已在处理 → 归一 processing，不重复 POST、不增计数"""
        cert = self._add_local(engine, 7102)
        engine._save_pending_key(cert, 'KEY')
        engine._save_pending_csr(cert, 'CSR')
        engine._config.update_metadata(7102, {'issue_retry_count': 1, 'last_csr_hash': 'h'})
        engine._mock_api.query_order.return_value = {'status': 'processing'}
        result = engine._renew_local(engine._config.get_cert(7102), engine._mock_api)
        assert result is False
        engine._mock_api.submit_csr.assert_not_called()
        meta = engine._config.get_cert(7102)['metadata']
        assert meta['last_issue_state'] == 'processing'
        assert meta['issue_retry_count'] == 1

    def test_recovery_pending_response_normalizes_processing(self, engine):
        """在途 CSR + 服务端返回 pending → 归一 processing（不重复 POST）"""
        cert = self._add_local(engine, 7105)
        engine._save_pending_key(cert, 'KEY')
        engine._save_pending_csr(cert, 'CSR')
        engine._config.update_metadata(7105, {'issue_retry_count': 2})
        engine._mock_api.query_order.return_value = {'status': 'pending'}
        result = engine._renew_local(engine._config.get_cert(7105), engine._mock_api)
        assert result is False
        engine._mock_api.submit_csr.assert_not_called()
        assert engine._config.get_cert(7105)['metadata']['last_issue_state'] == 'processing'

    def test_unpaid_is_waiting_and_never_posts(self, engine):
        """unpaid 是可自愈中间态（spec §2.4），必须按在途等待处置——**绝不 POST**

        POST 会触发服务端 pay 扣费，涉及资金的动作不由客户端自动发起。此前 unpaid 被当作
        终态写进 last_issue_state，该值既不在 TERMINAL_ISSUE_STATES（前置过滤拦不住）、
        又不等于 processing（不再走查询分支），下一轮就落到 _submit_new_csr 发出 POST。
        """
        cert = self._add_local(engine, 7103)
        engine._save_pending_key(cert, 'KEY')
        engine._save_pending_csr(cert, 'CSR-ORIG')
        engine._config.update_metadata(7103, {'issue_retry_count': 1})
        engine._mock_api.query_order.return_value = {'status': 'unpaid'}
        engine._mock_api.submit_csr.return_value = {'status': 'processing'}

        # 连续多轮：每轮都只查询，一次 POST 都不该发生
        for _ in range(3):
            assert engine._renew_local(engine._config.get_cert(7103), engine._mock_api) is False

        engine._mock_api.submit_csr.assert_not_called()
        meta = engine._config.get_cert(7103)['metadata']
        assert meta['last_issue_state'] == 'processing', 'unpaid 应归一在途，不得写成终态'
        assert meta['last_order_status'] == 'unpaid', '订单状态只进展示字段'
        assert meta['issue_retry_count'] == 1
        assert meta['no_progress_since'], '纯查询路径必须由无进展时限兜底'
        assert engine._has_pending_csr(engine._config.get_cert(7103))

    def test_processing_terminal_status_goes_to_display_field_only(self, engine):
        """processing 查询到真终态：状态只写展示字段，停止自动动作等待人工处理（spec §2.4）"""
        cert = self._add_local(engine, 7106)
        engine._save_pending_key(cert, 'KEY')
        engine._save_pending_csr(cert, 'CSR')
        engine._config.update_metadata(7106, {
            'issue_retry_count': 2,
            'last_issue_state': 'processing',
        })
        engine._mock_api.query_order.return_value = {'status': 'cancelled'}

        result = engine._renew_local(engine._config.get_cert(7106), engine._mock_api)

        assert result is False
        engine._mock_api.submit_csr.assert_not_called()
        meta = engine._config.get_cert(7106)['metadata']
        # 订单状态只进 last_order_status；在途标记保持不动，本路径恒为「只查询」
        assert meta['last_order_status'] == 'cancelled'
        assert meta['last_issue_state'] == 'processing'
        assert meta['issue_retry_count'] == 2

    def test_processing_approving_treated_as_waiting(self, engine):
        """processing 查询到短暂中间态 approving → 视同 processing 继续等待，不判异常不清文件"""
        cert = self._add_local(engine, 7107)
        engine._save_pending_key(cert, 'KEY')
        engine._save_pending_csr(cert, 'CSR')
        engine._config.update_metadata(7107, {
            'issue_retry_count': 1,
            'last_issue_state': 'processing',
            'pending_verify_paths': ['/tmp/v.txt'],
        })
        engine._mock_api.query_order.return_value = {'status': 'approving'}

        result = engine._renew_local(engine._config.get_cert(7107), engine._mock_api)

        assert result is False
        engine._mock_api.submit_csr.assert_not_called()
        meta = engine._config.get_cert(7107)['metadata']
        assert meta['last_issue_state'] == 'processing'
        assert meta['pending_verify_paths'] == ['/tmp/v.txt']  # 验证文件记录未被清理
        engine._logger.error.assert_not_called()

    def test_recovery_approving_normalizes_processing(self, engine):
        """在途 CSR + 服务端返回 approving → 归一 processing（不重复 POST、不增计数）"""
        cert = self._add_local(engine, 7108)
        engine._save_pending_key(cert, 'KEY')
        engine._save_pending_csr(cert, 'CSR')
        engine._config.update_metadata(7108, {'issue_retry_count': 1})
        engine._mock_api.query_order.return_value = {'status': 'approving'}

        result = engine._renew_local(engine._config.get_cert(7108), engine._mock_api)

        assert result is False
        engine._mock_api.submit_csr.assert_not_called()
        meta = engine._config.get_cert(7108)['metadata']
        assert meta['last_issue_state'] == 'processing'
        assert meta['issue_retry_count'] == 1

    def test_terminal_status_unchanged_no_repeat_error_or_persist(self, engine):
        """订单终态持续多轮：仅首轮记 error 并落盘，后续轮次不重复记 error、不重复写 metadata"""
        cert = self._add_local(engine, 7109)
        engine._save_pending_key(cert, 'KEY')
        engine._save_pending_csr(cert, 'CSR')
        engine._config.update_metadata(7109, {
            'issue_retry_count': 2,
            'last_issue_state': 'processing',
        })
        engine._mock_api.query_order.return_value = {'status': 'cancelled'}

        engine._renew_local(engine._config.get_cert(7109), engine._mock_api)  # 首轮：状态首次变化
        assert engine._logger.error.call_count == 1
        meta = engine._config.get_cert(7109)['metadata']
        assert meta['last_order_status'] == 'cancelled'
        assert meta['last_issue_state'] == 'processing'

        with patch.object(engine._config, 'update_metadata') as mock_update:
            engine._renew_local(engine._config.get_cert(7109), engine._mock_api)  # 次轮：状态未变化
            mock_update.assert_not_called()
        assert engine._logger.error.call_count == 1  # 未重复记 error
        engine._mock_api.submit_csr.assert_not_called()

    @patch('lib.renew.cert_utils.generate_csr')
    def test_ten_definitive_failures_then_cap_no_eleventh(self, mock_csr, engine):
        """签发定义性失败连续 10 轮：计数递增至 10，第 11 轮 CAPPED(issue) 无第 11 次提交（§4.1.1）"""
        from lib.api_client import APIError
        mock_csr.return_value = ('CSR', 'KEY', 'h')
        self._add_local(engine, 7300)
        engine._config.update_metadata(7300, _make_cert_entry(10, order_id=7300)['metadata'])
        engine._mock_api.submit_csr.side_effect = APIError('订单不可提交', code=40001)
        for _ in range(10):
            engine.check_and_renew_all()
        assert engine._mock_api.submit_csr.call_count == 10
        assert engine._config.get_cert(7300)['metadata']['issue_retry_count'] == MAX_ISSUE_RETRY_COUNT
        engine.check_and_renew_all()  # 第 11 轮
        assert engine._mock_api.submit_csr.call_count == 10  # 无第 11 次提交
        meta = engine._config.get_cert(7300)['metadata']
        assert meta['last_issue_state'] == 'CAPPED'
        assert meta['capped_phase'] == 'issue'


class TestCallbackOwnershipConvergence:
    """回调所有权收敛：自动续签底层不发回调，编排层结果落盘后统一发一次"""

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_orchestrator_sends_single_success_callback(self, mock_set_ssl, mock_cert_utils, tmp_data_dir):
        from lib.deployer import Deployer
        config = ConfigManager(tmp_data_dir)
        api = MagicMock()
        api.last_renew_before_days = 0
        api.callback.return_value = {'code': 1}
        api.query_order.return_value = {
            'status': 'active', 'certificate': '---C---',
            'ca_certificate': '---CA---', 'private_key': '---K---'}
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = {
            'not_after': datetime(2035, 1, 1, tzinfo=timezone.utc), 'serial': 'X'}
        mock_set_ssl.return_value = {'status': True}
        deployer = Deployer(config, None, MagicMock())  # 无 site_manager
        engine = RenewEngine(config, MagicMock(return_value=api), deployer, MagicMock())
        config.add_cert(order_id=6401, cert_name='order-6401',
                        domains=['example.com'], site_names=['example.com'])
        config.update_metadata(6401, _make_cert_entry(10, order_id=6401)['metadata'])

        result = engine._renew_pull(config.get_cert(6401), api)
        assert result is True
        # 恰好一次回调（编排层发），success
        api.callback.assert_called_once()
        assert api.callback.call_args.kwargs['status'] == 'success'
        # 部署成功后 deploy_attempt_count 清零、started 清除
        meta = config.get_cert(6401)['metadata']
        assert meta['deploy_attempt_count'] == 0
        assert meta.get('deploy_started') is False


class TestDeployEnvironmentGate:
    """Web 配置损坏时的环境闸门：不计数、发回调、修好即自愈（B2）

    核心风险：checkWebConfig 天然全局，任何无关站点的坏配置都会命中。若按部署尝试
    计数，10 轮后全部证书静默进入 CAPPED；不计数则配置修好后自动恢复。
    """

    @pytest.fixture
    def engine(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        api_factory = MagicMock(return_value=mock_api)
        deployer = MagicMock()
        deployer.deploy_multi.return_value = [
            {'site_name': 'example.com', 'status': True, 'message': '部署成功'}]
        eng = RenewEngine(config, api_factory, deployer, MagicMock())
        eng._mock_api = mock_api
        return eng

    def _seed(self, engine, order_id=7100):
        cert = _make_cert_entry(10, order_id=order_id)
        engine._config.add_cert(
            order_id=cert['order_id'], cert_name=cert['cert_name'],
            domains=cert['domains'], site_names=cert['site_name'],
        )
        engine._config.update_metadata(cert['order_id'], cert['metadata'])
        engine._mock_api.query_order.return_value = {
            'status': 'active',
            'certificate': '---CERT---',
            'ca_certificate': '---CA---',
            'private_key': '---KEY---',
        }
        return engine._config.get_cert(cert['order_id'])

    @staticmethod
    def _break_config(monkeypatch, broken=True):
        import public
        monkeypatch.setattr(
            public, 'checkWebConfig',
            (lambda: 'nginx: [emerg] unrelated site broken') if broken else (lambda: True))

    def test_blocked_does_not_deploy_or_count(self, engine, monkeypatch):
        self._break_config(monkeypatch)
        cert = self._seed(engine)
        # 已按 failure 上报服务端，本地口径必须一致：本轮计为失败而非等待
        with pytest.raises(RuntimeError, match='Web 服务配置校验失败'):
            engine._renew_pull(cert, engine._mock_api)
        engine._deployer.deploy_multi.assert_not_called()
        meta = engine._config.get_cert(cert['order_id'])['metadata']
        assert meta.get('deploy_attempt_count', 0) == 0
        assert meta.get('deploy_started', False) is False

    def test_blocked_reports_failure_callback(self, engine, monkeypatch):
        """必须上报：服务端按「订单最新一条仍为 failure」做状态判定并按 TTL 提醒，
        不上报会让该订单从服务端失败视图消失，才是真正的静默过期"""
        self._break_config(monkeypatch)
        cert = self._seed(engine, 7101)
        with pytest.raises(RuntimeError):
            engine._renew_pull(cert, engine._mock_api)
        engine._mock_api.callback.assert_called_once()
        kwargs = engine._mock_api.callback.call_args[1]
        assert kwargs['status'] == 'failure'
        assert 'Web 服务配置校验失败' in kwargs['message']

    def test_blocked_records_reason_for_panel(self, engine, monkeypatch):
        self._break_config(monkeypatch)
        cert = self._seed(engine, 7102)
        with pytest.raises(RuntimeError):
            engine._renew_pull(cert, engine._mock_api)
        meta = engine._config.get_cert(cert['order_id'])['metadata']
        assert 'nginx: [emerg] unrelated site broken' in meta['last_deploy_block_reason']
        assert meta['last_deploy_block_at']

    def test_many_blocked_rounds_never_cap(self, engine, monkeypatch):
        """连续多轮环境阻断不得推入 CAPPED（否则配置修好还要人工解除）"""
        from lib.config import MAX_DEPLOY_ATTEMPT_COUNT, TERMINAL_ISSUE_STATES

        self._break_config(monkeypatch)
        cert = self._seed(engine, 7103)
        for _ in range(MAX_DEPLOY_ATTEMPT_COUNT * 2):
            with pytest.raises(RuntimeError):
                engine._renew_pull(engine._config.get_cert(cert['order_id']), engine._mock_api)
        meta = engine._config.get_cert(cert['order_id'])['metadata']
        assert meta.get('deploy_attempt_count', 0) == 0
        assert meta.get('last_issue_state', '') not in TERMINAL_ISSUE_STATES

    def test_recovers_after_config_fixed(self, engine, monkeypatch):
        """配置修复后下一轮正常部署，无需任何人工干预"""
        self._break_config(monkeypatch)
        cert = self._seed(engine, 7104)
        with pytest.raises(RuntimeError):
            engine._renew_pull(engine._config.get_cert(cert['order_id']), engine._mock_api)
        engine._deployer.deploy_multi.assert_not_called()
        assert engine._config.get_cert(cert['order_id'])['metadata']['last_deploy_block_reason']

        self._break_config(monkeypatch, broken=False)
        result = engine._renew_pull(engine._config.get_cert(cert['order_id']), engine._mock_api)
        assert result is True
        engine._deployer.deploy_multi.assert_called_once()
        # 环境恢复后阻断标记必须清除，否则面板会一直显示已消失的旧原因
        meta = engine._config.get_cert(cert['order_id'])['metadata']
        assert meta['last_deploy_block_reason'] == ''
        assert meta['last_deploy_block_at'] == ''

    def test_blocked_round_counts_as_failure_in_summary(self, engine, monkeypatch):
        """环境阻断在续签汇总里记为失败：与上报服务端的 failure 口径一致，
        不能让面板显示「续签成功」而实际什么都没发生"""
        self._break_config(monkeypatch)
        self._seed(engine, 7106)
        results = engine._do_renew_all()
        assert len(results) == 1
        assert results[0]['status'] == 'failure'
        assert 'Web 服务配置校验失败' in results[0]['message']

    def test_block_reason_is_single_line_and_capped(self, engine, monkeypatch):
        """nginx -t 的多行输出压平为单行并限长，避免撑爆面板布局"""
        import public
        monkeypatch.setattr(public, 'checkWebConfig', lambda: 'line one\nline two\n' + 'x' * 500)
        cert = self._seed(engine, 7107)
        with pytest.raises(RuntimeError):
            engine._renew_pull(cert, engine._mock_api)
        reason = engine._config.get_cert(cert['order_id'])['metadata']['last_deploy_block_reason']
        assert '\n' not in reason
        assert 'line one line two' in reason
        assert len(reason) < 400

    def test_processing_path_keeps_pending_key(self, engine, monkeypatch):
        """local 模式 processing 路径被阻断时保留 pending key 待下轮"""
        cert = _make_cert_entry(10, renew_mode='local', issue_state='processing', order_id=7105)
        engine._config.add_cert(
            order_id=cert['order_id'], cert_name=cert['cert_name'],
            domains=cert['domains'], site_names=cert['site_name'], renew_mode='local',
        )
        engine._config.update_metadata(cert['order_id'], cert['metadata'])
        engine._mock_api.query_order.return_value = {
            'status': 'active', 'certificate': '---CERT---', 'ca_certificate': '---CA---',
        }
        entry = engine._config.get_cert(cert['order_id'])
        engine._save_pending_key(entry, '---PENDING-KEY---')
        self._break_config(monkeypatch)

        with pytest.raises(RuntimeError, match='Web 服务配置校验失败'):
            engine._handle_processing(entry, engine._mock_api)
        assert engine._read_pending_key(entry) == '---PENDING-KEY---'


class TestPendingKeyDanglingSymlink:
    """悬空符号链接不清理会让 O_EXCL 创建每轮失败（D2）"""

    @pytest.fixture
    def engine(self, tmp_data_dir):
        return RenewEngine(ConfigManager(tmp_data_dir), MagicMock(), MagicMock(), MagicMock())

    def test_save_overwrites_dangling_symlink(self, engine):
        cert = _make_cert_entry(10, order_id=7200)
        path = engine._pending_key_path(cert)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(os.path.join(os.path.dirname(path), 'nonexistent-target.pem'), path)

        engine._save_pending_key(cert, '---KEY---')
        assert not os.path.islink(path)
        assert engine._read_pending_key(cert) == '---KEY---'

    def test_cleanup_removes_dangling_symlink(self, engine):
        cert = _make_cert_entry(10, order_id=7201)
        path = engine._pending_key_path(cert)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(os.path.join(os.path.dirname(path), 'nonexistent-target.pem'), path)

        engine._cleanup_pending_key(cert)
        assert not os.path.lexists(path)


class TestPanelRuntimeGate:
    """运行环境闸门：宝塔运行时不可用时整轮中止且零回调（F6/C1）

    核心风险：cron 脚本在面板解释器失效时回退系统 python3，lib/ 全是标准库所以插件
    照常启动、API 照常查询，直到部署闸门里 import public 才失败——而那时回调发不出、
    计数不递增，服务端与面板两侧都看不见。
    """

    @pytest.fixture
    def engine(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        deployer = MagicMock()
        eng = RenewEngine(config, MagicMock(return_value=mock_api), deployer, MagicMock())
        eng._mock_api = mock_api
        return eng

    @staticmethod
    def _seed(engine, order_id=7300):
        cert = _make_cert_entry(10, order_id=order_id)
        engine._config.add_cert(
            order_id=cert['order_id'], cert_name=cert['cert_name'],
            domains=cert['domains'], site_names=cert['site_name'],
        )
        engine._config.update_metadata(cert['order_id'], cert['metadata'])
        return cert

    def test_runtime_unavailable_aborts_without_any_callback(self, engine, tmp_data_dir):
        """运行时不可用：整轮中止、零回调、零 API 查询"""
        self._seed(engine)
        with patch('lib.renew.probe_panel_runtime', return_value='宝塔运行时模块不可用: public(x)'):
            results = engine.check_and_renew_all()

        assert results == []
        engine._mock_api.callback.assert_not_called()
        engine._mock_api.query_order.assert_not_called()
        engine._deployer.deploy_multi.assert_not_called()

    def test_abort_reason_distinguishes_from_nothing_to_do(self, engine, tmp_data_dir):
        """中止与"跑完但无需续签"都返回空列表，必须能被调用方区分"""
        self._seed(engine)
        with patch('lib.renew.probe_panel_runtime', return_value='运行时不可用'):
            engine.check_and_renew_all()
        assert engine.last_abort_reason

        # 环境正常且无需续签：同样空列表，但中止原因必须被清空
        engine._config.update_metadata(7300, {'cert_expires_at': (
            datetime.now(timezone.utc) + timedelta(days=60)).strftime('%Y-%m-%dT%H:%M:%SZ')})
        with patch('lib.renew.probe_panel_runtime', return_value=None):
            results = engine.check_and_renew_all()
        assert results == []
        assert engine.last_abort_reason == ''

    def test_abort_writes_reason_into_renew_status(self, engine, tmp_data_dir):
        """面板据此告警：last_run 是新鲜的，不能让面板误判为健康"""
        self._seed(engine)
        with patch('lib.renew.probe_panel_runtime', return_value='运行时不可用: public'):
            engine.check_and_renew_all()

        with open(os.path.join(tmp_data_dir, 'renew_status.json')) as f:
            status = json.load(f)
        assert status['aborted_reason']
        assert status['last_run']
        assert status['total'] == 0

    def test_healthy_runtime_leaves_reason_empty(self, engine, tmp_data_dir):
        """正常运行时 aborted_reason 必须为空，否则面板永久告警"""
        with patch('lib.renew.probe_panel_runtime', return_value=None):
            engine.check_and_renew_all()
        with open(os.path.join(tmp_data_dir, 'renew_status.json')) as f:
            assert json.load(f)['aborted_reason'] == ''

    def test_probe_detects_missing_module(self):
        """探测函数本身：模块缺失时返回带根因提示的错误串"""
        from lib.deployer import probe_panel_runtime
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == 'public':
                raise ImportError("No module named 'psutil'")
            return real_import(name, *a, **kw)

        with patch('builtins.__import__', side_effect=fake_import):
            err = probe_panel_runtime()
        assert err and 'public' in err and 'psutil' in err
        assert '解释器' in err

    def test_probe_returns_none_when_available(self):
        from lib.deployer import probe_panel_runtime
        assert probe_panel_runtime() is None


class TestEnvBlockCallbackEdgeTriggered:
    """环境阻断回调改为变化触发（F6）

    服务端 DeployFailureReminderCommand 是电平驱动（判据为"订单最新一行仍为 failure"），
    一行就足以让订单永久留在失败视图；逐日重发零信息增量，却会淹没管理端列表，
    且因 pull 单 latestCert 恒 active 而被 PurgeCommand 的终态过滤永久保留。
    """

    @pytest.fixture
    def engine(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        deployer = MagicMock()
        deployer.deploy_multi.return_value = [
            {'site_name': 'example.com', 'status': True, 'message': '部署成功'}]
        eng = RenewEngine(config, MagicMock(return_value=mock_api), deployer, MagicMock())
        eng._mock_api = mock_api
        return eng

    def _seed(self, engine, order_id=7400):
        cert = _make_cert_entry(10, order_id=order_id)
        engine._config.add_cert(
            order_id=cert['order_id'], cert_name=cert['cert_name'],
            domains=cert['domains'], site_names=cert['site_name'],
        )
        engine._config.update_metadata(cert['order_id'], cert['metadata'])
        engine._mock_api.query_order.return_value = {
            'status': 'active', 'certificate': '---CERT---',
            'ca_certificate': '---CA---', 'private_key': '---KEY---',
        }
        return engine._config.get_cert(cert['order_id'])

    @staticmethod
    def _set_config_error(monkeypatch, message):
        import public
        monkeypatch.setattr(public, 'checkWebConfig', lambda: message)

    def test_repeated_identical_block_reports_once(self, engine, monkeypatch):
        """同一原因连续多轮只上报一次"""
        self._set_config_error(monkeypatch, 'nginx: [emerg] same error')
        cert = self._seed(engine)
        for _ in range(5):
            with pytest.raises(RuntimeError):
                engine._renew_pull(engine._config.get_cert(cert['order_id']), engine._mock_api)
        assert engine._mock_api.callback.call_count == 1

    def test_changed_reason_reports_again(self, engine, monkeypatch):
        """原因变化即上报：服务端需要拿到新信息"""
        self._set_config_error(monkeypatch, 'nginx: [emerg] first error')
        cert = self._seed(engine)
        with pytest.raises(RuntimeError):
            engine._renew_pull(engine._config.get_cert(cert['order_id']), engine._mock_api)

        self._set_config_error(monkeypatch, 'nginx: [emerg] second different error')
        with pytest.raises(RuntimeError):
            engine._renew_pull(engine._config.get_cert(cert['order_id']), engine._mock_api)
        assert engine._mock_api.callback.call_count == 2

    def test_recovery_then_reblock_reports_again(self, engine, monkeypatch):
        """恢复后复发必须再报：_clear_deploy_block 清空原因，复发即构成变化"""
        import public
        self._set_config_error(monkeypatch, 'nginx: [emerg] boom')
        cert = self._seed(engine)
        with pytest.raises(RuntimeError):
            engine._renew_pull(engine._config.get_cert(cert['order_id']), engine._mock_api)
        assert engine._mock_api.callback.call_count == 1

        monkeypatch.setattr(public, 'checkWebConfig', lambda: True)
        engine._renew_pull(engine._config.get_cert(cert['order_id']), engine._mock_api)

        self._set_config_error(monkeypatch, 'nginx: [emerg] boom')
        with pytest.raises(RuntimeError):
            engine._renew_pull(engine._config.get_cert(cert['order_id']), engine._mock_api)
        # 第 2 次阻断上报 + 中间那次部署成功的回调由编排层发出
        assert engine._mock_api.callback.call_count >= 2

    def test_check_web_config_raising_still_blocks_visibly(self, engine, monkeypatch):
        """check_web_config 抛异常与返回错误同样处置：上报 + 落盘 + 不计数。
        裸调用会让异常穿透到通用 except，届时回调发不出、原因不落盘、计数停在 0 而永不触顶。
        """
        import public

        def boom():
            raise RuntimeError('checkWebConfig 内部炸了')

        monkeypatch.setattr(public, 'checkWebConfig', boom)
        cert = self._seed(engine, 7401)
        with pytest.raises(RuntimeError):
            engine._renew_pull(engine._config.get_cert(cert['order_id']), engine._mock_api)

        engine._mock_api.callback.assert_called_once()
        assert engine._mock_api.callback.call_args[1]['status'] == 'failure'
        meta = engine._config.get_cert(cert['order_id'])['metadata']
        assert 'Web 配置检查执行异常' in meta['last_deploy_block_reason']
        assert meta.get('deploy_attempt_count', 0) == 0
        engine._deployer.deploy_multi.assert_not_called()


class TestConfigDegradedAbortsRenew:
    """配置降级时续签整轮中止（F2/A2）

    此时 get_certs 恒为空，照常跑完会写出全 0 的新鲜 renew_status，
    与"确实无需续签"在面板上不可区分——正是要消除的健康假象。
    """

    def test_degraded_config_aborts_with_reason(self, tmp_data_dir):
        with open(os.path.join(tmp_data_dir, 'config.json'), 'w') as f:
            f.write('{ broken')

        config = ConfigManager(tmp_data_dir)
        assert config.is_degraded() is True

        mock_api = MagicMock()
        engine = RenewEngine(config, MagicMock(return_value=mock_api), MagicMock(), MagicMock())
        with patch('lib.renew.probe_panel_runtime', return_value=None):
            results = engine.check_and_renew_all()

        assert results == []
        assert '配置文件损坏' in engine.last_abort_reason
        mock_api.callback.assert_not_called()

        with open(os.path.join(tmp_data_dir, 'renew_status.json')) as f:
            assert json.load(f)['aborted_reason']


class TestCollectIsolation:
    """阶段 1 逐证书异常隔离（F7/C2）

    核心风险：阶段 1 此前一行 try 都没有，而 APIClient.__init__ 会对 SSRF 命中、
    token 非法、协议非法主动 raise ValueError。一张证书构造失败会让整批后续证书
    连一次 API 调用都没有，且 renew_status 保留上一轮的成功记录，面板完全健康。
    """

    @pytest.fixture
    def engine(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        deployer = MagicMock()
        deployer.deploy_multi.return_value = [
            {'site_name': 'example.com', 'status': True, 'message': '部署成功'}]
        return config, deployer

    def test_one_bad_cert_does_not_block_others(self, engine, tmp_data_dir):
        config, deployer = engine
        # 每张证书用独立站点：add_cert 有站点唯一绑定校验，共用站点会被静默过滤成空绑定
        for oid in (9001, 9002, 9003):
            c = _make_cert_entry(10, order_id=oid)
            site = 'site%d.example.com' % oid
            config.add_cert(order_id=oid, cert_name='order-%d' % oid,
                            domains=c['domains'], site_names=[site])
            config.update_metadata(oid, c['metadata'])

        built = []

        def factory(cert):
            if cert['order_id'] == 9001:
                raise ValueError('API 地址指向内网，已拒绝')
            api = MagicMock()
            api.query_order.return_value = {
                'status': 'active', 'certificate': '---C---',
                'ca_certificate': '---CA---', 'private_key': '---K---'}
            built.append(cert['order_id'])
            return api

        deployer.deploy_multi.side_effect = lambda site_names, **kw: [
            {'site_name': s, 'status': True, 'message': '部署成功'} for s in site_names]

        eng = RenewEngine(config, factory, deployer, MagicMock())
        with patch('lib.renew.probe_panel_runtime', return_value=None):
            results = eng.check_and_renew_all()

        assert built == [9002, 9003], '后续证书必须照常处理'
        by_id = {r['order_id']: r for r in results}
        assert by_id[9001]['status'] == 'failure'
        assert '预处理失败' in by_id[9001]['message']
        assert by_id[9002]['status'] == 'success'

    def test_bad_cert_appears_in_renew_status(self, engine, tmp_data_dir):
        """预处理失败必须进汇总：否则 renew_status 保留上一轮成功记录，面板看不出异常"""
        config, deployer = engine
        c = _make_cert_entry(10, order_id=9101)
        config.add_cert(order_id=9101, cert_name='order-9101',
                        domains=c['domains'], site_names=['site9101.example.com'])
        config.update_metadata(9101, c['metadata'])

        def factory(cert):
            raise ValueError('token 格式非法')

        eng = RenewEngine(config, factory, deployer, MagicMock())
        with patch('lib.renew.probe_panel_runtime', return_value=None):
            eng.check_and_renew_all()

        with open(os.path.join(tmp_data_dir, 'renew_status.json')) as f:
            status = json.load(f)
        assert status['failure'] == 1
        assert status['total'] == 1


class TestLockWaitAndSkip:
    """抢锁重试与跳过可见性（F11/C6）"""

    @pytest.fixture
    def engine(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        return RenewEngine(config, MagicMock(), MagicMock(), MagicMock())

    def test_lock_held_records_skip_without_touching_renew_status(self, engine, tmp_data_dir):
        """抢锁失败写独立小文件，绝不读改写 renew_status.json

        那条路径按定义就是没拿到锁的进程，与正在收尾的持锁进程并发读改写，
        会用陈旧快照覆盖对方刚写的真实计数。
        """
        status_path = os.path.join(tmp_data_dir, 'renew_status.json')
        with open(status_path, 'w') as f:
            json.dump({'last_run': '2020-01-01T00:00:00Z', 'total': 9,
                       'success': 9, 'pending': 0, 'failure': 0}, f)
        with open(status_path) as f:
            original = f.read()

        holder = open(os.path.join(tmp_data_dir, 'renew.lock'), 'w')
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            results = engine.check_and_renew_all(lock_wait=0)
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()

        assert results == []
        assert engine.last_abort_reason, '被锁挡下必须能与"无需续签"区分'
        with open(status_path) as f:
            assert f.read() == original, 'renew_status 不得被覆盖'
        assert os.path.exists(os.path.join(tmp_data_dir, 'renew_lock_skip.json'))

    def test_lock_wait_retries_until_released(self, engine, tmp_data_dir):
        """cron 可以等锁：丢掉当天唯一的续签窗口，代价远大于多等几秒"""
        holder = open(os.path.join(tmp_data_dir, 'renew.lock'), 'w')
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

        released = []

        def release_soon():
            time.sleep(0.3)
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
            released.append(True)

        t = threading.Thread(target=release_soon)
        t.start()
        with patch('lib.renew.probe_panel_runtime', return_value=None), \
             patch('lib.renew.LOCK_RETRY_INTERVAL', 0.1):
            engine.check_and_renew_all(lock_wait=5)
        t.join()

        assert released, '锁应已被释放'
        assert engine.last_abort_reason == '', '等到锁之后应正常执行'

    def test_unopenable_lock_aborts_visibly(self, engine, tmp_data_dir, monkeypatch):
        """锁文件打不开时不得整轮静默消失"""
        def boom(*a, **kw):
            raise OSError('read-only file system')

        monkeypatch.setattr(os, 'open', boom)
        results = engine.check_and_renew_all()
        assert results == []
        assert '锁文件' in engine.last_abort_reason


class TestCertUnchangedDetection:
    """证书更替检测（F9/C4）

    核心风险：deploy_multi 从不比对 cert_serial，服务端反复返回同一张旧证书时
    每轮都报 success，服务端最新一行永远是成功、面板一路「正常」→「已过期」。
    """

    @pytest.fixture
    def engine(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        mock_api = MagicMock()
        eng = RenewEngine(config, MagicMock(return_value=mock_api), MagicMock(), MagicMock())
        eng._mock_api = mock_api
        return eng

    @staticmethod
    def _cert(engine, order_id, serial):
        engine._config.add_cert(order_id=order_id, cert_name='order-%d' % order_id,
                                domains=['a.com'], site_names=['s%d.com' % order_id])
        engine._config.update_metadata(order_id, {'cert_serial': serial})
        return engine._config.get_cert(order_id)

    def test_same_serial_twice_reports_failure(self, engine):
        cert = self._cert(engine, 9501, 'AABB')
        # 两端序列号由调用方传入（prev, new）——本方法不读 metadata
        assert engine._track_cert_unchanged(cert, 9501, 'AABB', 'AABB') == ''   # 第 1 轮仅计数
        assert '未实际更新' in engine._track_cert_unchanged(cert, 9501, 'AABB', 'AABB')

    def test_changed_serial_resets_counter(self, engine):
        cert = self._cert(engine, 9502, 'AABB')
        engine._track_cert_unchanged(cert, 9502, 'AABB', 'AABB')
        assert cert['metadata']['unchanged_cert_rounds'] == 1
        # 新证书：计数归零，绝不能累积到误报
        assert engine._track_cert_unchanged(cert, 9502, 'AABB', 'CCDD') == ''
        assert cert['metadata']['unchanged_cert_rounds'] == 0

    def test_empty_serial_never_triggers(self, engine):
        """两端都要非空：解析失败返回 ''，老 OpenSSL 上 '' == '' 会让每次部署都误报"""
        cert = self._cert(engine, 9503, '')
        for _ in range(5):
            assert engine._track_cert_unchanged(cert, 9503, '', '') == ''
        assert cert['metadata'].get('unchanged_cert_rounds', 0) == 0

    def test_partial_failure_round_does_not_accumulate(self, engine):
        """部分失败轮次不写 cert_serial，prev 与 new 不同 → 不累积（F8×F9 无假阳性）"""
        cert = self._cert(engine, 9504, 'AABB')
        # 部分失败：deploy_multi 未写 cert_serial，metadata 仍是旧值，但 prev 取的是部署前值
        assert engine._track_cert_unchanged(cert, 9504, '', 'AABB') == ''
        assert cert['metadata'].get('unchanged_cert_rounds', 0) == 0


class TestExpiredSelfHealing:
    """EXPIRED 自动解除（F16/D4）

    一次时钟前跳就把还有几十天有效期的证书永久打成「已过期停更」，且标签本身是错的。
    解除逻辑必须在终态 continue 之前，否则永远执行不到。
    """

    @pytest.fixture
    def engine(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        return RenewEngine(config, MagicMock(return_value=MagicMock()), MagicMock(), MagicMock())

    def test_expired_state_cleared_when_cert_actually_valid(self, engine):
        exp = (datetime.now(timezone.utc) + timedelta(days=60)).strftime('%Y-%m-%dT%H:%M:%SZ')
        engine._config.add_cert(order_id=9601, cert_name='order-9601',
                                domains=['a.com'], site_names=['s.com'])
        engine._config.update_metadata(9601, {
            'cert_expires_at': exp, 'last_issue_state': 'EXPIRED'})

        pending = []
        engine._collect_one(engine._config.get_cert(9601), pending)
        assert engine._config.get_cert(9601)['metadata']['last_issue_state'] == ''

    def test_truly_expired_stays_terminal(self, engine):
        exp = (datetime.now(timezone.utc) - timedelta(days=3)).strftime('%Y-%m-%dT%H:%M:%SZ')
        engine._config.add_cert(order_id=9602, cert_name='order-9602',
                                domains=['a.com'], site_names=['s2.com'])
        engine._config.update_metadata(9602, {
            'cert_expires_at': exp, 'last_issue_state': 'EXPIRED'})

        engine._collect_one(engine._config.get_cert(9602), [])
        assert engine._config.get_cert(9602)['metadata']['last_issue_state'] == 'EXPIRED'

    def test_unparseable_expiry_stays_terminal(self, engine):
        """元数据损坏时保持终态：否则真过期的证书会反复启动新动作，违反 spec §3.2"""
        engine._config.add_cert(order_id=9603, cert_name='order-9603',
                                domains=['a.com'], site_names=['s3.com'])
        engine._config.update_metadata(9603, {
            'cert_expires_at': 'not-a-date', 'last_issue_state': 'EXPIRED'})

        engine._collect_one(engine._config.get_cert(9603), [])
        assert engine._config.get_cert(9603)['metadata']['last_issue_state'] == 'EXPIRED'

    def test_clearing_does_not_reset_counts(self, engine):
        """只清状态不动计数：清了会绕过触顶保护"""
        exp = (datetime.now(timezone.utc) + timedelta(days=60)).strftime('%Y-%m-%dT%H:%M:%SZ')
        engine._config.add_cert(order_id=9604, cert_name='order-9604',
                                domains=['a.com'], site_names=['s4.com'])
        engine._config.update_metadata(9604, {
            'cert_expires_at': exp, 'last_issue_state': 'EXPIRED',
            'issue_retry_count': 7, 'deploy_attempt_count': 5})

        engine._collect_one(engine._config.get_cert(9604), [])
        meta = engine._config.get_cert(9604)['metadata']
        assert meta['issue_retry_count'] == 7
        assert meta['deploy_attempt_count'] == 5


class TestVerifyFileRetry:
    """验证文件放置判据（F13/D1）"""

    @pytest.fixture
    def engine(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        verifier = MagicMock()
        eng = RenewEngine(config, MagicMock(), MagicMock(), MagicMock(), verifier)
        eng._verifier = verifier
        return eng

    def test_retries_when_previous_placement_failed(self, engine):
        """首轮放置失败后必须重试：此前 pending_file_verify 照样落盘，
        之后每轮都被短路，文件一次都没写进去而订单永远卡在 processing"""
        engine._verifier.place_file.return_value = []
        cert = {'order_id': 1, 'site_name': ['a.com'],
                'metadata': {'pending_file_verify': {'path': '.well-known/x', 'content': 'c'},
                             'pending_verify_paths': []}}
        engine._config.add_cert(order_id=1, cert_name='order-1',
                                domains=['a.com'], site_names=['a.com'])
        engine._try_place_verify_file(cert, {'file': {'path': '.well-known/x', 'content': 'c'}})
        engine._verifier.place_file.assert_called_once()

    def test_skips_when_files_intact(self, engine, tmp_data_dir):
        """真的放上去了才短路"""
        real = os.path.join(tmp_data_dir, 'placed.txt')
        with open(real, 'w') as f:
            f.write('x')
        info = {'path': '.well-known/x', 'content': 'c'}
        cert = {'order_id': 2, 'site_name': ['a.com'],
                'metadata': {'pending_file_verify': info, 'pending_verify_paths': [real]}}
        engine._try_place_verify_file(cert, {'file': info})
        engine._verifier.place_file.assert_not_called()

    def test_retries_when_file_disappeared(self, engine, tmp_data_dir):
        """文件被清理/站点重建后消失也要重放"""
        info = {'path': '.well-known/x', 'content': 'c'}
        cert = {'order_id': 3, 'site_name': ['a.com'],
                'metadata': {'pending_file_verify': info,
                             'pending_verify_paths': [os.path.join(tmp_data_dir, 'gone.txt')]}}
        engine._config.add_cert(order_id=3, cert_name='order-3',
                                domains=['a.com'], site_names=['a.com'])
        engine._verifier.place_file.return_value = ['/tmp/x']
        engine._try_place_verify_file(cert, {'file': info})
        engine._verifier.place_file.assert_called_once()

    def test_partial_coverage_retries(self, engine, tmp_data_dir):
        """多站点只放上一个：CA 可能恰好去验失败的那个，必须重试"""
        real = os.path.join(tmp_data_dir, 'one.txt')
        with open(real, 'w') as f:
            f.write('x')
        info = {'path': '.well-known/x', 'content': 'c'}
        cert = {'order_id': 4, 'site_name': ['a.com', 'b.com'],
                'metadata': {'pending_file_verify': info, 'pending_verify_paths': [real]}}
        engine._config.add_cert(order_id=4, cert_name='order-4',
                                domains=['a.com'], site_names=['a.com'])
        engine._verifier.place_file.return_value = [real]
        engine._try_place_verify_file(cert, {'file': info})
        engine._verifier.place_file.assert_called_once()


class TestBatchFairness:
    """批次选择公平性（F15/D3）

    核心风险：卡在 processing 的证书每轮都进列表且不会自行退出，
    按配置顺序截断会让第 101 张之后的证书确定性饿死。
    """

    @pytest.fixture
    def engine(self, tmp_data_dir):
        return RenewEngine(ConfigManager(tmp_data_dir), MagicMock(), MagicMock(), MagicMock())

    @staticmethod
    def _item(oid, state='', hours=240, last_attempt=''):
        exp = (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
        return ({'order_id': oid, 'metadata': {
            'last_issue_state': state, 'cert_expires_at': exp,
            'last_attempt_at': last_attempt}}, MagicMock(), 'pull')

    def test_processing_cannot_starve_the_tail(self, engine):
        """100 张卡 processing + 5 张待续签：尾部必须进得来"""
        items = [self._item(i, state='processing', hours=100) for i in range(100)]
        items += [self._item(1000 + i, hours=300) for i in range(5)]

        picked = engine._select_batch(items)
        picked_ids = {i[0]['order_id'] for i in picked}
        assert len(picked) == 100
        for i in range(5):
            assert 1000 + i in picked_ids, '待续签证书必须进入本批次'

    def test_processing_quota_is_capped(self, engine):
        items = [self._item(i, state='processing') for i in range(200)]
        items += [self._item(1000 + i) for i in range(60)]
        picked = engine._select_batch(items)
        n_processing = sum(1 for i in picked
                           if i[0]['metadata']['last_issue_state'] == 'processing')
        assert n_processing <= 50

    def test_rotation_prefers_least_recently_attempted(self, engine):
        """轮转保证 ceil(N/100) 轮内全部触达"""
        old = [self._item(i, last_attempt='2020-01-01T00:00:00Z') for i in range(60)]
        recent = [self._item(500 + i, last_attempt='2030-01-01T00:00:00Z') for i in range(60)]
        picked = engine._select_batch(recent + old)
        picked_ids = {i[0]['order_id'] for i in picked}
        assert all(i in picked_ids for i in range(60)), '久未尝试的证书应优先'

    def test_under_limit_returns_all(self, engine):
        items = [self._item(i) for i in range(10)]
        assert engine._select_batch(items) == items


def _backdate_no_progress(config, order_id, days):
    """把停更计时起点往前拨，模拟真实经过的天数（cron 每天才跑一次）"""
    past = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
    config.update_metadata(order_id, {'no_progress_since': past})


class TestNoProgressBound:
    """纯 GET 轮询的绝对边界（spec §3.2）

    轮询按 spec 不计入任何尝试计数，此前唯一的边界是到期闸门——而 _remaining_hours()
    在 cert_expires_at 为空时返回 None，整段闸门被跳过。新增证书默认就是空到期时间
    且只有全部站点部署成功才回填，所以"从未成功部署过"的证书会每天空查询到永远，
    pending 私钥同步永驻磁盘。
    """

    def _engine(self, tmp_data_dir, api):
        return RenewEngine(ConfigManager(tmp_data_dir), MagicMock(return_value=api),
                           MagicMock(), MagicMock())

    def _rounds(self, engine, n):
        with patch('lib.renew.probe_panel_runtime', return_value=None):
            for _ in range(n):
                engine.check_and_renew_all(spread=False, lock_wait=0)

    def test_local_processing_converges_and_clears_key(self, tmp_data_dir):
        """local 卡 processing + 到期时间未知：超时后零拉取、转 CAPPED、清 pending 私钥"""
        api = MagicMock()
        api.last_renew_before_days = 0
        api.query_order.return_value = {'order_id': 9401, 'status': 'processing'}
        engine = self._engine(tmp_data_dir, api)
        engine._config.add_cert(order_id=9401, cert_name='order-9401', domains=['a.com'],
                                site_names=['a.com'], renew_mode='local')
        engine._config.update_metadata(9401, {
            'last_issue_state': 'processing', 'cert_expires_at': ''})
        cert = engine._config.get_cert(9401)
        engine._save_pending_key(cert, 'PENDING-KEY')
        engine._save_pending_csr(cert, 'PENDING-CSR')

        self._rounds(engine, 3)
        _backdate_no_progress(engine._config, 9401, MAX_NO_PROGRESS_DAYS + 1)
        before = api.query_order.call_count
        self._rounds(engine, 50)

        meta = engine._config.get_cert(9401)['metadata']
        assert api.query_order.call_count == before, '超时后不得再拉取'
        assert meta['last_issue_state'] == ISSUE_STATE_CAPPED
        assert meta['capped_phase'] == CAPPED_PHASE_STALLED
        assert not os.path.isfile(engine._pending_key_path(cert)), 'pending 私钥必须清理'
        assert not os.path.isfile(engine._pending_csr_path(cert))

    def test_pull_terminal_order_converges(self, tmp_data_dir):
        """pull 模式订单已 cancelled + 到期时间未知：超时后停止轮询"""
        api = MagicMock()
        api.last_renew_before_days = 0
        api.query_order.return_value = {'order_id': 9402, 'status': 'cancelled'}
        engine = self._engine(tmp_data_dir, api)
        engine._config.add_cert(order_id=9402, cert_name='order-9402', domains=['b.com'],
                                site_names=['b.com'], renew_mode='pull')
        engine._config.update_metadata(9402, {'cert_expires_at': ''})

        self._rounds(engine, 3)
        _backdate_no_progress(engine._config, 9402, MAX_NO_PROGRESS_DAYS + 1)
        before = api.query_order.call_count
        self._rounds(engine, 50)

        meta = engine._config.get_cert(9402)['metadata']
        assert api.query_order.call_count == before
        assert meta['last_issue_state'] == ISSUE_STATE_CAPPED
        assert meta['capped_phase'] == CAPPED_PHASE_STALLED

    def test_under_limit_keeps_polling(self, tmp_data_dir):
        """未达时限必须照常轮询：边界不能提前误伤正常等待签发的证书"""
        api = MagicMock()
        api.last_renew_before_days = 0
        api.query_order.return_value = {'order_id': 9403, 'status': 'processing'}
        engine = self._engine(tmp_data_dir, api)
        engine._config.add_cert(order_id=9403, cert_name='order-9403', domains=['c.com'],
                                site_names=['c.com'], renew_mode='local')
        engine._config.update_metadata(9403, {
            'last_issue_state': 'processing', 'cert_expires_at': ''})

        self._rounds(engine, 2)
        _backdate_no_progress(engine._config, 9403, MAX_NO_PROGRESS_DAYS - 1)
        before = api.query_order.call_count
        self._rounds(engine, 5)

        assert api.query_order.call_count == before + 5
        assert engine._config.get_cert(9403)['metadata']['last_issue_state'] == 'processing'

    def test_clock_jump_reanchors_instead_of_capping(self, tmp_data_dir):
        """时间戳落在时钟合理区间之外：重新锚定，不得据此判定停更"""
        api = MagicMock()
        api.last_renew_before_days = 0
        api.query_order.return_value = {'order_id': 9404, 'status': 'processing'}
        engine = self._engine(tmp_data_dir, api)
        engine._config.add_cert(order_id=9404, cert_name='order-9404', domains=['d.com'],
                                site_names=['d.com'], renew_mode='local')
        engine._config.update_metadata(9404, {
            'last_issue_state': 'processing', 'cert_expires_at': ''})

        self._rounds(engine, 1)
        _backdate_no_progress(engine._config, 9404, 400)
        self._rounds(engine, 3)

        assert engine._config.get_cert(9404)['metadata']['last_issue_state'] == 'processing'

    def test_progress_resets_the_clock(self, tmp_data_dir):
        """订单恢复可用后计时清零，不带着旧的停滞历史进入下一次等待"""
        api = MagicMock()
        api.last_renew_before_days = 0
        api.query_order.return_value = {'order_id': 9405, 'status': 'processing'}
        engine = self._engine(tmp_data_dir, api)
        engine._config.add_cert(order_id=9405, cert_name='order-9405', domains=['e.com'],
                                site_names=['e.com'], renew_mode='pull')
        engine._config.update_metadata(9405, {'cert_expires_at': ''})
        self._rounds(engine, 1)
        assert engine._config.get_cert(9405)['metadata']['no_progress_since']

        api.query_order.return_value = {
            'order_id': 9405, 'status': 'active', 'certificate': 'C',
            'ca_certificate': 'CA', 'private_key': 'K'}
        engine._deployer.deploy_multi.return_value = [
            {'site_name': 'e.com', 'status': True, 'message': '部署成功'}]
        self._rounds(engine, 1)
        assert engine._config.get_cert(9405)['metadata']['no_progress_since'] == ''


class TestBlockCallbackBound:
    """环境阻断回调的次数上限（spec §2.8）

    阻断按设计不递增 deploy_attempt_count（修好即自动恢复，不必人工解除），
    此前唯一的抑制是原因字符串相等——原因串含 PID/路径/异常文本等可变内容时
    每轮都算"变化"，整条回调路径因此没有任何上限。
    """

    def _engine(self, tmp_data_dir, api):
        return RenewEngine(ConfigManager(tmp_data_dir), MagicMock(return_value=api),
                           MagicMock(), MagicMock())

    def _cert(self, engine, order_id):
        engine._config.add_cert(order_id=order_id, cert_name='order-%d' % order_id,
                                domains=['x.com'], site_names=['x.com'], renew_mode='pull')
        engine._config.update_metadata(order_id, {'cert_expires_at': (
            datetime.now(timezone.utc) + timedelta(days=5)).strftime('%Y-%m-%dT%H:%M:%SZ')})

    def _rounds(self, engine, n):
        with patch('lib.renew.probe_panel_runtime', return_value=None):
            for _ in range(n):
                engine.check_and_renew_all(spread=False, lock_wait=0)

    def test_varying_reason_is_capped(self, tmp_data_dir):
        """阻断原因每轮不同：回调总数封顶，不再每轮一发"""
        api = MagicMock()
        api.last_renew_before_days = 0
        api.query_order.return_value = {
            'order_id': 9501, 'status': 'active', 'certificate': 'C',
            'ca_certificate': 'CA', 'private_key': 'K'}
        engine = self._engine(tmp_data_dir, api)
        self._cert(engine, 9501)
        seq = iter(range(10000))
        with patch('lib.renew.check_web_config',
                   side_effect=lambda: 'nginx: [emerg] 错误 (pid %d)' % next(seq)):
            self._rounds(engine, 60)

        assert api.callback.call_count == MAX_BLOCK_REPORT_COUNT
        assert engine._config.get_cert(9501)['metadata']['block_report_count'] \
            == MAX_BLOCK_REPORT_COUNT

    def test_stable_reason_still_reports_once(self, tmp_data_dir):
        """原因稳定时仍只发一次：上限不改变既有的边沿触发语义"""
        api = MagicMock()
        api.last_renew_before_days = 0
        api.query_order.return_value = {
            'order_id': 9502, 'status': 'active', 'certificate': 'C',
            'ca_certificate': 'CA', 'private_key': 'K'}
        engine = self._engine(tmp_data_dir, api)
        self._cert(engine, 9502)
        with patch('lib.renew.check_web_config', return_value='nginx: [emerg] 固定错误'):
            self._rounds(engine, 30)
        assert api.callback.call_count == 1

    def test_recovery_restores_the_budget(self, tmp_data_dir):
        """环境恢复过就是新一轮故障，上报额度重置"""
        api = MagicMock()
        api.last_renew_before_days = 0
        api.query_order.return_value = {
            'order_id': 9503, 'status': 'active', 'certificate': 'C',
            'ca_certificate': 'CA', 'private_key': 'K'}
        engine = self._engine(tmp_data_dir, api)
        self._cert(engine, 9503)
        with patch('lib.renew.check_web_config', return_value='坏了'):
            self._rounds(engine, 3)
        assert api.callback.call_count == 1
        with patch('lib.renew.check_web_config', return_value=None):
            self._rounds(engine, 1)
        assert engine._config.get_cert(9503)['metadata']['block_report_count'] == 0


class TestErrorCodeClassification:
    """deploy-spec §2.2：确定性失败必须停止本轮动作，不得当作网络错误逐轮重试"""

    def _engine(self, tmp_data_dir, api):
        return RenewEngine(ConfigManager(tmp_data_dir), MagicMock(return_value=api),
                           MagicMock(), MagicMock())

    def _run(self, engine):
        with patch('lib.renew.probe_panel_runtime', return_value=None):
            return engine.check_and_renew_all(spread=False, lock_wait=0)

    @staticmethod
    def _api_err(error_code, retry_after=0, msg='rejected'):
        return APIError(msg, code=0, error_code=error_code, retry_after=retry_after)

    def _add(self, engine, order_id, api_url='https://api.example.com', **kw):
        engine._config.add_cert(order_id=order_id, cert_name='order-%s' % order_id,
                                domains=['d%s.com' % order_id],
                                site_names=['d%s.com' % order_id],
                                api_url=api_url, api_token='t' * 32, **kw)
        engine._config.update_metadata(order_id, {'cert_expires_at': ''})

    def test_rate_limited_stops_the_round_for_that_token(self, tmp_data_dir):
        """限流是 token 级的：同 token 的后续证书本轮不再发起任何调用"""
        api = MagicMock()
        api.last_renew_before_days = 0
        api.query_order.side_effect = self._api_err('rate_limited', retry_after=100)
        engine = self._engine(tmp_data_dir, api)
        for oid in (9601, 9602, 9603):
            self._add(engine, oid, renew_mode='pull')

        self._run(engine)
        # 只有触发阻断的第一张真的查过；其余两张被拉黑跳过
        assert api.query_order.call_count == 1

        with open(os.path.join(tmp_data_dir, 'renew_status.json')) as f:
            status = json.load(f)
        assert status['auth_blocks'][0]['error_code'] == 'rate_limited'
        assert status['auth_blocks'][0]['retry_after'] == 100
        reasons = [s['reason'] for s in status['skipped']]
        assert reasons.count('auth_blocked:rate_limited') == 2

    def test_other_token_still_runs(self, tmp_data_dir):
        """按 (url, token) 拉黑而非全局：别的 token 必须照常跑"""
        bad, good = MagicMock(), MagicMock()
        bad.last_renew_before_days = good.last_renew_before_days = 0
        bad.query_order.side_effect = self._api_err('token_disabled')
        good.query_order.return_value = {'order_id': 9612, 'status': 'processing'}

        cfg = ConfigManager(tmp_data_dir)
        engine = RenewEngine(cfg, lambda cert: bad if cert['order_id'] == 9611 else good,
                             MagicMock(), MagicMock())
        self._add(engine, 9611, api_url='https://bad.example.com', renew_mode='pull')
        self._add(engine, 9612, api_url='https://good.example.com', renew_mode='pull')

        self._run(engine)
        assert bad.query_order.call_count == 1
        assert good.query_order.call_count == 1, '不同 token 不应被连带拉黑'

    def test_auth_block_does_not_burn_issue_count(self, tmp_data_dir):
        """限流/认证失败被中间件拒在业务层之外，那次尝试从未发生：必须回滚签发计数"""
        api = MagicMock()
        api.last_renew_before_days = 0
        api.query_order.return_value = {'order_id': 9621, 'status': 'active',
                                        'certificate': '', 'expires_at': ''}
        api.submit_csr.side_effect = self._api_err('rate_limited', retry_after=9)
        engine = self._engine(tmp_data_dir, api)
        self._add(engine, 9621, renew_mode='local', validation_method='file')

        cert = engine._config.get_cert(9621)
        before = cert['metadata'].get('issue_retry_count', 0)
        with patch('lib.cert_utils.generate_csr',
                   return_value=('---CSR---', '---KEY---', 'hash1')):
            with pytest.raises(APIError):
                engine._submit_new_csr(engine._config.get_cert(9621), api)

        meta = engine._config.get_cert(9621)['metadata']
        assert meta['issue_retry_count'] == before, '认证类失败不得消耗签发额度'
        assert meta['csr_submitted_at'] == ''
        assert meta['last_csr_hash'] == ''
        assert not os.path.isfile(engine._pending_key_path(engine._config.get_cert(9621)))

    def test_business_rejection_still_burns_issue_count(self, tmp_data_dir):
        """对照组：服务端确实处理并拒绝了这次提交，计数必须保留"""
        api = MagicMock()
        api.last_renew_before_days = 0
        api.submit_csr.side_effect = APIError('该产品不支持文件验证', code=0)
        engine = self._engine(tmp_data_dir, api)
        self._add(engine, 9622, renew_mode='local', validation_method='file')

        with patch('lib.cert_utils.generate_csr',
                   return_value=('---CSR---', '---KEY---', 'hash1')):
            with pytest.raises(APIError):
                engine._submit_new_csr(engine._config.get_cert(9622), api)

        meta = engine._config.get_cert(9622)['metadata']
        assert meta['issue_retry_count'] == 1, '明确业务拒绝仍是一次真实尝试'

    def test_order_not_found_anchors_no_progress_clock(self, tmp_data_dir):
        """订单已被删除：查询每轮抛异常，必须仍起停更计时，否则 14 天边界形同不存在"""
        api = MagicMock()
        api.last_renew_before_days = 0
        api.query_order.side_effect = self._api_err('order_not_found', msg='未找到匹配的订单')
        engine = self._engine(tmp_data_dir, api)
        self._add(engine, 9631, renew_mode='pull')

        self._run(engine)
        meta = engine._config.get_cert(9631)['metadata']
        assert meta['no_progress_since'], '订单级拒绝必须锚定停更计时'
        assert meta['last_order_status'] == 'order_not_found'

        _backdate_no_progress(engine._config, 9631, MAX_NO_PROGRESS_DAYS + 1)
        before = api.query_order.call_count
        for _ in range(5):
            self._run(engine)
        meta = engine._config.get_cert(9631)['metadata']
        assert api.query_order.call_count == before, '超时后不得再拉取'
        assert meta['last_issue_state'] == ISSUE_STATE_CAPPED
        assert meta['capped_phase'] == CAPPED_PHASE_STALLED

    def test_order_level_error_does_not_block_other_certs(self, tmp_data_dir):
        """订单级失败只影响单张证书，同 token 的其他证书照常处理"""
        api = MagicMock()
        api.last_renew_before_days = 0

        def query(order_id):
            if int(order_id) == 9641:
                raise self._api_err('order_not_found')
            return {'order_id': order_id, 'status': 'processing'}

        api.query_order.side_effect = query
        engine = self._engine(tmp_data_dir, api)
        self._add(engine, 9641, renew_mode='pull')
        self._add(engine, 9642, renew_mode='pull')

        self._run(engine)
        assert api.query_order.call_count == 2, '订单级拒绝不得拉黑整个 token'

    def test_unclassified_error_keeps_old_behavior(self, tmp_data_dir):
        """无 error_code 的错误维持原状：逐证书失败，不拉黑、不跳过"""
        api = MagicMock()
        api.last_renew_before_days = 0
        api.query_order.side_effect = APIError('boom', code=0)
        engine = self._engine(tmp_data_dir, api)
        self._add(engine, 9651, renew_mode='pull')
        self._add(engine, 9652, renew_mode='pull')

        results = self._run(engine)
        assert api.query_order.call_count == 2
        assert all(r['status'] == 'failure' for r in results)
        with open(os.path.join(tmp_data_dir, 'renew_status.json')) as f:
            status = json.load(f)
        assert status['auth_blocks'] == []


class TestCallbackBreaker:
    """非关键上报熔断（spec §11）

    单次回调最坏 = MAX_RETRIES(3) × TIMEOUT_POST(60s) + 退避 ≈ 183 秒，而 cron 批量
    上限 100 张：逐张各等一份完整超时预算时最坏耗时随证书数线性放大到数小时。
    """

    def _engine(self, tmp_data_dir, api=None):
        return RenewEngine(ConfigManager(tmp_data_dir), MagicMock(return_value=api or MagicMock()),
                           MagicMock(), MagicMock())

    def test_opens_after_threshold_and_stops_calling(self, tmp_data_dir):
        api = MagicMock()
        api.callback.side_effect = OSError('connection refused')
        engine = self._engine(tmp_data_dir, api)

        for _ in range(CALLBACK_BREAKER_THRESHOLD + 5):
            engine._send_failure_callback(api, 1, 'boom')

        assert api.callback.call_count == CALLBACK_BREAKER_THRESHOLD, \
            '熔断后不得再发起同类上报'

    def test_success_resets_the_streak(self, tmp_data_dir):
        api = MagicMock()
        engine = self._engine(tmp_data_dir, api)

        # 连续失败两次（未达阈值），随后一次成功应清零
        api.callback.side_effect = [OSError('x'), OSError('x'), None]
        for _ in range(3):
            engine._send_failure_callback(api, 1, 'boom')
        assert engine._callback_fail_streak == 0

        # 清零后重新获得完整额度
        api.callback.side_effect = OSError('x')
        for _ in range(CALLBACK_BREAKER_THRESHOLD + 3):
            engine._send_failure_callback(api, 1, 'boom')
        assert api.callback.call_count == 3 + CALLBACK_BREAKER_THRESHOLD

    def test_deploy_result_and_block_reports_share_the_counter(self, tmp_data_dir):
        """两类非关键上报共享计数：通道坏了就是坏了，不该各自再试三次"""
        api = MagicMock()
        api.callback.side_effect = OSError('x')
        engine = self._engine(tmp_data_dir, api)

        engine._send_deploy_callback(api, 1, 'failure', '2026-07-26T00:00:00Z', 'm', False)
        engine._send_deploy_callback(api, 2, 'failure', '2026-07-26T00:00:00Z', 'm', False)
        assert api.callback.call_count == 2
        # 第三次由 failure 回调触发即达阈值，之后两类都不再发
        engine._send_failure_callback(api, 3, 'boom')
        assert api.callback.call_count == CALLBACK_BREAKER_THRESHOLD
        engine._send_deploy_callback(api, 4, 'failure', '2026-07-26T00:00:00Z', 'm', False)
        engine._send_failure_callback(api, 5, 'boom')
        assert api.callback.call_count == CALLBACK_BREAKER_THRESHOLD

    def test_breaker_does_not_consume_block_report_budget(self, tmp_data_dir):
        """熔断打开时阻断上报根本没发出去，不得消耗 block_report_count 额度

        否则一次上报通道故障就能烧完 10 次额度，通道恢复时该证书已永久静默。
        """
        api = MagicMock()
        api.callback.side_effect = OSError('x')
        engine = self._engine(tmp_data_dir, api)
        engine._config.add_cert(order_id=9701, cert_name='order-9701', domains=['a.com'],
                                site_names=['a.com'], renew_mode='pull')

        # 先把熔断打开（用不消耗额度的部署结果回调）
        for _ in range(CALLBACK_BREAKER_THRESHOLD):
            engine._send_deploy_callback(api, 9701, 'failure', '2026-07-26T00:00:00Z', 'm', False)
        assert engine._callback_breaker_open()

        cert = engine._config.get_cert(9701)
        for i in range(5):
            engine._report_block(cert, api, 9701, 'reason-%d' % i)

        meta = engine._config.get_cert(9701)['metadata']
        assert meta['block_report_count'] == 0, '未发出的上报不得消耗额度'
        assert meta['last_deploy_block_reason'] == 'reason-4', '本地记录仍须落盘'

    def test_block_report_consumes_budget_when_attempted(self, tmp_data_dir):
        """对照组：通道正常时每次变化都真的发一次并消耗额度"""
        api = MagicMock()
        engine = self._engine(tmp_data_dir, api)
        engine._config.add_cert(order_id=9702, cert_name='order-9702', domains=['b.com'],
                                site_names=['b.com'], renew_mode='pull')

        cert = engine._config.get_cert(9702)
        for i in range(3):
            engine._report_block(cert, api, 9702, 'reason-%d' % i)

        meta = engine._config.get_cert(9702)['metadata']
        assert meta['block_report_count'] == 3
        assert api.callback.call_count == 3


class TestOrderStatusClassification:
    """服务端订单状态显式分类（spec §2.4）

    spec 明令禁止「其余即终态」兜底：服务端枚举含 unpaid / cancelling 这类可自愈中间态，
    误判为终态会让证书停在等人工而实际无人需处理；未知新增状态当终态更会误伤全量证书。
    """

    @pytest.mark.parametrize('status,expected', [
        ('active', 'active'),
        ('pending', 'waiting'),
        ('processing', 'waiting'),
        ('approving', 'waiting'),
        ('unpaid', 'waiting'),
        ('cancelling', 'waiting'),
        ('failed', 'terminal'),
        ('cancelled', 'terminal'),
        ('revoked', 'terminal'),
        ('expired', 'terminal'),
        ('renewed', 'chain'),
        ('reissued', 'chain'),
        ('brand_new_state', 'unknown'),
        ('', 'unknown'),
        (None, 'unknown'),
    ])
    def test_every_status_is_explicitly_classified(self, status, expected):
        assert classify_order_status(status) == expected

    @pytest.mark.parametrize('status', ['pending', 'approving', 'unpaid', 'cancelling',
                                        'brand_new_state', ''])
    def test_waiting_and_unknown_normalize_to_processing(self, status):
        """在途等待与未知新增状态一律归一 processing：只查询、不重复提交"""
        assert _normalize_issue_status(status) == 'processing'

    @pytest.mark.parametrize('status', ['cancelled', 'revoked', 'failed', 'renewed'])
    def test_terminal_and_chain_are_not_normalized(self, status):
        assert _normalize_issue_status(status) == status

    def _engine(self, tmp_data_dir, api):
        return RenewEngine(ConfigManager(tmp_data_dir), MagicMock(return_value=api),
                           MagicMock(), MagicMock())

    def test_unknown_status_keeps_polling_instead_of_halting(self, tmp_data_dir):
        """服务端新增未知状态：保守当在途等待，不得写终态、不得计入失败"""
        api = MagicMock()
        api.last_renew_before_days = 0
        api.query_order.return_value = {'order_id': 9801, 'status': 'some_new_status'}
        engine = self._engine(tmp_data_dir, api)
        engine._config.add_cert(order_id=9801, cert_name='order-9801', domains=['a.com'],
                                site_names=['a.com'], renew_mode='local')
        engine._config.update_metadata(9801, {'last_issue_state': 'processing',
                                              'cert_expires_at': ''})

        with patch('lib.renew.probe_panel_runtime', return_value=None):
            results = engine.check_and_renew_all(spread=False, lock_wait=0)

        meta = engine._config.get_cert(9801)['metadata']
        assert meta['last_issue_state'] == 'processing', '未知状态不得升级为终态'
        assert meta['last_order_status'] == 'some_new_status'
        assert all(r['status'] != 'failure' for r in results), '未知状态不该计入失败统计'

    def test_chain_status_warns_once_then_stays_quiet(self, tmp_data_dir):
        """链式状态（renewed/reissued）= 链数据异常：首次告警，之后静默等自愈"""
        api = MagicMock()
        api.last_renew_before_days = 0
        api.query_order.return_value = {'order_id': 9802, 'status': 'renewed'}
        logger = MagicMock()
        engine = RenewEngine(ConfigManager(tmp_data_dir), MagicMock(return_value=api),
                             MagicMock(), logger)
        engine._config.add_cert(order_id=9802, cert_name='order-9802', domains=['b.com'],
                                site_names=['b.com'], renew_mode='local')
        engine._config.update_metadata(9802, {'last_issue_state': 'processing',
                                              'cert_expires_at': ''})

        with patch('lib.renew.probe_panel_runtime', return_value=None):
            for _ in range(3):
                engine.check_and_renew_all(spread=False, lock_wait=0)

        chain_errors = [c for c in logger.error.call_args_list if '链数据异常' in str(c)]
        assert len(chain_errors) == 1, '仅状态首次变化时告警，之后静默'
        meta = engine._config.get_cert(9802)['metadata']
        assert meta['last_order_status'] == 'renewed'
        assert meta['last_issue_state'] == 'processing', '链式状态同样不写在途标记'

    def test_terminal_order_never_falls_through_to_post(self, tmp_data_dir):
        """终态订单连续多轮：绝不落到 _submit_new_csr（POST 会触发服务端扣费）"""
        api = MagicMock()
        api.last_renew_before_days = 0
        api.query_order.return_value = {'order_id': 9803, 'status': 'cancelled'}
        engine = self._engine(tmp_data_dir, api)
        engine._config.add_cert(order_id=9803, cert_name='order-9803', domains=['c.com'],
                                site_names=['c.com'], renew_mode='local')
        # 到期时间已知且临期：这正是旧实现会落到 POST 的条件
        soon = (datetime.now(timezone.utc) + timedelta(days=3)).strftime('%Y-%m-%dT%H:%M:%SZ')
        engine._config.update_metadata(9803, {'last_issue_state': 'processing',
                                              'cert_expires_at': soon})

        with patch('lib.renew.probe_panel_runtime', return_value=None):
            for _ in range(5):
                engine.check_and_renew_all(spread=False, lock_wait=0)

        api.submit_csr.assert_not_called()


class TestCertUnchangedIntegration:
    """证书更替检测的**集成**契约（spec §3.8）

    既有测试全是对 _track_cert_unchanged 的孤立单测、手工传入 prev_serial，从未跑过
    _deploy_and_report → deploy_multi → 检测 的真实链路，因此两个缺陷同时对测试隐形：

    1. deploy_multi 只 update_metadata 写盘、不回写内存 cert_entry，检测若从 metadata
       读"新序列号"，拿到的永远是部署前的旧值 → 与 prev 恒等 → 每张正常续签的证书在
       第二次续签时被误判 failure，而服务端失败提醒是电平驱动，健康证书永久留在失败视图；
    2. unchanged_cert_rounds 一旦进入 DEPLOY_SUCCESS_RESET_KEYS，计数每轮先归零再递增
       到 1，永远达不到阈值（spec §3.8 明文点名的陷阱）。
    """

    def _engine(self, tmp_data_dir, serials):
        """serials: 每轮 deploy_multi 落盘的 cert_serial（模拟服务端交付的证书）"""
        cfg = ConfigManager(tmp_data_dir)
        cfg.add_cert(order_id=1, cert_name='o1', domains=['a.com'], site_names=['s1'])
        cfg.update_metadata(1, {'cert_serial': serials[0]})
        api = MagicMock()
        api.last_renew_before_days = 0
        deployer = MagicMock()
        engine = RenewEngine(cfg, MagicMock(return_value=api), deployer, MagicMock())
        engine._check_deploy_environment = lambda *a, **k: None   # 跳过环境闸门

        state = {'i': 0}

        def fake_deploy_multi(**kw):
            """忠实模拟 deploy_multi：只写盘、不回写内存 cert_entry"""
            state['i'] += 1
            meta = {'site_deploy_status': {'s1': {'status': True}}}
            meta.update(DEPLOY_SUCCESS_RESET_KEYS)
            meta['cert_serial'] = serials[min(state['i'], len(serials) - 1)]
            cfg.update_metadata(1, meta)
            return [{'site_name': 's1', 'status': True, 'message': ''}]

        deployer.deploy_multi = fake_deploy_multi
        return engine, cfg, api, state

    def _round(self, engine, cfg, api, serial_of_this_round):
        """跑一轮编排层部署；serial_of_this_round 即 fullchain 解析出的序列号"""
        cert = cfg.get_cert(1)      # 每轮重新从盘加载，等同 cron 新进程
        with patch('lib.renew.cert_utils.parse_cert_info',
                   return_value={'serial': serial_of_this_round}):
            engine._deploy_and_report(cert, api, 'FULLCHAIN', 'KEY', ['s1'], ['a.com'], 1)
        return api.callback.call_args.kwargs

    def test_new_cert_each_round_never_false_positives(self, tmp_data_dir):
        """服务端每轮都给新证书：绝不能报 failure（此前从第 2 轮起必误报）"""
        serials = ['S0', 'S1', 'S2', 'S3']
        engine, cfg, api, _ = self._engine(tmp_data_dir, serials)

        for rnd in range(1, 4):
            cb = self._round(engine, cfg, api, serials[rnd])
            assert cb['status'] == 'success', \
                '第 %d 轮误报：服务端确实换了证书（%s）' % (rnd, serials[rnd])

    def test_same_cert_twice_is_detected_end_to_end(self, tmp_data_dir):
        """服务端反复返回同一张证书：第 2 轮必须改判 failure 并上报"""
        engine, cfg, api, _ = self._engine(tmp_data_dir, ['SAME'])

        cb1 = self._round(engine, cfg, api, 'SAME')
        assert cb1['status'] == 'success', '第 1 轮仅计数，不改判'
        assert cfg.get_cert(1)['metadata']['unchanged_cert_rounds'] == 1

        cb2 = self._round(engine, cfg, api, 'SAME')
        assert cb2['status'] == 'failure', '连续 2 轮同一张证书必须改判失败'
        assert '未实际更新' in cb2['message']

    def test_deploy_success_must_not_reset_the_counter(self, tmp_data_dir):
        """spec §3.8：计数所有权归检测本身，绝不能随部署成功清零

        这道断言直接钉住那张清零列表——把 unchanged_cert_rounds 加回
        DEPLOY_SUCCESS_RESET_KEYS 就会让它变红。
        """
        assert 'unchanged_cert_rounds' not in DEPLOY_SUCCESS_RESET_KEYS

        engine, cfg, api, _ = self._engine(tmp_data_dir, ['SAME'])
        self._round(engine, cfg, api, 'SAME')
        # 本轮部署是成功的（deploy_multi 写入了整张成功清零表），计数仍须留存
        assert cfg.get_cert(1)['metadata']['unchanged_cert_rounds'] == 1

    def test_serial_change_clears_accumulated_rounds(self, tmp_data_dir):
        """中途换成新证书：累计计数必须清零，不留到下次凑够阈值误报"""
        engine, cfg, api, _ = self._engine(tmp_data_dir, ['SAME'])
        self._round(engine, cfg, api, 'SAME')
        assert cfg.get_cert(1)['metadata']['unchanged_cert_rounds'] == 1

        cb = self._round(engine, cfg, api, 'BRAND-NEW')
        assert cb['status'] == 'success'
        assert cfg.get_cert(1)['metadata']['unchanged_cert_rounds'] == 0


class TestPostErrorCodePolicy:
    """§2.6 提交路径上确定性失败的处置分档（spec §2.2 单条目组）

    order_in_progress 是唯一的过渡态；其余永久码刻意不分档——「有界」由签发计数上限
    提供、「可见」由 error_code 进错误文本提供，不为它们各造终态（立即终态会杀死自动
    恢复：用户充值/开开关后还得回面板点「恢复自动续签」）。
    """

    def _engine(self, tmp_data_dir, submit_error, order_status='processing'):
        cfg = ConfigManager(tmp_data_dir)
        cfg.add_cert(order_id=1, cert_name='o1', domains=['a.com'], site_names=['s1'],
                     renew_mode='local', validation_method='file',
                     api_url='https://api.example.com', api_token='t' * 32)
        soon = (datetime.now(timezone.utc) + timedelta(days=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
        cfg.update_metadata(1, {'cert_expires_at': soon})   # 临期 + 到期时间已知 → 走提交路径
        api = MagicMock()
        api.last_renew_before_days = 0
        api.query_order.return_value = {'order_id': 1, 'status': order_status}
        api.submit_csr.side_effect = submit_error
        engine = RenewEngine(cfg, MagicMock(return_value=api), MagicMock(), MagicMock())
        return engine, cfg, api

    def _rounds(self, engine, n):
        with patch('lib.renew.probe_panel_runtime', return_value=None), \
                patch('lib.cert_utils.generate_csr', return_value=('CSR', 'KEY', 'h')):
            out = []
            for _ in range(n):
                out.append(engine.check_and_renew_all(spread=False, lock_wait=0))
            return out

    def test_order_in_progress_normalizes_and_stops_posting(self, tmp_data_dir):
        """过渡态：归一 processing，只提交一次，之后零 POST、计数不再增长"""
        err = APIError('订单处于 pending 状态 [order_in_progress]', code=0,
                       error_code='order_in_progress')
        engine, cfg, api = self._engine(tmp_data_dir, err)

        self._rounds(engine, 5)

        assert api.submit_csr.call_count == 1, '归一后不得再重复提交（会每轮烧签发额度）'
        meta = cfg.get_cert(1)['metadata']
        assert meta['last_issue_state'] == 'processing'
        assert meta['issue_retry_count'] == 1, '计数停在首次提交，绝不涨到触顶'
        assert meta['last_issue_state'] != ISSUE_STATE_CAPPED

    def test_order_in_progress_clears_this_round_pending(self, tmp_data_dir):
        """本轮 pending key/CSR 必须清掉：服务端签的是更早那个 CSR，留着必然不配对

        留着会让 deployer 的配对校验抛 DeployError，把坑从签发侧挪到部署侧
        （转而烧部署额度，10 轮后 CAPPED(deploy)）。
        """
        err = APIError('order in progress [order_in_progress]', code=0,
                       error_code='order_in_progress')
        engine, cfg, api = self._engine(tmp_data_dir, err)

        self._rounds(engine, 1)

        cert = cfg.get_cert(1)
        assert not os.path.isfile(engine._pending_key_path(cert)), 'pending 私钥必须清理'
        assert not engine._has_pending_csr(cert), '在途 CSR 标记必须清理'

    @pytest.mark.parametrize('code,msg', [
        ('insufficient_balance', '余额不足以支付本次续费'),
        ('auto_renew_disabled', '订单未开启自动续费'),
        ('validation_method_unsupported', '该产品不支持文件验证'),
    ])
    def test_permanent_codes_are_bounded_and_visible(self, tmp_data_dir, code, msg):
        """永久码：每轮一次提交、计数递增，10 轮触顶后零请求；error_code 全程在文本里"""
        err = APIError('%s [%s]' % (msg, code), code=0, error_code=code)
        engine, cfg, api = self._engine(tmp_data_dir, err)

        rounds = self._rounds(engine, 12)

        assert api.submit_csr.call_count == MAX_ISSUE_RETRY_COUNT, \
            '触顶后不得再发起提交'
        meta = cfg.get_cert(1)['metadata']
        assert meta['last_issue_state'] == ISSUE_STATE_CAPPED
        assert meta['capped_phase'] == CAPPED_PHASE_ISSUE
        # 可见性：失败文本必须带 error_code，这是运维判断「为何停止」的唯一线索
        assert code in rounds[0][0]['message']

    def test_permanent_code_recovers_when_fixed_within_bound(self, tmp_data_dir):
        """额度内修好（充值/开开关）→ 下轮自动恢复，无需人工点「恢复自动续签」

        这正是不为永久码另造立即终态的理由：立即终态会让这条自愈路径消失。
        """
        err = APIError('余额不足 [insufficient_balance]', code=0,
                       error_code='insufficient_balance')
        engine, cfg, api = self._engine(tmp_data_dir, err)

        self._rounds(engine, 3)
        assert cfg.get_cert(1)['metadata']['issue_retry_count'] == 3

        # 用户充值后服务端接受提交
        api.submit_csr.side_effect = None
        api.submit_csr.return_value = {'order_id': 1, 'status': 'processing'}
        self._rounds(engine, 1)

        meta = cfg.get_cert(1)['metadata']
        assert meta['last_issue_state'] == 'processing', '修好后应自动继续，无需人工解除'
        assert meta['last_issue_state'] != ISSUE_STATE_CAPPED


class TestAuthBlockRoundSummary:
    """Token 被拒的轮末汇总（spec §2.2：单条目降 debug，轮末统一一条）"""

    def test_summary_reports_token_and_skipped_counts(self, tmp_data_dir):
        cfg = ConfigManager(tmp_data_dir)
        api = MagicMock()
        api.last_renew_before_days = 0
        api.query_order.side_effect = APIError(
            'Invalid token [token_invalid]', code=0, error_code='token_invalid')
        logger = MagicMock()
        engine = RenewEngine(cfg, MagicMock(return_value=api), MagicMock(), logger)
        for oid in (1, 2, 3):
            cfg.add_cert(order_id=oid, cert_name='o%d' % oid, domains=['d%d.com' % oid],
                         site_names=['s%d' % oid], api_url='https://api.example.com',
                         api_token='t' * 32)
            cfg.update_metadata(oid, {'cert_expires_at': ''})

        with patch('lib.renew.probe_panel_runtime', return_value=None):
            engine.check_and_renew_all(spread=False, lock_wait=0)

        summaries = [c for c in logger.error.call_args_list
                     if '个部署 Token 被服务端拒绝' in str(c)]
        assert len(summaries) == 1, '轮末必须有且只有一条汇总'
        args = summaries[0][0]
        assert args[1] == 1, '被拒 token 数'
        assert 'token_invalid' in args[2]
        assert args[3] == 2, '被跳过的证书数（首张探明结果，其余零请求跳过）'
        # 单条目降 debug，不占 error 通道
        assert any('跳过该证书' in str(c) for c in logger.debug.call_args_list)
