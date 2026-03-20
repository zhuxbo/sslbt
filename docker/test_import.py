import sys, os, json
os.chdir('/www/server/panel')
sys.path.insert(0, 'class')
sys.path.insert(0, '.')

from panelPlugin import panelPlugin

pp = panelPlugin()

class FakeGet:
    tmp_path = '/www/server/panel/temp/sslbt_tmp'
    plugin_name = 'sslbt'

result = pp.input_zip(FakeGet())
print(json.dumps(result, ensure_ascii=False))
