"""计划任务模块测试"""

import sys
import builtins
import sqlite3
import subprocess
import types
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.cron import CronManager, CRON_NAME, CRON_RUNNER, PLUGIN_DIR


class TestCronManager:
    @pytest.fixture
    def cron_mgr(self, tmp_data_dir):
        return CronManager(tmp_data_dir)

    def test_build_script(self, cron_mgr):
        """任务正文只引用仓库内的稳定入口脚本"""
        script = cron_mgr._build_script(sys.executable)
        assert CRON_RUNNER in script
        assert sys.executable in script
        assert 'sslbt_main' not in script
        assert 'run_renew_cron' not in script

    def test_build_script_uses_verified_interpreter(self, cron_mgr):
        """脚本烧入自检通过的解释器而非裸 python3（BT-06）"""
        script = cron_mgr._build_script('/www/server/panel/pyenv/bin/python3')
        assert '/www/server/panel/pyenv/bin/python3' in script
        # 不应残留裸 python3 调用
        assert 'python3 -c' not in script

    @staticmethod
    def _make_cron_db(tmp_path, with_task=False):
        """创建临时 crontab 数据库，可选预置一条本插件任务"""
        db_path = str(tmp_path / 'crontab.db')
        conn = sqlite3.connect(db_path)
        conn.execute('CREATE TABLE crontab (id INTEGER PRIMARY KEY, name TEXT, status INTEGER, sBody TEXT)')
        if with_task:
            script = 'cd %s && echo test' % PLUGIN_DIR
            conn.execute('INSERT INTO crontab (id, name, status, sBody) VALUES (7, ?, 1, ?)',
                         (CRON_NAME, script))
        conn.commit()
        conn.close()
        return db_path

    @staticmethod
    def _make_mock_crontab(add_result, db_path=None):
        """构造 crontab mock；db_path 给定时 AddCrontab 会真实插入任务行"""
        mock_crontab_module = types.ModuleType('crontab')
        mock_cron_obj = MagicMock()

        def _add(params):
            if db_path:
                conn = sqlite3.connect(db_path)
                conn.execute('INSERT INTO crontab (name, status, sBody) VALUES (?, 1, ?)',
                             (params['name'], params['sBody']))
                conn.commit()
                conn.close()
            return add_result

        mock_cron_obj.AddCrontab.side_effect = _add
        mock_crontab_module.crontab = MagicMock(return_value=mock_cron_obj)
        return mock_crontab_module, mock_cron_obj

    def test_setup_success(self, cron_mgr, tmp_path):
        """创建计划任务：宝塔返回成功形态且任务入库"""
        db_path = self._make_cron_db(tmp_path)
        module, mock_cron_obj = self._make_mock_crontab(
            {'status': True, 'msg': '添加成功'}, db_path=db_path)

        with patch('lib.cron._cron_db_path', return_value=db_path), \
             patch('lib.cron.resolve_python', return_value=sys.executable), \
             patch.dict(sys.modules, {'crontab': module}):
            result = cron_mgr.setup()
        assert result['status'] is True
        mock_cron_obj.AddCrontab.assert_called_once()

    def test_setup_status_false_returns_failure(self, cron_mgr, tmp_path):
        """宝塔显式返回 status False 时判失败并透传原因"""
        db_path = self._make_cron_db(tmp_path)
        module, _ = self._make_mock_crontab({'status': False, 'msg': '任务名称不能为空'})

        with patch('lib.cron._cron_db_path', return_value=db_path), \
             patch('lib.cron.resolve_python', return_value=sys.executable), \
             patch.dict(sys.modules, {'crontab': module}):
            result = cron_mgr.setup()
        assert result['status'] is False
        assert '任务名称不能为空' in result['message']

    def test_setup_status_false_wins_over_db(self, cron_mgr, tmp_path):
        """显式失败优先于数据库反查（面板可能知道入库之外的失败）"""
        db_path = self._make_cron_db(tmp_path, with_task=True)
        module, _ = self._make_mock_crontab({'status': False, 'msg': '写入 crontab 文件失败'})

        with patch('lib.cron._cron_db_path', return_value=db_path), \
             patch('lib.cron.resolve_python', return_value=sys.executable), \
             patch.dict(sys.modules, {'crontab': module}):
            result = cron_mgr.setup()
        assert result['status'] is False
        assert '写入 crontab 文件失败' in result['message']

    def test_setup_none_result_not_in_db_fails(self, cron_mgr, tmp_path):
        """返回 None 且任务未入库时判失败"""
        db_path = self._make_cron_db(tmp_path)
        module, _ = self._make_mock_crontab(None)

        with patch('lib.cron._cron_db_path', return_value=db_path), \
             patch('lib.cron.resolve_python', return_value=sys.executable), \
             patch.dict(sys.modules, {'crontab': module}):
            result = cron_mgr.setup()
        assert result['status'] is False

    def test_setup_true_result_but_not_in_db_fails(self, cron_mgr, tmp_path):
        """返回 status True 但任务未入库时判失败（谎报成功）"""
        db_path = self._make_cron_db(tmp_path)
        module, _ = self._make_mock_crontab({'status': True, 'msg': '添加成功'})

        with patch('lib.cron._cron_db_path', return_value=db_path), \
             patch('lib.cron.resolve_python', return_value=sys.executable), \
             patch.dict(sys.modules, {'crontab': module}):
            result = cron_mgr.setup()
        assert result['status'] is False

    def test_setup_odd_shape_but_in_db_succeeds(self, cron_mgr, tmp_path):
        """返回形态异常（如缺 status 键）但任务已入库时以入库为准判成功"""
        db_path = self._make_cron_db(tmp_path)
        module, _ = self._make_mock_crontab({'id': 9}, db_path=db_path)

        with patch('lib.cron._cron_db_path', return_value=db_path), \
             patch('lib.cron.resolve_python', return_value=sys.executable), \
             patch.dict(sys.modules, {'crontab': module}):
            result = cron_mgr.setup()
        assert result['status'] is True

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
        conn.execute('CREATE TABLE crontab (id INTEGER PRIMARY KEY, name TEXT, status INTEGER,'
                     ' type TEXT, where1 TEXT, addtime TEXT, sBody TEXT)')
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
        conn.execute('CREATE TABLE crontab (id INTEGER PRIMARY KEY, name TEXT, status INTEGER,'
                     ' type TEXT, where1 TEXT, where_hour TEXT, where_minute TEXT, addtime TEXT, sBody TEXT)')
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
            with patch('lib.cron.resolve_python', return_value=sys.executable), \
                 patch.dict(sys.modules, {'crontab': mock_crontab_module}):
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
        """日志轮转随仓库入口脚本更新，不再固化进宝塔任务正文"""
        script = cron_mgr._build_script(sys.executable)
        runner = Path(__file__).resolve().parents[1] / 'src' / 'scripts' / 'renew-cron.sh'
        content = runner.read_text(encoding='utf-8')
        assert 'tail -500' not in script
        assert 'LOG_FILE=' in content
        assert 'tail -500' in content
        assert 'cron.log' in content


