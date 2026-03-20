"""Mock 宝塔 panelSite 模块"""


class panelSite:
    """Mock panelSite"""

    _ssl_data = {}
    _sites = [
        {
            'id': 1,
            'name': 'test.example.com',
            'path': '/www/wwwroot/test.example.com',
            'status': '1',
            'domain': 'test.example.com',
        },
        {
            'id': 2,
            'name': 'demo.example.com',
            'path': '/www/wwwroot/demo.example.com',
            'status': '1',
            'domain': 'demo.example.com,www.demo.example.com',
        },
    ]

    def GetList(self, args=None):
        return {'data': self._sites}

    def GetSSL(self, args=None):
        site_name = getattr(args, 'siteName', '')
        if site_name in self._ssl_data:
            return {'status': True, 'key': self._ssl_data[site_name]['key'],
                    'csr': self._ssl_data[site_name]['cert']}
        return {'status': False}

    def SetSSL(self, args=None):
        site_name = getattr(args, 'siteName', '')
        key = getattr(args, 'key', '')
        cert = getattr(args, 'csr', '')  # 宝塔 API 中 csr 参数是证书
        if not site_name or not key or not cert:
            return {'status': False, 'msg': '参数不完整'}
        self._ssl_data[site_name] = {'key': key, 'cert': cert}
        return {'status': True, 'msg': '设置成功'}

    @classmethod
    def reset(cls):
        cls._ssl_data = {}
