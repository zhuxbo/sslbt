"""计划任务模块测试"""

import sys
import types
import pytest
from unittest.mock import MagicMock, patch

from lib.cron import CronManager, CRON_NAME, PLUGIN_DIR


class TestCronManager:
    @pytest.fixture
    def cron_mgr(self, tmp_data_dir):
        return CronManager(tmp_data_dir)

    def test_build_script(self, cron_mgr):
        """生成的脚本包含正确路径"""
        script = cron_mgr._build_script()
        assert PLUGIN_DIR in script
        assert 'sslbt_main' in script
        assert 'run_renew' in script

    def test_setup_success(self, cron_mgr):
        """创建计划任务（mock crontab 模块）"""
        mock_crontab_module = types.ModuleType('crontab')
        mock_cron_obj = MagicMock()
        mock_cron_obj.GetCrontab.return_value = {'data': []}
        mock_crontab_class = MagicMock(return_value=mock_cron_obj)
        mock_crontab_module.crontab = mock_crontab_class

        with patch.dict(sys.modules, {'crontab': mock_crontab_module}):
            result = cron_mgr.setup(interval_hours=6)
        assert result['status'] is True
        mock_cron_obj.AddCrontab.assert_called_once()

    def test_remove(self, cron_mgr):
        """删除计划任务"""
        mock_crontab_module = types.ModuleType('crontab')
        mock_cron_obj = MagicMock()
        mock_cron_obj.GetCrontab.return_value = {
            'data': [{'id': 1, 'name': CRON_NAME}]
        }
        mock_crontab_class = MagicMock(return_value=mock_cron_obj)
        mock_crontab_module.crontab = mock_crontab_class

        with patch.dict(sys.modules, {'crontab': mock_crontab_module}):
            cron_mgr.remove()
        mock_cron_obj.DelCrontab.assert_called_once()

    def test_get_status_not_found(self, cron_mgr):
        """未找到任务返回 exists=False"""
        mock_crontab_module = types.ModuleType('crontab')
        mock_cron_obj = MagicMock()
        mock_cron_obj.GetCrontab.return_value = {'data': []}
        mock_crontab_class = MagicMock(return_value=mock_cron_obj)
        mock_crontab_module.crontab = mock_crontab_class

        with patch.dict(sys.modules, {'crontab': mock_crontab_module}):
            status = cron_mgr.get_status()
        assert status['exists'] is False

    def test_get_status_found(self, cron_mgr):
        """找到任务返回 exists=True"""
        mock_crontab_module = types.ModuleType('crontab')
        mock_cron_obj = MagicMock()
        mock_cron_obj.GetCrontab.return_value = {
            'data': [{'id': 5, 'name': CRON_NAME, 'status': 1, 'cycle': '360分钟', 'addtime': '2025-01-01'}]
        }
        mock_crontab_class = MagicMock(return_value=mock_cron_obj)
        mock_crontab_module.crontab = mock_crontab_class

        with patch.dict(sys.modules, {'crontab': mock_crontab_module}):
            status = cron_mgr.get_status()
        assert status['exists'] is True
        assert status['id'] == 5

    def test_setup_import_error(self, cron_mgr):
        """crontab 模块不存在时返回失败"""
        with patch.dict(sys.modules, {'crontab': None}):
            result = cron_mgr.setup(interval_hours=6)
        assert result['status'] is False
