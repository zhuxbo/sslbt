"""Mock 宝塔 public 模块"""


def returnMsg(status, msg):
    return {'status': status, 'msg': msg}


def getMsg(key, args=None):
    return key


def checkWebConfig():
    """真实宝塔返回 True 或错误字符串"""
    return True


def serviceReload():
    """真实宝塔返回 ExecShell 的 (stdout, stderr) 元组"""
    return ('', '')


def M(table):
    """Mock 数据库操作"""
    return MockDB(table)


class MockDB:
    def __init__(self, table):
        self._table = table
        self._data = []

    def where(self, *args, **kwargs):
        return self

    def field(self, *args):
        return self

    def order(self, *args):
        return self

    def limit(self, *args):
        return self

    def select(self):
        return self._data

    def find(self):
        return self._data[0] if self._data else None

    def count(self):
        return len(self._data)