class TestCronInterpreterFallback:
    """cron 脚本解释器回退：面板 Python 升级/迁移后路径失效不得静默停跑（D1）"""

    def test_script_falls_back_to_path_python3(self, tmp_data_dir):
        from lib.cron import CronManager

        script = CronManager(tmp_data_dir)._build_script(sys.executable)
        runner = Path(__file__).resolve().parents[1] / 'src' / 'scripts' / 'renew-cron.sh'
        content = runner.read_text(encoding='utf-8')
        assert sys.executable in script
        assert 'PY_BIN=' in content
        assert '[ -x "$PY_BIN" ] || PY_BIN="$(command -v python3)"' in content
        assert '"$PY_BIN" -' in content

    def test_script_burns_in_given_interpreter(self, tmp_data_dir):
        from lib.cron import CronManager

        script = CronManager(tmp_data_dir)._build_script('/www/server/panel/pyenv/bin/python')
        assert script.endswith(' /www/server/panel/pyenv/bin/python\n')

    def test_script_still_matched_by_plugin_dir_lookup(self, tmp_data_dir):
        """脚本仍需包含插件路径，否则 _find_cron_ids 的 LIKE 匹配失效"""
        from lib.cron import CronManager, PLUGIN_DIR

        assert PLUGIN_DIR in CronManager(tmp_data_dir)._build_script(sys.executable)


