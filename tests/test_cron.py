"""计划任务模块测试"""

import os
import sys
import sqlite3
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
        assert 'run_renew_cron' in script

    def test_setup_success(self, cron_mgr):
        """创建计划任务（mock crontab 模块）"""
        mock_crontab_module = types.ModuleType('crontab')
        mock_cron_obj = MagicMock()
        mock_crontab_class = MagicMock(return_value=mock_cron_obj)
        mock_crontab_module.crontab = mock_crontab_class

        with patch.dict(sys.modules, {'crontab': mock_crontab_module}):
            result = cron_mgr.setup()
        assert result['status'] is True
        mock_cron_obj.AddCrontab.assert_called_once()

    def test_remove(self, cron_mgr, tmp_path):
        """删除计划任务（通过临时数据库）"""
        # 创建临时 crontab 数据库
        db_path = str(tmp_path / 'crontab.db')
        script = 'cd %s && echo test' % PLUGIN_DIR
        conn = sqlite3.connect(db_path)
        conn.execute('CREATE TABLE crontab (id INTEGER PRIMARY KEY, name TEXT, status INTEGER, sBody TEXT)')
        conn.execute('INSERT INTO crontab (id, name, status, sBody) VALUES (1, ?, 1, ?)', (CRON_NAME, script))
        conn.execute('INSERT INTO crontab (id, name, status, sBody) VALUES (2, ?, 1, ?)', (CRON_NAME, script))
        conn.commit()
        conn.close()

        mock_crontab_module = types.ModuleType('crontab')
        mock_cron_obj = MagicMock()
        mock_crontab_class = MagicMock(return_value=mock_cron_obj)
        mock_crontab_module.crontab = mock_crontab_class

        with patch('lib.cron._cron_db_path', return_value=db_path), \
             patch.dict(sys.modules, {'crontab': mock_crontab_module}):
            cron_mgr.remove()
        assert mock_cron_obj.DelCrontab.call_count == 2

    def test_get_status_not_found(self, cron_mgr, tmp_path):
        """未找到任务返回 exists=False"""
        db_path = str(tmp_path / 'crontab.db')
        conn = sqlite3.connect(db_path)
        conn.execute('CREATE TABLE crontab (id INTEGER PRIMARY KEY, name TEXT, status INTEGER, type TEXT, where1 TEXT, addtime TEXT, sBody TEXT)')
        conn.commit()
        conn.close()

        with patch('lib.cron._cron_db_path', return_value=db_path):
            status = cron_mgr.get_status()
        assert status['exists'] is False

    def test_get_status_found(self, cron_mgr, tmp_path):
        """找到任务返回 exists=True"""
        db_path = str(tmp_path / 'crontab.db')
        script = 'cd %s && echo test' % PLUGIN_DIR
        conn = sqlite3.connect(db_path)
        conn.execute('CREATE TABLE crontab (id INTEGER PRIMARY KEY, name TEXT, status INTEGER, type TEXT, where1 TEXT, where_hour TEXT, where_minute TEXT, addtime TEXT, sBody TEXT)')
        conn.execute("INSERT INTO crontab VALUES (5, ?, 1, 'day', '', '3', '15', '2025-01-01', ?)", (CRON_NAME, script))
        conn.commit()
        conn.close()

        with patch('lib.cron._cron_db_path', return_value=db_path):
            status = cron_mgr.get_status()
        assert status['exists'] is True
        assert status['id'] == 5

    def test_setup_hour_range(self, cron_mgr):
        """随机小时在 9-23 范围内"""
        mock_crontab_module = types.ModuleType('crontab')
        mock_cron_obj = MagicMock()
        mock_crontab_class = MagicMock(return_value=mock_cron_obj)
        mock_crontab_module.crontab = mock_crontab_class

        hours = set()
        for _ in range(200):
            with patch.dict(sys.modules, {'crontab': mock_crontab_module}):
                cron_mgr.setup()
            call_args = mock_cron_obj.AddCrontab.call_args[0][0]
            hours.add(int(call_args['hour']))
        assert all(9 <= h <= 23 for h in hours)
        assert min(hours) == 9

    def test_dedup_removes_extras(self, cron_mgr, tmp_path):
        """创建后如果存在重复任务，只保留最新一条"""
        db_path = str(tmp_path / 'crontab.db')
        script = 'cd %s && echo test' % PLUGIN_DIR
        conn = sqlite3.connect(db_path)
        conn.execute('CREATE TABLE crontab (id INTEGER PRIMARY KEY, name TEXT, status INTEGER, sBody TEXT)')
        conn.execute('INSERT INTO crontab VALUES (1, ?, 1, ?)', (CRON_NAME, script))
        conn.execute('INSERT INTO crontab VALUES (5, ?, 1, ?)', (CRON_NAME, script))
        conn.execute('INSERT INTO crontab VALUES (3, ?, 1, ?)', (CRON_NAME, script))
        conn.commit()
        conn.close()

        mock_crontab_module = types.ModuleType('crontab')
        mock_cron_obj = MagicMock()
        mock_crontab_class = MagicMock(return_value=mock_cron_obj)
        mock_crontab_module.crontab = mock_crontab_class

        with patch('lib.cron._cron_db_path', return_value=db_path), \
             patch.dict(sys.modules, {'crontab': mock_crontab_module}):
            cron_mgr._dedup(mock_cron_obj)

        # 应删除 id=1 和 id=3，保留 id=5（最大）
        deleted_ids = [call[0][0]['id'] for call in mock_cron_obj.DelCrontab.call_args_list]
        assert sorted(deleted_ids) == [1, 3]
        assert mock_cron_obj.DelCrontab.call_count == 2

    def test_setup_import_error(self, cron_mgr):
        """crontab 模块不存在时返回失败"""
        with patch.dict(sys.modules, {'crontab': None}):
            result = cron_mgr.setup()
        assert result['status'] is False

    def test_build_script_has_log_rotation(self, cron_mgr):
        """脚本包含 cron.log 轮转逻辑"""
        script = cron_mgr._build_script()
        assert 'LOG_FILE=' in script
        assert 'tail -500' in script
        assert 'cron.log' in script
