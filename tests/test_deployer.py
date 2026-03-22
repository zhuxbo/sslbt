"""部署器测试"""

import pytest
from unittest.mock import MagicMock, patch

from lib.deployer import Deployer, DeployError
from lib.config import ConfigManager


class TestDeployer:
    @pytest.fixture
    def deployer(self, tmp_data_dir):
        config = ConfigManager(tmp_data_dir)
        api = MagicMock()
        api.callback.return_value = {'code': 1}
        logger = MagicMock()
        return Deployer(config, api, logger)

    def test_deploy_invalid_cert(self, deployer):
        with pytest.raises(DeployError, match='证书验证失败'):
            deployer.deploy('test-site', 'bad cert', 'bad key')

    def test_deploy_invalid_key(self, deployer):
        cert = "-----BEGIN CERTIFICATE-----\nTEST\n-----END CERTIFICATE-----"
        with pytest.raises(DeployError, match='私钥验证失败'):
            deployer.deploy('test-site', cert, 'bad key')

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_deploy_success(self, mock_set_ssl, mock_cert_utils, deployer, tmp_data_dir):
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = {
            'common_name': 'example.com',
            'serial': 'ABC123',
        }
        mock_set_ssl.return_value = {'status': True}

        config = ConfigManager(tmp_data_dir)
        config.add_cert(12345, 'test', ['example.com'], site_name='example.com')

        deployer._config = config
        result = deployer.deploy(
            site_name='example.com',
            fullchain_pem='cert-pem',
            key_pem='key-pem',
            order_id=12345,
            domains=['example.com'],
        )
        assert result['status'] is True
        mock_set_ssl.assert_called_once()

    @patch('lib.deployer.cert_utils')
    def test_deploy_key_mismatch(self, mock_cert_utils, deployer):
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = False

        with pytest.raises(DeployError, match='不匹配'):
            deployer.deploy('test-site', 'cert', 'key')

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_deploy_multi_site(self, mock_set_ssl, mock_cert_utils, deployer, tmp_data_dir):
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = {
            'common_name': 'a.com',
            'serial': 'ABC123',
        }
        mock_set_ssl.return_value = {'status': True}

        config = ConfigManager(tmp_data_dir)
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1.a.com', 's2.a.com'])

        deployer._config = config
        results = deployer.deploy_multi(
            site_names=['s1.a.com', 's2.a.com'],
            fullchain_pem='cert-pem',
            key_pem='key-pem',
            order_id=12345,
            domains=['a.com'],
        )
        assert len(results) == 2
        assert all(r['status'] is True for r in results)
        assert mock_set_ssl.call_count == 2

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_deploy_multi_partial_failure(self, mock_set_ssl, mock_cert_utils, deployer, tmp_data_dir):
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = {
            'common_name': 'a.com',
            'serial': 'ABC123',
        }
        mock_set_ssl.side_effect = [{'status': True}, Exception('部署超时')]

        config = ConfigManager(tmp_data_dir)
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1.a.com', 's2.a.com'])

        deployer._config = config
        results = deployer.deploy_multi(
            site_names=['s1.a.com', 's2.a.com'],
            fullchain_pem='cert-pem',
            key_pem='key-pem',
            order_id=12345,
            domains=['a.com'],
        )
        assert results[0]['status'] is True
        assert results[1]['status'] is False


    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_deploy_multi_all_fail_no_metadata_update(self, mock_set_ssl, mock_cert_utils, deployer, tmp_data_dir):
        """全部站点失败时不更新 metadata（保留重试状态）"""
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_set_ssl.side_effect = Exception('部署超时')

        config = ConfigManager(tmp_data_dir)
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1.a.com', 's2.a.com'])
        # 设置初始 metadata（模拟 Local 模式的重试状态）
        config.update_metadata(12345, {
            'last_issue_state': 'processing',
            'issue_retry_count': 3,
        })

        deployer._config = config
        results = deployer.deploy_multi(
            site_names=['s1.a.com', 's2.a.com'],
            fullchain_pem='cert-pem',
            key_pem='key-pem',
            order_id=12345,
            domains=['a.com'],
        )
        assert all(r['status'] is False for r in results)
        # metadata 不应被更新（重试状态保留）
        cert = config.get_cert(12345)
        assert cert['metadata']['last_issue_state'] == 'processing'
        assert cert['metadata']['issue_retry_count'] == 3

    @patch('lib.deployer.cert_utils')
    @patch('lib.deployer.Deployer._set_ssl')
    def test_deploy_multi_partial_success_updates_metadata(self, mock_set_ssl, mock_cert_utils, deployer, tmp_data_dir):
        """部分成功时正常更新 metadata"""
        mock_cert_utils.validate_cert_pem.return_value = (True, '')
        mock_cert_utils.validate_key_pem.return_value = (True, '')
        mock_cert_utils.verify_cert_key_match.return_value = True
        mock_cert_utils.parse_cert_info.return_value = {
            'common_name': 'a.com',
            'serial': 'DEF456',
        }
        mock_set_ssl.side_effect = [{'status': True}, Exception('超时')]

        config = ConfigManager(tmp_data_dir)
        config.add_cert(12345, 'test', ['a.com'], site_names=['s1.a.com', 's2.a.com'])
        config.update_metadata(12345, {
            'last_issue_state': 'processing',
            'issue_retry_count': 3,
        })

        deployer._config = config
        results = deployer.deploy_multi(
            site_names=['s1.a.com', 's2.a.com'],
            fullchain_pem='cert-pem',
            key_pem='key-pem',
            order_id=12345,
            domains=['a.com'],
        )
        assert results[0]['status'] is True
        assert results[1]['status'] is False
        # metadata 应被更新（部分成功）
        cert = config.get_cert(12345)
        assert cert['metadata']['last_issue_state'] == ''
        assert cert['metadata']['issue_retry_count'] == 0
        assert cert['metadata']['last_deploy_at'] != ''


class TestDeployError:
    def test_error_attributes(self):
        err = DeployError('test error', phase='validate', retryable=True)
        assert str(err) == 'test error'
        assert err.phase == 'validate'
        assert err.retryable is True
