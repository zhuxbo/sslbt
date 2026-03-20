---
name: sslbt-dev
description: Use when developing sslbt baota panel plugin - modifying API client, deployer, renew engine, frontend UI, or config module
---

# sslbt 宝塔面板插件开发

纯 Python 宝塔面板插件，通过部署 API 获取证书，`panelSite.SetSSL()` 部署。仅用标准库，无第三方依赖。

## 架构

```
sslbt_main.py  ← 插件入口（控制器），宝塔面板调用
  ├─ api_client.py   ← 部署 API 客户端（Bearer Token + 重试）
  ├─ deployer.py     ← 证书部署（SetSSL + 回调）
  ├─ renew.py        ← 续签引擎（Pull/Local 两种模式）
  ├─ config.py       ← 配置读写（文件锁，证书级 API 配置）
  ├─ cert_utils.py   ← 证书验证 + CSR 生成
  ├─ site_manager.py ← 宝塔站点管理 + 域名匹配
  ├─ cron.py         ← 宝塔计划任务
  └─ logger.py       ← 日志（敏感信息过滤）
index.html           ← 前端 UI（纯 JS，3 Tab: 证书管理/设置/日志）
```

## 部署 API 接口

Bearer Token 认证，部署链接格式：`https://domain/api/deploy?token=xxx&order=123`

### GET /api/deploy — 查询

参数 `order`（统一）：纯数字=ID，字符串=域名，含逗号=批量。不传返回最新 active 订单。
分页参数：`currentPage`、`pageSize`（max 100）

统一分页响应：
```json
{"code": 1, "data": {"total": N, "currentPage": 1, "pageSize": 100, "data": [...]}}
```

每条数据：
```json
{
  "order_id": 123,
  "domains": "example.com,www.example.com",
  "status": "active|processing|pending|unpaid",
  "certificate": "...",       // 仅 active
  "private_key": "...",       // 仅 active
  "ca_certificate": "...",    // 仅 active
  "issued_at": "2026-01-01",  // 仅 active
  "expires_at": "2026-04-01", // 仅 active
  "file": {"path":"","content":""}  // 仅 processing + 文件验证
}
```

### POST /api/deploy — 提交 CSR / 续签

```json
{"order_id": 123, "csr": "...", "domains": "a.com,b.com", "validation_method": "delegation|file"}
```
服务端自动处理状态流转：unpaid→pay, pending→commit, active→reissue/renew。
响应为单对象（非分页）。

### POST /api/deploy/callback — 部署回调

```json
{"order_id": 123, "status": "success|failure", "deployed_at": "2026-01-01T00:00:00Z"}
```

## 部署流程

```
添加证书（用户粘贴部署链接）
  fetch_deploy_url → 提取 token/order → GET ?order=xxx → 解析 domains → 匹配站点 → add_cert

部署证书
  query_order → 取 cert/key/ca → 校验匹配 → panelSite.SetSSL() → callback

自动续签（定时任务）
  Pull: query_order → active 则部署
  Local: generate_csr → submit_csr → 等待 → query_order active → 部署
```

## 续签引擎关键逻辑

- Pull 模式：查询订单，active 且证书完整则直接部署
- Local 模式：生成 CSR → 提交 → processing 状态轮询 → active 后部署
- `_check_deploy_results()`：全部失败抛异常，部分失败记警告
- callback：全部站点成功=success，任一失败=failure
- 常量：PULL_RENEW_DEFAULT_DAY=13, LOCAL_RENEW_DEFAULT_DAY=15, SERVER_AUTO_RENEW_DAYS=14, MAX_ISSUE_RETRY_COUNT=10

## 配置层级

每个证书必须自带 `api_url`/`api_token`，无全局 API 配置。全局配置仅保留运行参数（check_interval_hours、renew_before_days、renew_mode、release_url、update_channel）。

## 前端约定

- `sslbt_main.py` 方法名 = 前端 `P._call('method_name', params, callback)` 的 method_name
- 证书编辑用 `update_cert_config`（原子更新 site_name/renew_mode，不触碰 api_token）
- `_parse_cert_domains` 只解析 `domains` 字段（逗号分隔字符串）

## 命令

```bash
make test          # pytest 单元测试
make build         # 构建 ZIP
make docker-test   # 容器集成测试
```

## 运行时路径

- 插件目录：`/www/server/panel/plugin/sslbt/`
- 数据目录：`data/`（config.json、certs.json、logs/、pending-keys/）