class TestInterpreterVerification:
    """解释器自检（F3）

    核心风险：_build_script 曾把 sys.executable 无条件烧进脚本。若在系统 python3 下
    执行注册（install.sh 此前正是这样），烧进去的路径每天都会在 import public 处失败，
    而脚本里的 `[ -x "$PY_BIN" ]` 存在性检查恰好通过、回退分支永不触发 —— 永久静默失效。
    """

    def test_rejects_interpreter_that_cannot_import_public(self, monkeypatch):
        from lib import cron as cron_mod

        monkeypatch.setattr(cron_mod, '_verify_interpreter', lambda p: False)
        assert cron_mod.resolve_python() is None

    def test_prefers_first_verified_candidate(self, monkeypatch):
        from lib import cron as cron_mod

        monkeypatch.setattr(cron_mod.sys, 'executable', '/usr/bin/python3')
        monkeypatch.setattr(
            cron_mod, '_verify_interpreter',
            lambda p: p == cron_mod.PANEL_PYTHON)
        assert cron_mod.resolve_python() == cron_mod.PANEL_PYTHON

    def test_install_refuses_when_no_usable_interpreter(self, tmp_data_dir, monkeypatch):
        """拿不到可用解释器就放弃：注册一个跑不通的任务比不注册更糟"""
        from lib import cron as cron_mod

        monkeypatch.setattr(cron_mod, 'resolve_python', lambda: None)
        module = types.ModuleType('crontab')
        module.crontab = MagicMock()
        with patch.dict(sys.modules, {'crontab': module}):
            res = CronManager(tmp_data_dir).setup()
        assert res['status'] is False
        assert 'Python 解释器' in res['message']
        module.crontab.assert_not_called()

    def test_verify_rejects_missing_binary(self):
        from lib.cron import _verify_interpreter
        assert _verify_interpreter('/nonexistent/python3') is False
        assert _verify_interpreter('') is False


