"""宝塔计划任务集成模块"""

import os
import sys
import random
import shlex
import sqlite3
import subprocess

CRON_NAME = 'SSL 证书自动续签'
PLUGIN_DIR = '/www/server/panel/plugin/sslbt'
PANEL_CLASS_DIR = '/www/server/panel/class'
PANEL_PYTHON = '/www/server/panel/pyenv/bin/python3'
CRON_RUNNER = PLUGIN_DIR + '/scripts/renew-cron.sh'

# 宝塔计划任务数据库路径
_CRON_DB_NEW = '/www/server/panel/data/db/crontab.db'
_CRON_DB_OLD = '/www/server/panel/data/default.db'

# 解释器自检超时（秒）：面板 Python 冷启动有开销，但不该无限等
_VERIFY_TIMEOUT = 20


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
    """通过任务名或脚本路径找到所有本插件的计划任务 ID。"""
    db_path = _cron_db_path()
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT id FROM crontab WHERE name = ? OR sBody LIKE ?",
                (CRON_NAME, '%' + PLUGIN_DIR + '%'),
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


def _verify_interpreter(py_bin):
    """验证解释器能加载宝塔运行时（public 模块）

    必须用 subprocess 验「另一个」解释器——面板进程内 import 成功只能证明自己可用，
    而烧进 cron 脚本的可能是完全不同的一个。系统 python3 缺 psutil 等面板依赖，
    烧进去后每天的续签都会在 import public 处失败，且 `[ -x ]` 存在性检查恰好通过、
    回退分支永不触发，是永久性的静默失效。
    """
    if not py_bin or not os.path.isfile(py_bin):
        return False
    code = 'import sys; sys.path.insert(0, %r); import public' % PANEL_CLASS_DIR
    try:
        proc = subprocess.Popen(
            [py_bin, '-c', code],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return False
    try:
        proc.communicate(timeout=_VERIFY_TIMEOUT)
    except Exception:
        proc.kill()
        proc.communicate()
        return False
    return proc.returncode == 0


def resolve_python():
    """选出能加载宝塔运行时的解释器；找不到返回 None（调用方必须放弃 cron 操作）

    候选顺序：当前解释器（面板进程通常就是面板 pyenv）→ 面板 pyenv 绝对路径。
    绝不回落系统 python3：宁可不注册，也不注册一个每天必然失败的任务。
    """
    seen = []
    for cand in (sys.executable, PANEL_PYTHON):
        if not cand or cand in seen:
            continue
        seen.append(cand)
        if _verify_interpreter(cand):
            return cand
    return None


class CronManager:
    """通过宝塔 crontab 模块管理计划任务"""

    def __init__(self, data_dir, logger=None):
        self._data_dir = data_dir
        self._logger = logger

    def setup(self):
        """创建计划任务，每天随机时间执行一次（首次安装/用户手动重建）"""
        return self._install(random.randint(9, 23), random.randint(0, 59))

    def refresh(self):
        """刷新脚本正文，保留现有执行时间（deploy-spec §7.4 幂等性）

        安装与升级走这个入口：remove + 新随机时间不叫"更新"——dev 通道频繁升级的用户
        执行时间每次都变，新时间早于当前时刻则当天不再执行，晚于则当天跑两次。
        任务不存在时退化为 setup()。
        """
        cur = self.get_status()
        if cur.get('error'):
            # 查询失败时不动现有任务：无法确认存在与否，重建可能删掉正常的任务
            if self._logger:
                self._logger.warning("计划任务状态查询失败，跳过刷新: %s", cur['error'])
            return {'status': False, 'message': '状态查询失败: %s' % cur['error']}
        if not cur.get('exists'):
            return self.setup()
        return self._install(
            cur.get('hour'),
            cur.get('minute'),
            paused=bool(cur.get('paused')),
        )

    def ensure_healthy(self):
        """检查并修正计划任务；健康时只读，发现偏差时才原子替换。

        校验任务唯一性、名称、每天周期、执行时间、暂停状态和完整脚本正文。修复时
        以 ID 最大的任务为时间与暂停态基准，先创建并确认新任务（包括暂停态），再
        删除全部旧任务。任何查询、解释器验证或暂停确认失败都保持旧任务不动。
        """
        db_path = _cron_db_path()
        if not os.path.exists(db_path):
            result = dict(self.setup())
            result['changed'] = bool(result.get('status'))
            return result

        try:
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    'SELECT id, name, status, type, where1, where_hour, where_minute, sBody'
                    ' FROM crontab WHERE name = ? OR sBody LIKE ? ORDER BY id DESC',
                    (CRON_NAME, '%' + PLUGIN_DIR + '%'),
                ).fetchall()
            finally:
                conn.close()
        except Exception as e:
            return {'status': False, 'changed': False,
                    'message': '状态查询失败: %s' % str(e)}

        if not rows:
            result = dict(self.setup())
            result['changed'] = bool(result.get('status'))
            return result

        # 当前进程已经成功加载插件和宝塔运行时，健康路径可直接用其解释器做精确
        # 比对，避免每次打开设置页/每天 cron 都额外拉起验证子进程。只有发现偏差
        # 需要重建时，才调用 resolve_python() 做完整的独立进程自检。
        current_python = (
            sys.executable
            if sys.executable and os.path.isfile(sys.executable)
            else None
        )
        expected = self._build_script(current_python) if current_python else ''
        reference = rows[0]
        try:
            hour = int(reference['where_hour'])
            minute = int(reference['where_minute'])
        except (TypeError, ValueError):
            hour, minute = None, None
        valid_time = (
            hour is not None and minute is not None
            and 0 <= hour <= 23 and 0 <= minute <= 59
        )
        healthy = (
            len(rows) == 1
            and reference['name'] == CRON_NAME
            and reference['status'] in (0, 1)
            and reference['type'] == 'day'
            and (reference['where1'] or '') == ''
            and valid_time
            and (reference['sBody'] or '') == expected
        )
        if healthy:
            return {'status': True, 'changed': False, 'message': '计划任务正常'}

        py_bin = resolve_python()
        if not py_bin:
            return {
                'status': False,
                'changed': False,
                'message': '未找到可加载宝塔运行时的 Python 解释器',
            }
        # 完整自检可能选出与当前解释器不同、但同样有效的面板解释器。重建统一使用
        # 自检结果，不复用上方只服务于健康快路径的 expected。
        result = dict(self._install(
            hour if valid_time else None,
            minute if valid_time else None,
            paused=reference['status'] == 0,
            python_bin=py_bin,
        ))
        result['changed'] = bool(result.get('status'))
        return result

    def refresh_if_legacy(self):
        """仅当现有任务仍是旧完整正文时迁移为薄入口。

        首次升级到薄入口版本时，执行升级的父进程和浏览器都可能仍运行旧代码。
        旧前端刷新后至少会调用新版 get_config，因此由该入口做一次形态检测；已经
        迁移的任务直接返回，避免每次读取配置都重建计划任务。
        """
        db_path = _cron_db_path()
        if not os.path.exists(db_path):
            return {'status': True, 'changed': False}
        try:
            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    'SELECT sBody FROM crontab WHERE sBody LIKE ?',
                    ('%' + PLUGIN_DIR + '%',),
                ).fetchall()
            finally:
                conn.close()
        except Exception as e:
            return {'status': False, 'changed': False, 'message': str(e)}

        if not rows or all(CRON_RUNNER in (row[0] or '') for row in rows):
            return {'status': True, 'changed': False}
        return self.ensure_healthy()

    def _install(self, run_hour, run_minute, paused=False, python_bin=None):
        """注册计划任务：先建后删，绝不留下"旧的删了新的没建成"的空窗

        run_hour/run_minute 为 None 时回落随机值（历史条目缺字段的兜底）。
        """
        py_bin = python_bin or resolve_python()
        if not py_bin:
            msg = ('未找到可加载宝塔运行时的 Python 解释器（已试 %s、%s）；'
                   '注册一个跑不通的任务比不注册更糟，已放弃'
                   % (sys.executable or '未知', PANEL_PYTHON))
            if self._logger:
                self._logger.error("创建计划任务失败: %s", msg)
            return {'status': False, 'message': msg}

        # crontab 模块可用性探测放在任何删除动作之前：先删后失败会让用户一个任务都不剩
        try:
            import crontab
        except Exception as e:
            msg = '宝塔 crontab 模块不可用: %s' % e
            if self._logger:
                self._logger.error("创建计划任务失败: %s", msg)
            return {'status': False, 'message': msg}

        try:
            run_hour = int(run_hour) if run_hour is not None else random.randint(9, 23)
            run_minute = int(run_minute) if run_minute is not None else random.randint(0, 59)
        except (TypeError, ValueError):
            run_hour, run_minute = random.randint(9, 23), random.randint(0, 59)

        old_ids = set(_find_cron_ids())
        script = self._build_script(py_bin)

        try:
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

            # 结果判定：显式 status False 直接判失败（面板可能知道入库之外的失败，
            # 如 crontab 文件同步）；其余形态以任务是否入库为准，防止失败被误报成功
            if isinstance(result, dict) and result.get('status') is False:
                msg = str(result.get('msg') or '') or repr(result)
                if self._logger:
                    self._logger.error("创建计划任务失败（旧任务保持不动）: %s", msg)
                return {'status': False, 'message': '创建失败: %s' % msg}

            new_ids = [i for i in _find_cron_ids() if i not in old_ids]
            if not new_ids:
                if self._logger:
                    self._logger.error("创建计划任务失败: AddCrontab 返回 %r 且任务未入库", result)
                return {'status': False,
                        'message': '创建失败: AddCrontab 返回 %r 且任务未入库' % (result,)}

            new_id = max(new_ids)
            if paused:
                try:
                    pause_result = cron_obj.set_cron_status(_BtParams(id=new_id))
                    if isinstance(pause_result, dict) and pause_result.get('status') is False:
                        raise RuntimeError(str(pause_result.get('msg') or pause_result))
                    if not self._task_has_status(new_id, 0):
                        raise RuntimeError('数据库未确认暂停状态')
                except Exception as e:
                    # 不能把用户主动暂停的任务在“修复”后悄悄启用。
                    try:
                        cron_obj.DelCrontab(_BtParams(id=new_id))
                    except Exception:
                        pass
                    msg = '无法保留暂停状态: %s' % str(e)
                    if self._logger:
                        self._logger.error("创建计划任务失败（旧任务保持不动）: %s", msg)
                    return {'status': False, 'message': msg}

            # 新任务确认入库后才删旧的
            for cron_id in old_ids:
                try:
                    cron_obj.DelCrontab(_BtParams(id=cron_id))
                except Exception as e:
                    if self._logger:
                        self._logger.warning("删除旧计划任务失败: id=%s, error=%s", cron_id, str(e))
            self._dedup(cron_obj)

            if self._logger:
                self._logger.info("计划任务已就绪: 每天 %d:%02d, 解释器=%s",
                                  run_hour, run_minute, py_bin)
            return {'status': True, 'message': '计划任务已就绪（每天 %d:%02d）' % (run_hour, run_minute)}
        except Exception as e:
            if self._logger:
                self._logger.error("创建计划任务失败: %s", str(e))
            return {'status': False, 'message': '创建失败: %s' % str(e)}

    @staticmethod
    def _task_has_status(cron_id, expected):
        """从数据库确认状态，避免只相信不同宝塔版本的 API 返回形态。"""
        try:
            conn = sqlite3.connect(_cron_db_path())
            try:
                row = conn.execute(
                    'SELECT status FROM crontab WHERE id = ?',
                    (cron_id,),
                ).fetchone()
            finally:
                conn.close()
            return bool(row) and row[0] == expected
        except Exception:
            return False

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
        """查询计划任务状态

        必须区分「确认不存在」与「查询失败」：吞掉异常一律返回 exists=False，会让
        add_cert 的「不存在就建」在一次瞬时 DB 锁定时重建用户正常的任务，
        也会让面板把查询故障显示成「未设置」。查询失败时带 error 字段。
        """
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
        except Exception as e:
            return {'exists': False, 'error': str(e)}

        if not row:
            return {'exists': False}
        try:
            hour = int(row['where_hour'] or 0)
            minute = int(row['where_minute'] or 0)
        except (TypeError, ValueError):
            hour, minute = 0, 0
        paused = row['status'] != 1
        return {
            'exists': True,
            'id': row['id'],
            'status': '已暂停' if paused else '运行中',
            'paused': paused,
            'hour': hour,
            'minute': minute,
            'cycle': '每天 %d:%02d' % (hour, minute),
            'last_run': row['addtime'] or '',
        }

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

    def _build_script(self, python_bin):
        """构建只引用仓库入口的薄任务脚本

        python_bin 由 resolve_python() 选出并已通过 import public 自检，绝不会是系统
        python3。入口脚本随插件包升级，任务数据库无需再复制完整执行逻辑；解释器路径
        作为参数保留，路径失效时由入口脚本回退 PATH 中的 python3。
        """
        return '#!/bin/bash\n/bin/bash %s %s\n' % (
            shlex.quote(CRON_RUNNER),
            shlex.quote(python_bin),
        )
