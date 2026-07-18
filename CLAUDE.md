# 宝塔面板 sslbt 证书部署插件

> **统一规范**：跨项目共通行为规范见 `deploy-spec.md`

纯 Python 宝塔面板插件，调用部署 API 获取证书，通过 `panelSite.SetSSL()` 部署。

## 命令

```bash
make test               # 单元测试
make build              # 构建 ZIP
make docker-test        # 容器集成测试
```

## 开发参考

详细架构、API 接口、部署流程见 `skills/sslbt-dev.md`。

## 规范对齐要点

- releases.json 使用规范扁平格式，客户端按 pre-release 标识过滤通道
- 迁移引擎支持 delete/rename/move/spread 四种操作
- API 客户端通过自定义 opener 实现 DNS Rebinding 防护（连接后二次校验 IP）
- 非 loopback 地址强制 HTTPS
- 升级模块复用 api_client 的 Safe Handler，HTTPS 强制 + SSRF/DNS Rebinding 防护 + 通道白名单
- 安全解压：符号链接拒绝、目录 0700、文件 0600
- 安装脚本：curl --max-filesize 限制、解压前符号链接检查
- validation_method 受域名类型约束：IP 不可选 delegation、通配符不可选 file（add/update/renew 三层校验）
- deploy_cert 私钥四级回退：API → 参数路径 → 站点已有私钥(GetSSL) → 返回 need_key 由前端弹窗收集
- SetSSL 部署：写入前 pre-flight 校验既有配置（损坏即快速失败不写入）并捕获原证书；结果白名单判定（仅 dict 且 status is True 算成功）；写入后 checkWebConfig + serviceReload 校验失败则回滚原证书；失败回调 failure 经 message 携带原因（含回滚状态）
- 计划任务创建结果双重校验：AddCrontab 显式 status False 判失败，其余形态以任务入库反查为准，不再无条件报成功
