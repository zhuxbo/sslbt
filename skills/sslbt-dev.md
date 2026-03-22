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
  ├─ deployer.py     ← 证书部署（_BtParams + SetSSL + 回调）
  ├─ renew.py        ← 续签引擎（Pull/Local 两种模式）
  ├─ config.py       ← 配置读写（文件锁，证书级 API 配置，废弃字段过滤）
  ├─ cert_utils.py   ← 证书验证 + CSR 生成
  ├─ site_manager.py ← 宝塔站点管理 + 域名匹配（兼容新旧数据库分片）
  ├─ cron.py         ← 宝塔计划任务（_BtParams + 直接查库 + 每天随机时间 + cron.log 轮转）
  └─ logger.py       ← 日志（敏感信息过滤，MAX_LOG_FILES=90 自动清理）
index.html           ← 前端 UI（纯 JS，3 Tab: 证书管理/设置/日志）
```

## 宝塔兼容

### _BtParams
宝塔 API 方法（SetSSL、AddCrontab 等）对参数对象混用属性访问（`get.name`）和字典访问（`get['name']`、`'key' in get`）。`_BtParams(dict)` 同时支持两种方式，在 deployer.py 和 cron.py 中各有定义。

### 数据库分片
新版宝塔将表迁移到 `data/db/` 子目录：
- `sites`/`domain` 表 → `data/db/site.db`（site_manager.py）
- `crontab` 表 → `data/db/crontab.db`（cron.py）
- 旧版仍在 `data/default.db`

代码优先检查新路径，回退旧路径。

### session 持久化
`_pending_tokens` 为类变量（非实例变量），宝塔每次请求创建新实例但模块保持加载，类变量跨请求保持。

### Logger 单例陷阱
`logging.getLogger(name)` 对同名返回全局单例，宝塔每次请求 new 插件实例都会 new Logger。Logger.__init__ 先 `handlers.clear()` + `filters.clear()` 再添加，防止 handler 累积导致重复日志。

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
- 分散续签：`check_and_renew_all(spread=True)` 在证书间加动态延迟，根据需续签数量自动缩短间隔（总延迟上限 600s），仅 cron 调用启用
- 汇总日志：续签完成后记录成功/等待/失败数量
- 常量：RENEW_DEFAULT_DAYS=13, MAX_ISSUE_RETRY_COUNT=10, RENEW_SLEEP_MIN=5, RENEW_SLEEP_MAX=120, SPREAD_TOTAL_MAX=600
- 已过期证书（days_remaining < 0）不再触发续签
- deploy_multi 全部站点失败时不更新 metadata（保留重试状态）
- 单次续签上限 MAX_RENEW_BATCH=100，超出按配置文件顺序截断，剩余下次 cron 处理；紧急证书由用户手动触发

## 已知局限（无需处理，仅记录）

- 批次截断按添加顺序，不按过期紧急度排序（紧急证书用户自行手动续签）
- `_update_json` 内部 JSON 损坏的日志/备份路径无单独测试（逻辑与 `_read_json` 相同，风险低）
- `_calc_spread_delay` 的 `sleep_min` 计算值无精确断言（仅验证上限约束）
- API 指数退避（1s→2s→4s）无直接测试（API 调用全部 mock，退避逻辑简单）
- `.lock` 锁文件不主动清理（Linux 常规做法，删除反而引入竞态）

## 配置层级

每个证书必须自带 `api_url`/`api_token`，无全局 API 配置。全局配置仅保留运行参数（renew_before_days、renew_mode、release_url、update_channel）。

- `renew_before_days`：提前续签天数，全局生效，默认 13
- 废弃字段（`api_url`、`api_token`、`version`）读写时自动过滤
- 站点唯一绑定：一个站点只能绑定一个证书，add_cert / update_cert / update_cert_config 均校验
- ConfigManager 支持可选 `logger` 参数，JSON 损坏时记录 error 并创建 .bak 备份
- `add_cert` / `update_cert` / `remove_cert` 使用 `_update_json` 原子读-改-写（独立锁文件防止竞态）

## 前端约定

- `sslbt_main.py` 方法名 = 前端 `P._call('method_name', params, callback)` 的 method_name
- 证书编辑用 `update_cert_config`（原子更新 site_name/renew_mode，站点唯一绑定校验）
- `_parse_cert_domains` 只解析 `domains` 字段（逗号分隔字符串）
- 证书列表支持 checkbox 多选，顶部按钮（部署/删除）操作选中证书
- 状态标签：未绑定 → 待部署 → 已部署（有 last_deploy_at 但无 cert_expires_at）→ 正常 → 即将过期 → 已过期
- 添加证书后自动创建计划任务（如果尚未设置），失败不阻塞添加流程
- 在线升级后弹窗确认重启面板（`restart_panel` → `bt restart`）

## 命令

```bash
make test          # pytest 单元测试
make build         # 构建 ZIP
make docker-test   # 容器集成测试
```

## 运行时路径

- 插件目录：`/www/server/panel/plugin/sslbt/`
- 数据目录：`data/`（config.json、certs.json、logs/、pending-keys/）
