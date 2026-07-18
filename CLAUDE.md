# 宝塔面板 sslbt 证书部署插件

纯 Python 宝塔面板插件（仅标准库、零第三方依赖），调用部署 API 获取证书，通过 `panelSite.SetSSL()` 部署到站点，支持自动续签。

> **维护指引**：保持本文件精简，仅包含项目概览和快速参考。详细规范写入 `skills/` 目录。
>
> **统一规范**：跨项目共通行为规范见 `deploy-spec.md`

## 核心指令

- **不要自动提交** - 完成修改后等待用户确认"提交"再执行 git commit/push
- **测试发现 bug 必须修复代码** - 测试的目的是发现 bug 并修复，绝不修改测试去迎合错误的代码
- **零第三方依赖** - 只用 Python 标准库 + 宝塔运行时（`panelSite`/`public`），不引入 requests/cryptography 等第三方包

## 项目结构

```text
src/                 # 源代码 = 插件 ZIP 包内容
  sslbt_main.py      # 插件入口（控制器），方法名 = 前端 P._call 的 method_name
  index.html         # 前端 UI（纯 JS，3 Tab：证书管理/设置/日志）
  info.json          # 插件元信息（版本号由构建脚本注入）
  install.sh         # 宝塔插件注册脚本
  lib/               # 核心模块：api_client / net_guard / deployer / renew /
                     #   file_verifier / config / cert_utils / site_manager /
                     #   updater / cron / logger
tests/               # pytest 单测 + mock_bt/（宝塔运行时桩：panelSite/public）
deploy/              # 远程安装脚本（curl | bash）
scripts/  build/     # 构建脚本、发布脚本及配置
docker/              # 容器集成测试（宝塔面板 + mock-api）
skills/              # 开发规范（sslbt-dev.md）
```

## 命令

```bash
make test          # pytest 单元测试
make build         # 构建插件 ZIP（需 VERSION，产物 dist/sslbt.zip）
make lint          # flake8（行宽 120）
make release       # 构建并发布到远程服务器（需 VERSION）
make docker-test   # 容器集成测试（nginx/apache 双环境）
```

## 文档索引

- 架构、宝塔兼容陷阱、部署/续签/文件验证流程、部署 API 接口、安全机制、前端约定、配置层级 → `skills/sslbt-dev.md`
- 跨项目共通规范（回调契约、续签语义、站点绑定/解绑、升级发布等）→ `deploy-spec.md`
- 发布流程 → `.claude/commands/remote-release.md`
- 完成前检查清单 → `.claude/commands/finish-check.md`