class TestCronNonDestructive:
    """先建后删 + 查询失败不动现有任务（F3/F4）

    核心风险：setup() 曾无条件先 remove() 再建。AddCrontab 抛异常或返回 status False 时，
    用户就一个计划任务都不剩 —— 而这正是 install.sh 每次安装/升级都会走的路径。
    """

    @staticmethod
    def _db_with_task(tmp_path, task_id=7):
        db_path = str(tmp_path / 'crontab.db')
        conn = sqlite3.connect(db_path)
        conn.execute('CREATE TABLE crontab (id INTEGER PRIMARY KEY, name TEXT, status INTEGER,'
                     ' sBody TEXT, where_hour TEXT, where_minute TEXT, type TEXT,'
                     ' where1 TEXT, addtime TEXT)')
        conn.execute(
            'INSERT INTO crontab (id, name, status, sBody, where_hour, where_minute)'
            ' VALUES (?, ?, 1, ?, "14", "30")',
            (task_id, CRON_NAME, 'cd %s && echo old' % PLUGIN_DIR))
        conn.commit()
        conn.close()
        return db_path

    @staticmethod
    def _ids(db_path):
        conn = sqlite3.connect(db_path)
        try:
            return [r[0] for r in conn.execute('SELECT id FROM crontab').fetchall()]
        finally:
            conn.close()

    def test_add_failure_keeps_existing_task(self, tmp_data_dir, tmp_path, monkeypatch):
        """AddCrontab 失败时旧任务必须原封不动"""
        from lib import cron as cron_mod

        db_path = self._db_with_task(tmp_path)
        monkeypatch.setattr(cron_mod, 'resolve_python', lambda: sys.executable)

        module = types.ModuleType('crontab')
        obj = MagicMock()
        obj.AddCrontab.return_value = {'status': False, 'msg': '写入失败'}
        module.crontab = MagicMock(return_value=obj)

        with patch('lib.cron._cron_db_path', return_value=db_path), \
             patch.dict(sys.modules, {'crontab': module}):
            res = CronManager(tmp_data_dir).setup()

        assert res['status'] is False
        assert self._ids(db_path) == [7], '创建失败时不得删除原有任务'
        obj.DelCrontab.assert_not_called()

    def test_crontab_import_failure_keeps_existing_task(self, tmp_data_dir, tmp_path, monkeypatch):
        """crontab 模块不可用时同样不得先删后败"""
        from lib import cron as cron_mod

        db_path = self._db_with_task(tmp_path)
        monkeypatch.setattr(cron_mod, 'resolve_python', lambda: sys.executable)

        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == 'crontab':
                raise ImportError('no crontab')
            return real_import(name, *a, **kw)

        with patch('lib.cron._cron_db_path', return_value=db_path), \
             patch('builtins.__import__', side_effect=fake_import):
            res = CronManager(tmp_data_dir).setup()

        assert res['status'] is False
        assert self._ids(db_path) == [7]

    def test_refresh_preserves_schedule_time(self, tmp_data_dir, tmp_path, monkeypatch):
        """刷新保留执行时间（spec §7.4 幂等）：重新随机化会让升级频繁的用户时间乱跳"""
        from lib import cron as cron_mod

        db_path = self._db_with_task(tmp_path)
        monkeypatch.setattr(cron_mod, 'resolve_python', lambda: sys.executable)

        module = types.ModuleType('crontab')
        obj = MagicMock()

        def _add(params):
            conn = sqlite3.connect(db_path)
            conn.execute(
                'INSERT INTO crontab (name, status, sBody, where_hour, where_minute)'
                ' VALUES (?, 1, ?, ?, ?)',
                (params['name'], params['sBody'], params['hour'], params['minute']))
            conn.commit()
            conn.close()
            return {'status': True}

        def _del(params):
            conn = sqlite3.connect(db_path)
            conn.execute('DELETE FROM crontab WHERE id = ?', (params['id'],))
            conn.commit()
            conn.close()

        obj.AddCrontab.side_effect = _add
        obj.DelCrontab.side_effect = _del
        module.crontab = MagicMock(return_value=obj)

        with patch('lib.cron._cron_db_path', return_value=db_path), \
             patch.dict(sys.modules, {'crontab': module}):
            res = CronManager(tmp_data_dir).refresh()

        assert res['status'] is True
        params = obj.AddCrontab.call_args[0][0]
        assert params['hour'] == '14' and params['minute'] == '30'
        assert CRON_RUNNER in params['sBody']
        assert 'run_renew_cron' not in params['sBody']
        # 旧任务在新任务确认入库之后才删
        assert self._ids(db_path) != [7]
        obj.DelCrontab.assert_called_once_with({'id': 7})

    def test_refresh_if_legacy_migrates_old_body(self, tmp_data_dir, tmp_path, monkeypatch):
        """新版后端被旧前端调用时，也必须把旧完整正文迁移为薄入口。"""
        from lib import cron as cron_mod

        db_path = self._db_with_task(tmp_path)
        monkeypatch.setattr(cron_mod, 'resolve_python', lambda: sys.executable)
        module = types.ModuleType('crontab')
        obj = MagicMock()

        def _add(params):
            conn = sqlite3.connect(db_path)
            conn.execute(
                'INSERT INTO crontab (name, status, sBody, where_hour, where_minute)'
                ' VALUES (?, 1, ?, ?, ?)',
                (params['name'], params['sBody'], params['hour'], params['minute']))
            conn.commit()
            conn.close()
            return {'status': True}

        def _del(params):
            conn = sqlite3.connect(db_path)
            conn.execute('DELETE FROM crontab WHERE id = ?', (params['id'],))
            conn.commit()
            conn.close()

        obj.AddCrontab.side_effect = _add
        obj.DelCrontab.side_effect = _del
        module.crontab = MagicMock(return_value=obj)

        with patch('lib.cron._cron_db_path', return_value=db_path), \
             patch.dict(sys.modules, {'crontab': module}):
            result = CronManager(tmp_data_dir).refresh_if_legacy()

        assert result['status'] is True
        assert result['changed'] is True
        conn = sqlite3.connect(db_path)
        row = conn.execute('SELECT sBody, where_hour, where_minute FROM crontab').fetchone()
        conn.close()
        assert CRON_RUNNER in row[0]
        assert row[1:] == ('14', '30')

    def test_refresh_if_legacy_is_noop_for_thin_body(self, tmp_data_dir, tmp_path):
        """迁移完成后，读取配置不得每天重建计划任务。"""
        db_path = self._db_with_task(tmp_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            'UPDATE crontab SET sBody=? WHERE id=7',
            (CronManager(tmp_data_dir)._build_script(sys.executable),),
        )
        conn.commit()
        conn.close()

        with patch('lib.cron._cron_db_path', return_value=db_path):
            result = CronManager(tmp_data_dir).refresh_if_legacy()

        assert result == {'status': True, 'changed': False}

    def test_ensure_healthy_is_noop_for_exact_single_task(
            self, tmp_data_dir, tmp_path, monkeypatch):
        """健康任务只查询不重建，避免每天运行都扰动任务 ID 和执行时间。"""
        from lib import cron as cron_mod

        db_path = self._db_with_task(tmp_path)
        monkeypatch.setattr(cron_mod, 'resolve_python', lambda: sys.executable)
        conn = sqlite3.connect(db_path)
        conn.execute(
            'UPDATE crontab SET type="day", sBody=? WHERE id=7',
            (CronManager(tmp_data_dir)._build_script(sys.executable),),
        )
        conn.commit()
        conn.close()

        module = types.ModuleType('crontab')
        module.crontab = MagicMock()
        with patch('lib.cron._cron_db_path', return_value=db_path), \
             patch.dict(sys.modules, {'crontab': module}):
            result = CronManager(tmp_data_dir).ensure_healthy()

        assert result == {'status': True, 'changed': False, 'message': '计划任务正常'}
        module.crontab.assert_not_called()

    def test_ensure_healthy_repairs_wrong_body_and_preserves_paused_schedule(
            self, tmp_data_dir, tmp_path, monkeypatch):
        """修正文、去重时保留最新任务的时间和暂停态，且仍遵守先建后删。"""
        from lib import cron as cron_mod

        db_path = self._db_with_task(tmp_path)
        conn = sqlite3.connect(db_path)
        conn.execute('UPDATE crontab SET status=0, type="day" WHERE id=7')
        conn.execute(
            'INSERT INTO crontab '
            '(id, name, status, sBody, where_hour, where_minute, type) '
            'VALUES (8, ?, 0, ?, "16", "12", "day")',
            (CRON_NAME, 'echo damaged'),
        )
        conn.commit()
        conn.close()
        monkeypatch.setattr(cron_mod, 'resolve_python', lambda: sys.executable)

        module = types.ModuleType('crontab')
        obj = MagicMock()

        def _add(params):
            conn = sqlite3.connect(db_path)
            conn.execute(
                'INSERT INTO crontab '
                '(name, status, sBody, where_hour, where_minute, type) '
                'VALUES (?, 1, ?, ?, ?, ?)',
                (params['name'], params['sBody'], params['hour'], params['minute'], params['type']),
            )
            conn.commit()
            conn.close()
            return {'status': True}

        def _toggle(params):
            conn = sqlite3.connect(db_path)
            conn.execute('UPDATE crontab SET status=0 WHERE id=?', (params['id'],))
            conn.commit()
            conn.close()
            return {'status': True}

        def _delete(params):
            conn = sqlite3.connect(db_path)
            conn.execute('DELETE FROM crontab WHERE id=?', (params['id'],))
            conn.commit()
            conn.close()

        obj.AddCrontab.side_effect = _add
        obj.set_cron_status.side_effect = _toggle
        obj.DelCrontab.side_effect = _delete
        module.crontab = MagicMock(return_value=obj)

        with patch('lib.cron._cron_db_path', return_value=db_path), \
             patch.dict(sys.modules, {'crontab': module}):
            result = CronManager(tmp_data_dir).ensure_healthy()

        assert result['status'] is True
        assert result['changed'] is True
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            'SELECT status, sBody, where_hour, where_minute, type FROM crontab'
        ).fetchall()
        conn.close()
        assert rows == [(0, CronManager(tmp_data_dir)._build_script(sys.executable),
                         '16', '12', 'day')]
        obj.set_cron_status.assert_called_once()

    def test_ensure_healthy_does_not_replace_when_interpreter_is_unavailable(
            self, tmp_data_dir, tmp_path, monkeypatch):
        """无法确认有效解释器时宁可保留旧任务，不得破坏性“修正”。"""
        from lib import cron as cron_mod

        db_path = self._db_with_task(tmp_path)
        monkeypatch.setattr(cron_mod, 'resolve_python', lambda: None)
        module = types.ModuleType('crontab')
        module.crontab = MagicMock()

        with patch('lib.cron._cron_db_path', return_value=db_path), \
             patch.dict(sys.modules, {'crontab': module}):
            result = CronManager(tmp_data_dir).ensure_healthy()

        assert result['status'] is False
        assert self._ids(db_path) == [7]
        module.crontab.assert_not_called()

    def test_ensure_healthy_keeps_paused_old_task_when_pause_confirmation_fails(
            self, tmp_data_dir, tmp_path, monkeypatch):
        """新任务暂停失败时不能删除用户原本暂停的任务。"""
        from lib import cron as cron_mod

        db_path = self._db_with_task(tmp_path)
        conn = sqlite3.connect(db_path)
        conn.execute('UPDATE crontab SET status=0, type="day" WHERE id=7')
        conn.commit()
        conn.close()
        monkeypatch.setattr(cron_mod, 'resolve_python', lambda: sys.executable)

        module = types.ModuleType('crontab')
        obj = MagicMock()

        def _add(params):
            conn = sqlite3.connect(db_path)
            conn.execute(
                'INSERT INTO crontab '
                '(name, status, sBody, where_hour, where_minute, type) '
                'VALUES (?, 1, ?, ?, ?, "day")',
                (params['name'], params['sBody'], params['hour'], params['minute']),
            )
            conn.commit()
            conn.close()
            return {'status': True}

        def _delete_new(params):
            conn = sqlite3.connect(db_path)
            conn.execute('DELETE FROM crontab WHERE id=? AND id<>7', (params['id'],))
            conn.commit()
            conn.close()

        obj.AddCrontab.side_effect = _add
        obj.set_cron_status.return_value = {'status': False, 'msg': '暂停失败'}
        obj.DelCrontab.side_effect = _delete_new
        module.crontab = MagicMock(return_value=obj)

        with patch('lib.cron._cron_db_path', return_value=db_path), \
             patch.dict(sys.modules, {'crontab': module}):
            result = CronManager(tmp_data_dir).ensure_healthy()

        assert result['status'] is False
        conn = sqlite3.connect(db_path)
        rows = conn.execute('SELECT id, status FROM crontab').fetchall()
        conn.close()
        assert rows == [(7, 0)]

    def test_refresh_creates_when_absent(self, tmp_data_dir, tmp_path, monkeypatch):
        from lib import cron as cron_mod

        db_path = str(tmp_path / 'crontab.db')
        conn = sqlite3.connect(db_path)
        conn.execute('CREATE TABLE crontab (id INTEGER PRIMARY KEY, name TEXT, status INTEGER,'
                     ' sBody TEXT, where_hour TEXT, where_minute TEXT, type TEXT,'
                     ' where1 TEXT, addtime TEXT)')
        conn.commit()
        conn.close()
        monkeypatch.setattr(cron_mod, 'resolve_python', lambda: sys.executable)

        module = types.ModuleType('crontab')
        obj = MagicMock()

        def _add(params):
            c = sqlite3.connect(db_path)
            c.execute('INSERT INTO crontab (name, status, sBody, where_hour, where_minute)'
                      ' VALUES (?, 1, ?, ?, ?)',
                      (params['name'], params['sBody'], params['hour'], params['minute']))
            c.commit()
            c.close()
            return {'status': True}

        obj.AddCrontab.side_effect = _add
        module.crontab = MagicMock(return_value=obj)

        with patch('lib.cron._cron_db_path', return_value=db_path), \
             patch.dict(sys.modules, {'crontab': module}):
            res = CronManager(tmp_data_dir).refresh()
        assert res['status'] is True

    def test_refresh_skips_on_query_error(self, tmp_data_dir, monkeypatch):
        """查询失败时不动现有任务：无法确认存在与否，重建可能删掉正常的"""
        mgr = CronManager(tmp_data_dir)
        monkeypatch.setattr(mgr, 'get_status', lambda: {'exists': False, 'error': 'db locked'})
        module = types.ModuleType('crontab')
        module.crontab = MagicMock()
        with patch.dict(sys.modules, {'crontab': module}):
            res = mgr.refresh()
        assert res['status'] is False
        module.crontab.assert_not_called()


