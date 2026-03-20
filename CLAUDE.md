# 宝塔面板 sslbt 证书部署插件

纯 Python 宝塔面板插件，调用部署 API 获取证书，通过 `panelSite.SetSSL()` 部署。

## 命令

```bash
make test               # 单元测试
make build              # 构建 ZIP
make docker-test        # 容器集成测试
```

## 开发参考

详细架构、API 接口、部署流程见 `skills/sslbt-dev.md`。
