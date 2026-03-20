"""宝塔计划任务集成模块"""

CRON_NAME = 'SSL 证书自动续签'
PLUGIN_DIR = '/www/server/panel/plugin/sslbt'


class CronManager:
    """通过宝塔 crontab 模块管理计划任务"""

    def __init__(self, data_dir, logger=None):
        self._data_dir = data_dir
        self._logger = logger

    def setup(self, interval_hours=6):
        """创建或更新计划任务"""
        # 先移除已有任务
        self.remove()

        script = self._build_script()

        try:
            import crontab
            cron_obj = crontab.crontab()

            class Params:
                pass

            params = Params()
            params.name = CRON_NAME
            params.type = 'minute-n'
            params.where1 = str(interval_hours * 60)  # 每 N 分钟
            params.hour = ''
            params.minute = ''
            params.week = ''
            params.sType = 'toShell'
            params.sBody = script
            params.sName = ''
            params.backupTo = ''
            params.save = ''
            params.urladdress = ''

            cron_obj.AddCrontab(params)
            if self._logger:
                self._logger.info("计划任务创建成功: 每 %d 小时", interval_hours)
            return {'status': True, 'message': '计划任务已创建'}
        except Exception as e:
            if self._logger:
                self._logger.error("创建计划任务失败: %s", str(e))
            return {'status': False, 'message': '创建失败: %s' % str(e)}

    def remove(self):
        """移除计划任务"""
        try:
            import crontab
            cron_obj = crontab.crontab()

            # 获取所有计划任务
            class Params:
                p = 1
                limit = 1000
                tojs = ''
                table = 'crontab'
                search = ''
                order = 'id desc'

            result = cron_obj.GetCrontab(Params())
            if not isinstance(result, dict):
                return

            for item in result.get('data', []):
                if item.get('name') == CRON_NAME:
                    class DelParams:
                        id = item['id']

                    cron_obj.DelCrontab(DelParams())
                    if self._logger:
                        self._logger.info("计划任务已删除: id=%s", item['id'])
        except Exception as e:
            if self._logger:
                self._logger.error("删除计划任务失败: %s", str(e))

    def get_status(self):
        """查询计划任务状态"""
        try:
            import crontab
            cron_obj = crontab.crontab()

            class Params:
                p = 1
                limit = 1000
                tojs = ''
                table = 'crontab'
                search = ''
                order = 'id desc'

            result = cron_obj.GetCrontab(Params())
            if not isinstance(result, dict):
                return {'exists': False}

            for item in result.get('data', []):
                if item.get('name') == CRON_NAME:
                    return {
                        'exists': True,
                        'id': item.get('id'),
                        'status': '运行中' if item.get('status') == 1 else '已暂停',
                        'cycle': item.get('cycle', ''),
                        'last_run': item.get('addtime', ''),
                    }
        except Exception:
            pass
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