class TestCronStatusTriState:
    """get_status 三态（F4）：吞掉异常一律返回 exists=False，会让 add_cert 的
    「不存在就建」在一次瞬时 DB 锁定时重建用户正常的任务"""

    def test_query_error_reports_error_not_absent(self, tmp_data_dir, tmp_path):
        db_path = str(tmp_path / 'crontab.db')
        with open(db_path, 'w') as f:
            f.write('not a sqlite db')

        with patch('lib.cron._cron_db_path', return_value=db_path):
            st = CronManager(tmp_data_dir).get_status()
        assert st['exists'] is False
        assert st.get('error'), '查询失败必须与「确认不存在」区分'

    def test_absent_has_no_error(self, tmp_data_dir, tmp_path):
        db_path = str(tmp_path / 'crontab.db')
        conn = sqlite3.connect(db_path)
        conn.execute('CREATE TABLE crontab (id INTEGER PRIMARY KEY, name TEXT, status INTEGER,'
                     ' sBody TEXT, where_hour TEXT, where_minute TEXT, type TEXT,'
                     ' where1 TEXT, addtime TEXT)')
        conn.commit()
        conn.close()

        with patch('lib.cron._cron_db_path', return_value=db_path):
            st = CronManager(tmp_data_dir).get_status()
        assert st['exists'] is False
        assert not st.get('error')

    def test_paused_task_is_reported(self, tmp_data_dir, tmp_path):
        """暂停必须能被面板看到：此前只渲染 cycle，暂停与运行中长得一模一样"""
        db_path = str(tmp_path / 'crontab.db')
        conn = sqlite3.connect(db_path)
        conn.execute('CREATE TABLE crontab (id INTEGER PRIMARY KEY, name TEXT, status INTEGER,'
                     ' sBody TEXT, where_hour TEXT, where_minute TEXT, type TEXT,'
                     ' where1 TEXT, addtime TEXT)')
        conn.execute(
            'INSERT INTO crontab (id, name, status, sBody, where_hour, where_minute)'
            ' VALUES (1, ?, 0, ?, "9", "5")', (CRON_NAME, 'cd %s' % PLUGIN_DIR))
        conn.commit()
        conn.close()

        with patch('lib.cron._cron_db_path', return_value=db_path):
            st = CronManager(tmp_data_dir).get_status()
        assert st['exists'] is True
        assert st['paused'] is True
        assert st['status'] == '已暂停'
        assert st['hour'] == 9 and st['minute'] == 5


