"""宝塔计划任务集成模块"""

import os
import sys
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
    """通过脚本内容匹配插件路径，找到所有本插件的计划任务 ID"""
    db_path = _cron_db_path()
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT id FROM crontab WHERE sBody LIKE ?",
                ('%' + PLUGIN_DIR + '%',),
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
        run_hour = random.randint(9, 23)
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

            result = cron_obj.AddCrontab(params)
            self._dedup(cron_obj)

            # 结果判定：显式 status False 直接判失败（面板可能知道入库之外的失败，
            # 如 crontab 文件同步）；其余形态以任务是否入库为准，防止失败被误报成功
            if isinstance(result, dict) and result.get('status') is False:
                msg = str(result.get('msg') or '') or repr(result)
                if self._logger:
                    self._logger.error("创建计划任务失败: %s", msg)
                return {'status': False, 'message': '创建失败: %s' % msg}

            if not _find_cron_ids():
                if self._logger:
                    self._logger.error("创建计划任务失败: AddCrontab 返回 %r 且任务未入库", result)
                return {'status': False,
                        'message': '创建失败: AddCrontab 返回 %r 且任务未入库' % (result,)}

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
                    " FROM crontab WHERE sBody LIKE ? LIMIT 1",
                    ('%' + PLUGIN_DIR + '%',),
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

    def _dedup(self, cron_obj):
        """保留最新一条同名任务，删除多余的"""
        ids = _find_cron_ids()
        if len(ids) <= 1:
            return
        keep_id = max(ids)
        for cron_id in ids:
            if cron_id != keep_id:
                try:
                    cron_obj.DelCrontab(_BtParams(id=cron_id))
                    if self._logger:
                        self._logger.info("清理重复任务: id=%s", cron_id)
                except Exception:
                    pass

    def _build_script(self):
        """构建续签检查脚本

        优先使用注册时进程的解释器（面板 Python，sys.executable）而非裸 python3，
        避免面板 pyenv 与系统 python3 环境不一致导致续签整体不可运行。
        脚本内做存在性检查：面板 Python 升级/迁移后该绝对路径会失效，届时回退
        PATH 中的 python3，避免计划任务静默不再运行（脚本只在 setup 时重建）。
        """
        log_file = '%s/logs/cron.log' % self._data_dir
        python_bin = sys.executable or 'python3'
        return '''#!/bin/bash
# cron.log 轮转：超过 1000 行保留最后 500 行
LOG_FILE="%s"
if [ -f "$LOG_FILE" ] && [ "$(wc -l < "$LOG_FILE")" -gt 1000 ]; then
    tail -500 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi
cd "%s"
# 注册时的面板解释器；路径失效（面板 Python 升级/迁移）时回退 PATH 中的 python3
PY_BIN="%s"
[ -x "$PY_BIN" ] || PY_BIN="$(command -v python3)"
"$PY_BIN" -c "
import sys
sys.path.insert(0, '/www/server/panel/class/')
sys.path.insert(0, '%s')
from sslbt_main import sslbt_main
plugin = sslbt_main()
plugin.run_renew_cron(None)
" >> "$LOG_FILE" 2>&1
''' % (log_file, PLUGIN_DIR, python_bin, PLUGIN_DIR)
