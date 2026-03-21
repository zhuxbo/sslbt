"""宝塔计划任务集成模块"""

import os
import random
import sqlite3

CRON_NAME = 'SSL 证书自动续签'
PLUGIN_DIR = '/www/server/panel/plugin/sslbt'

# 宝塔计划任务数据库路径
_CRON_DB_NEW = '/www/server/panel/data/db/crontab.db'
_CRON_DB_OLD = '/www/server/panel/data/default.db'


class _BtParams(dict):
    """兼容宝塔 API 的参数对象"""
    def __init__(self, **kw):
        super().__init__(**kw)

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

    def __setattr__(self, key, value):
        self[key] = value


def _cron_db_path():
    if os.path.exists(_CRON_DB_NEW):
        return _CRON_DB_NEW
    return _CRON_DB_OLD


def _find_cron_ids():
    """直接查数据库找到所有同名计划任务 ID"""
    db_path = _cron_db_path()
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                'SELECT id FROM crontab WHERE name = ?', (CRON_NAME,)
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


class CronManager:
    """通过宝塔 crontab 模块管理计划任务"""

    def __init__(self, data_dir, logger=None):
        self._data_dir = data_dir
        self._logger = logger

    def setup(self):
        """创建计划任务，每天随机时间执行一次"""
        self.remove()

        script = self._build_script()
        run_hour = random.randint(0, 23)
        run_minute = random.randint(0, 59)

        try:
            import crontab
            cron_obj = crontab.crontab()

            params = _BtParams(
                name=CRON_NAME,
                type='day',
                where1='',
                hour=str(run_hour),
                minute=str(run_minute),
                week='',
                sType='toShell',
                sBody=script,
                sName='',
                backupTo='',
                save='',
                urladdress='',
            )

            cron_obj.AddCrontab(params)
            if self._logger:
                self._logger.info("计划任务创建成功: 每天 %d:%02d", run_hour, run_minute)
            return {'status': True, 'message': '计划任务已创建'}
        except Exception as e:
            if self._logger:
                self._logger.error("创建计划任务失败: %s", str(e))
            return {'status': False, 'message': '创建失败: %s' % str(e)}

    def remove(self):
        """移除所有同名计划任务"""
        ids = _find_cron_ids()
        if not ids:
            return
        try:
            import crontab
            cron_obj = crontab.crontab()
            for cron_id in ids:
                cron_obj.DelCrontab(_BtParams(id=cron_id))
                if self._logger:
                    self._logger.info("计划任务已删除: id=%s", cron_id)
        except Exception as e:
            if self._logger:
                self._logger.error("删除计划任务失败: %s", str(e))

    def get_status(self):
        """查询计划任务状态"""
        db_path = _cron_db_path()
        if not os.path.exists(db_path):
            return {'exists': False}
        try:
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    'SELECT id, status, type, where1, where_hour, where_minute, addtime'
                    ' FROM crontab WHERE name = ? LIMIT 1',
                    (CRON_NAME,),
                ).fetchone()
            finally:
                conn.close()
            if not row:
                return {'exists': False}
            cycle = '每天 %s:%02d' % (row['where_hour'] or '0', int(row['where_minute'] or 0))
            return {
                'exists': True,
                'id': row['id'],
                'status': '运行中' if row['status'] == 1 else '已暂停',
                'cycle': cycle,
                'last_run': row['addtime'] or '',
            }
        except Exception:
            return {'exists': False}

    def _build_script(self):
        """构建续签检查脚本"""
        return '''#!/bin/bash
cd %s
python3 -c "
import sys
sys.path.insert(0, '/www/server/panel/class/')
sys.path.insert(0, '%s')
from sslbt_main import sslbt_main
plugin = sslbt_main()
plugin.run_renew(None)
" >> %s/logs/cron.log 2>&1
''' % (PLUGIN_DIR, PLUGIN_DIR, self._data_dir)