class TestCronScriptReportsResult:
    """cron 入口必须打印结果（F5）：run_renew_cron 吞掉所有异常并返回 _err，
    返回值被丢弃时 cron.log 恒为空，宝塔计划任务日志也永远显示成功"""

    def test_script_prints_result_message(self, tmp_data_dir):
        runner = Path(__file__).resolve().parents[1] / 'src' / 'scripts' / 'renew-cron.sh'
        content = runner.read_text(encoding='utf-8')
        assert 'print(' in content
        assert "get('msg'" in content

    def test_runner_executes_with_registered_interpreter(self, tmp_path):
        """入口优先使用任务注册时传入的解释器，并把运行结果写入 cron.log。"""
        plugin_dir = tmp_path / 'plugin'
        runner_dir = plugin_dir / 'scripts'
        data_dir = plugin_dir / 'data' / 'logs'
        runner_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        runner = Path(__file__).resolve().parents[1] / 'src' / 'scripts' / 'renew-cron.sh'
        target = runner_dir / 'renew-cron.sh'
        target.write_text(runner.read_text(encoding='utf-8'), encoding='utf-8')
        main = plugin_dir / 'sslbt_main.py'
        main.write_text(
            'class sslbt_main:\n'
            '    def run_renew_cron(self, args):\n'
            '        return {"msg": "入口执行成功"}\n',
            encoding='utf-8',
        )

        result = subprocess.run(
            ['bash', str(target), sys.executable],
            check=False, capture_output=True, text=True,
        )

        assert result.returncode == 0
        assert '入口执行成功' in (data_dir / 'cron.log').read_text(encoding='utf-8')
