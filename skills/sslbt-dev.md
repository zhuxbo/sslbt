# sslbt 宝塔面板插件开发

纯 Python 宝塔面板插件，通过部署 API 获取证书，`panelSite.SetSSL()` 部署。仅用标准库，无第三方依赖。

## 架构

```
sslbt_main.py  ← 插件入口（控制器），宝塔面板调用
  ├─ api_client.py      ← 部署 API 客户端（Bearer Token + 重试 + Safe Handler）
  ├─ net_guard.py       ← 网络安全（SSRF/DNS Rebinding 防护，IP 黑名单）
  ├─ deployer.py        ← 证书部署（_BtParams + SetSSL + 回调）
  ├─ renew.py           ← 续签引擎（Pull/Local 两种模式 + 文件验证集成）
  ├─ file_verifier.py   ← 文件验证（ACME 验证文件放置/清理）
  ├─ config.py          ← 配置读写（文件锁 + 数据驱动迁移引擎）
  ├─ cert_utils.py      ← 证书验证 + CSR 生成（支持 DNS/IP SAN）
  ├─ site_manager.py    ← 宝塔站点管理 + 域名匹配（兼容新旧数据库分片）
  ├─ updater.py         ← 在线升级（releases.json 解析 + 安全下载 + 校验）
  ├─ cron.py            ← 宝塔计划任务（setup 首建随机时间 / refresh 保留原时间只重写脚本；
  │                         resolve_python() 经 subprocess 验证解释器能 import public，绝不回落
  │                         系统 python3；先建后删；get_status 三态；AddCrontab 结果双重校验）
  └─ logger.py          ← 日志（对格式化后完整消息脱敏，覆盖 dict/list 参数，MAX_LOG_FILES=90 自动清理）
index.html              ← 前端 UI（纯 JS，3 Tab: 证书管理/设置/日志）
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
{"order_id": 123, "status": "success|failure", "deployed_at": "2026-01-01T00:00:00Z", "message": "失败原因（可选，仅 failure 携带；客户端脱敏并截断至 ≤256 字符，服务端上限 500）"}
```

## 部署流程

```
添加证书（用户粘贴部署链接）
  fetch_deploy_url → 提取 token/order → 经统一 APIClient(HTTPS 强制+SSRF+DNS Rebinding)query_batch → 解析 domains（DNS+IP SAN） → 匹配站点 → add_cert

部署证书
  query_order → 检查 order_id 变更 → active: 取 cert/key/ca → 校验匹配 → pre-flight 配置检查 → 捕获原证书 → panelSite.SetSSL() → 写入后校验 → callback

SetSSL 部署：写入前 pre-flight 校验既有配置（`check_web_config()`，损坏则快速失败不写入，phase='preflight'）并捕获站点原证书（GetSSL 为主，按 'csr' 键=证书解析——宝塔各版本未逐一确证；拿不到时回退读 vhost/cert、vhost/ssl 目录文件）；
结果白名单判定（仅「dict 且 status is True」算成功，异常形态一律判失败）；
写入后 `_verify_web_service()` 返回 `(kind, message)` 分两级判定：
- `kind='config'`（checkWebConfig 失败）：pre-flight 已证明写前配置是好的，因果闭合归因本次写入 → **回滚**原证书，phase='config'
- `kind='reload'`（配置有效但 serviceReload 失败）：属服务层问题，回滚证书无助于修复且多一次写入 → **不回滚**，phase='reload'，提示证书已写入待人工重载
- 「reload 后再跑一次 checkWebConfig」无意义：它校验磁盘配置语法，重载前后磁盘内容未变，结果必然相同
- reload 失败特征用结构化正则 `_RELOAD_ERROR_PATTERNS`（`[emerg|alert|crit|error]`、`Job for ... failed` 等），**禁止裸词匹配**——nginx/apache 正常输出常见 `error_log`/`ErrorLog`，裸词 'error' 会把成功部署误判并触发回滚
             → processing + file: FileVerifier.place_file() → 等待 CA 验证

自动续签（定时任务）
  Pull: query_order → 检查 order_id 变更 → active 则部署
  Local: generate_csr → submit_csr(validation_method) → 检查 order_id 变更 → processing + file → 放置验证文件 → 等待 → active → 部署 + 清理验证文件
```

## 文件验证流程

- `FileVerifier.place_file(file_info, site_names)`：将 ACME 验证文件写入绑定站点的根目录
- `FileVerifier.cleanup_files(placed_paths)`：证书签发后清理验证文件
- 路径安全校验：必须以 `.well-known/` 开头，不含 `..`
- 触发场景：Local 模式续签（`_submit_new_csr`/`_handle_processing`）和手动部署（`deploy_cert`）
- metadata 存储：`pending_file_verify`（文件信息）、`pending_verify_paths`（已放置路径列表）、
  `verify_file_place_failed`（未覆盖全部站点，面板告警）
- 清理时机：证书签发成功、状态异常（**无 CSR 超时机制**，三仓均无）
- 重放判据是「上次是否真的放上去了」而非「file_info 变没变」：`_verify_files_intact` 要求
  列表非空 + 覆盖全部绑定站点 + 文件仍在盘上，三者缺一即重放。首轮放置失败时
  `pending_file_verify` 照样落盘，只比对它会让文件一次都写不进去而订单永远卡在 processing

## 续签引擎关键逻辑

- 行为契约以 `deploy-spec.md`（§1.5/§2.6/§2.8/§3.2/§3.5/§5.1/§5.2）为准，本节仅记实现要点
- Pull 模式：查询订单，active 且证书完整则直接部署
- Local 模式：派生策略 → 生成 CSR → 提交 → processing 状态轮询 → active 后部署
- 计数分离与终止（`config` 常量）：签发 `issue_retry_count`（CSR 提交）与部署 `deploy_attempt_count` 分别计数、各自 `>= 10` 触顶；触顶置 `last_issue_state=CAPPED` 并记 `metadata.cap_stage`（issue/deploy/legacy）静默；已过期置 `EXPIRED` 静默；剩余有效期 < `SAFETY_MARGIN_HOURS=24` 不启动新动作。触顶/过期/policy 阻断均不发回调。前置过滤对 `processing` 证书豁免签发触顶（CSR 已被接受，继续轮询）
- 计数递增时机 = 持久化新逻辑尝试意图：`_submit_new_csr` 提交前递增签发计数；`_begin_deploy_attempt` 部署前递增部署计数并置 `deploy_started`，崩溃恢复重放（`deploy_started` 已置位）不自增
- 回调所有权收敛：自动续签底层 `deploy_multi(send_callback=False)` 只返回结构化结果，编排层 `_deploy_and_report` 在结果落盘后统一发一次；每次成功/明确失败各尽力一报，第 10 次（最后一次）失败 message 追加「已达重试上限」；签发失败不上报（仅本地日志与计数）。手动 `deploy`/`setup` 默认 `send_callback=True`，语义不变
- 环境闸门（`_check_deploy_environment`，在 `_begin_deploy_attempt` 之前）：`check_web_config()` 失败时**不递增** `deploy_attempt_count`、不置 `deploy_started`，写 `metadata.last_deploy_block_reason`/`last_deploy_block_at`（经 `_compact_reason` 压平多行并限长 300）供面板展示，**发一次 failure 回调**，`_deploy_and_report` 抛 `RuntimeError` 让本轮记为失败。
  - 不计数：检查天然全局，任一无关站点的坏配置都会命中；计数会让全部证书 10 轮后静默 CAPPED，且配置修好还要人工解除。不计数则修好即自愈
  - 发回调：服务端（ssl-manager `AutoDeployReport` + `DeployFailureReminderCommand`）按「订单最新一条上报仍为 failure」做状态判定并按 TTL 提醒；不上报会让该订单从服务端失败视图消失，才是真正的静默过期。环境阻断不在 spec §2.8 的免回调清单（触顶/过期/policy）内
  - 记为失败而非等待：既然已按 failure 上报服务端，本地汇总（`_renew_summary`）与面板必须同口径，否则用户看到「续签成功」却什么都没发生
  - 阻断标记清除：环境恢复时 `_clear_deploy_block` 清、部署成功时 `deploy_multi` 的 metadata 重置一并清，避免面板留存已消失的旧原因
  - 手动路径同样有闸门：`_deploy_cert_locked`/`_deploy_all_locked` 开头调用 `_env_block_error()`，在查询 API 之前直接返回原因，而不是让每个站点各失败一次只留下「0 成功 N 失败」
- 终态恢复入口 `reset_issue_state`：清 `last_issue_state`/`cap_stage`/两个计数/`deploy_started`/阻断原因。签发触顶且订单卡在 pending 的证书，自动路径被终态跳过、手动部署又要求订单 active，本入口是唯一出路
- 恢复纪律（response-loss）：CSR 提交前原子持久化 pending key + `pending-csr.pem` 作为在途标记；`submit_csr` 传输不确定（`APIError.transport=True`：超时/断连/解析失败）保留 pending，明确业务拒绝（含服务端未接收提交，以错误信息而非状态表达）才清理；下轮 `_has_pending_csr` → `_recover_pending_submit` 只查询订单、绝不重复 POST：`pending`/`processing`/`approving`/`active` 归一 `processing`（不增计数、不重生 CSR），其他状态为订单终态，持久化后停止等待人工处理
- 查询状态归一：服务端提交响应只会是 `pending`/`processing`；查询在 `processing → active` 之间可能出现短暂中间态 `approving`，三者统一归一 `processing` 继续等待；`active` 之后的状态为订单终态。终态持久化到 `last_issue_state` 后每轮仍查询一次（可自愈），但状态未变化时不重复记 error、不重复落盘
- 策略派生 `derive_or_validate_renew_policy`（`config`，唯一权威）：SAN 含 IP 强制 `renew_mode=local` + `validation_method=file`；DNS 校验兼容性。add_cert/update_cert_config/batch_set_renew_policy/续签提交统一调用
- 私钥回退（deploy-spec §5.3）：deploy_cert 中按 API → 参数路径 → 站点已有私钥(GetSSL) → 弹窗粘贴 四级回退，所有来源均需 verify_cert_key_match 校验
- 文件验证：CSR 提交返回 file 字段时自动放置，签发成功/状态异常时清理（无超时机制）
- `_check_deploy_results()`：全部失败抛异常；**部分失败同样抛异常**——回调本就报 failure，此前返回 True 造成本地与服务端双口径
- callback message：仅 failure 携带各站点失败原因摘要（含回滚状态、可能的「已达重试上限」标注）；上限 `CALLBACK_MESSAGE_MAX=256`，**先脱敏后截断**（`sanitize()` 过滤 Bearer/私钥/token 后再截断）；success 不带 message
- 分散续签：`check_and_renew_all(spread=True)` 在证书间加动态延迟，根据需续签数量自动缩短间隔（总延迟上限 600s），仅 cron 调用启用
- 汇总日志：续签完成后记录成功/等待/失败数量
- cron 注册：`_build_script()` 烧入 `resolve_python()` 自检通过的解释器；`install.sh` 与 `updater.do_update()` 均调 `refresh()`（脚本正文存在宝塔 crontab 库、不在 PLUGIN_DIR 内，不在这两处刷新则修正永远到不了存量安装）；`add_cert` 仅在**确认不存在**时创建，查询失败不动现有任务
- 续签状态：每次运行结束写 `data/renew_status.json`（last_run/total/success/pending/failure，原子写 0600），面板经 `get_renew_status` 展示「最近续签」
- 站点删除自愈（两轮确认）：`deploy_multi` 部署前查一次 `SiteManager.get_sites()` 复用清单检测站点存在；`get_sites` 查询失败（DB 缺失/锁定/表结构漂移）抛 `SiteQueryError` 与「确认零站点」严格区分，失败或清单为空时放弃本轮删除判定（保守视为全部存在，不计数、不解绑）；仅当清单查询成功且非空时才对不在清单中的站点计数——首轮仅记「疑似删除」（`site_missing`，按 failure 上报但不解绑），连续第二轮（计数达 `SITE_MISSING_CONFIRM_THRESHOLD=2`，且两轮间隔 ≥ `SITE_MISSING_MIN_INTERVAL_HOURS=12` 小时）确认后才解除绑定并持久化，缩小迁移/重装中途不完整快照误清绑定的破坏半径；缺失计数存于证书 `metadata.site_missing_counts`，站点恢复/解绑后自动清零；`site_missing`/`site_removed` 均按 failure 上报（与部署回调 failure 语义一致），其余站点继续部署，解绑后不再重复失败
- 常量：RENEW_DEFAULT_DAYS=14, MAX_ISSUE_RETRY_COUNT=10, MAX_DEPLOY_ATTEMPT_COUNT=10, SAFETY_MARGIN_HOURS=24, RENEW_SLEEP_MIN=5, RENEW_SLEEP_MAX=120, SPREAD_TOTAL_MAX=600（计数与状态常量集中在 `config`，`renew` 复用）
- 已过期证书（剩余 ≤ 0）转 `EXPIRED` 静默终止，不再触发续签、不发回调
- deploy_multi 全部站点失败时不更新 metadata（保留重试状态）
- 单次续签上限 MAX_RENEW_BATCH=100，超出按配置文件顺序截断，剩余下次 cron 处理；紧急证书由用户手动触发
- 续费订单 ID 更新：API 返回的 `order_id` 与本地不同时，`_check_order_update` 原子更新 config（order_id + cert_name）+ 重命名 pending key 目录 + 更新内存 cert_entry，后续操作使用新 ID；冲突（新 ID 已存在）时 warn 并沿用旧 ID

- 运行环境闸门：`_do_renew_all` 开头 `probe_panel_runtime()` 探测 `public`/`panelSite` 可导入性，
  不可用即整轮中止（写 `renew_status.aborted_reason` + 面板红条），**一个回调都不发**——
  进程级故障归因不到订单，spec §2.8 的上报对象是部署结果，此时一次部署都没发生。
  成因通常是 cron 脚本回退系统 python3（缺 psutil 等面板依赖）
- `check_web_config()` 调用必须包 try：抛异常与返回错误同样走阻断路径。裸调用会让异常穿透到
  通用 except，回调发不出、原因不落盘、计数停在 0 而永不触顶
- 阻断类回调一律**变化触发**（reason 变化才发一次）：服务端 reminder 是电平驱动，一行即可让
  订单永久留在失败视图，逐日重发零信息增量却会淹没管理端列表且不被 PurgeCommand 清理
- 部署结果三组落盘（`deploy_multi`）：`site_deploy_status` 无条件写；计数清零在「任一成功」
  （与编排层 `counts_cleared=any` 镜像，不收紧到全成功——一个永久坏的站点会让整张证书十轮后
  CAPPED，健康站点跟着停更）；`cert_expires_at`/`cert_serial`/`last_deploy_at` 仅全成功才写
  （部分失败不前推 → 次日重试失败站点，而非等 76 天）
- 部分站点失败改判为失败：`_deploy_callback_decision` 本就报 failure，`_check_deploy_results`
  此前返回 True 造成本地与服务端双口径
- 证书更替检测（`_track_cert_unchanged`）：仅编排层判定、两端 serial 非空才比、连续
  `CERT_UNCHANGED_ROUNDS=2` 轮才升级 failure。手动重复部署与部分失败重试都会命中同一序列号
- 抢锁窗口按调用方参数化：cron `CRON_LOCK_WAIT=120`，面板按钮 `PANEL_LOCK_WAIT=6`（同步 HTTP）；
  抢锁失败写 `renew_lock_skip.json` 而非读改写 `renew_status.json`（那是无锁路径，会覆盖持锁方结果）
- 批次选择 `_select_batch`：processing 组限额 `MAX_RENEW_BATCH//2`，组内按 `last_attempt_at`
  轮转 + 紧急度，保证 ⌈N/100⌉ 轮内全部触达。单纯按紧急度排序无效——processing 证书正因临期才提交
- EXPIRED 自愈：判定必须在终态 `continue` **之前**，守卫为「剩余有效期可解析且 > 安全余量」，
  只清状态不动计数。站点缺失计数另有时钟护栏（回拨或间隔 > 30 天视为跳变，不递增）
- 配置降级（`ConfigManager.is_degraded`）：主配置解析失败即拒绝一切写入并让续签整轮中止。
  真正的覆盖点是 `_update_json`（`_ensure_config` 的写入被 `changed` 门住），仅拦后者挡不住
  cron 的任意一次 `update_metadata`
- 新增 metadata 字段必须同步 `config.DEPLOY_SUCCESS_RESET_KEYS` 与 `MANUAL_RESET_KEYS`，
  否则面板会留存已消失的旧原因，而测试不会变红

## 已知局限（无需处理，仅记录）

- 批次截断按添加顺序，不按过期紧急度排序（紧急证书用户自行手动续签）
- `_update_json` 内部 JSON 损坏的日志/备份路径无单独测试（逻辑与 `_read_json` 相同，风险低）
- `_calc_spread_delay` 的 `sleep_min` 计算值无精确断言（仅验证上限约束）
- API 指数退避（1s→2s→4s）无直接测试（API 调用全部 mock，退避逻辑简单）
- `.lock` 锁文件不主动清理（Linux 常规做法，删除反而引入竞态）

## 配置层级

统一 config.json，结构见 deploy-spec.md §1。每个证书自带 `api`（url/token），全局仅保留运行参数。

- `schedule.renew_before_days`：提前续签天数，默认 14，API 返回值覆写
- `schedule.renew_mode`：全局续签模式（pull/local），证书级优先
- `release_url` / `upgrade_channel`：升级地址和通道（main/dev）
- `validation_method`：证书级验证方式（`delegation` 或 `file`），空值默认服务端决定；受域名类型约束（IP 不可 delegation，通配符不可 file）。派生统一走 `derive_or_validate_renew_policy()`（SAN 含 IP 强制 local/file），add_cert/update_cert_config/batch_set_renew_policy/续签提交均调用
- `metadata`：新增 `deploy_attempt_count`（部署计数，从零起算）；`last_issue_state` 取值扩为 `""`/`processing`/`CAPPED`/`EXPIRED`/`policy_blocked_needs_setup`；触顶阶段记于 `cap_stage`（见 deploy-spec §1.5）
- 站点唯一绑定：一个站点只能绑定一个证书，add_cert / update_cert / update_cert_config 均校验
- 数据驱动迁移引擎：delete/rename/move/spread 字段迁移 + 计算型语义迁移（`_migrate_cert_semantics`：pending 归一 processing、旧计数 `>=10` 立即 CAPPED(legacy)、旧非法 IP 配置 IP+pull/IP+delegation 进 `policy_blocked_needs_setup` 不自动改配置），仅在 `_ensure_config` 加载时一次性执行并持久化，不补发历史
- ConfigManager 支持可选 `logger` 参数，JSON 损坏时记录 error 并创建 .bak 备份
- `add_cert` / `update_cert` / `remove_cert` / `update_order_id` 使用 `_update_json` 原子读-改-写（独立锁文件防止竞态）

## 安全机制

网络与升级安全详见 deploy-spec §10，本仓要点：

- **网络出口统一 `APIClient`**（无裸 `urlopen`），三重防线：
  - HTTPS 强制——非 loopback 地址必须 HTTPS，仅 `localhost`/`127.0.0.1`/`::1` 允许 HTTP
  - SSRF——`net_guard.check_ssrf()` 解析主机名后对内网 IP 段（`10/8`、`172.16/12`、`192.168/16`、`127/8`、`169.254.169.254`、`fc00::/7` 等）黑名单拦截
  - DNS Rebinding——自定义 opener（`_SafeHTTPConnection`/`_SafeHTTPSConnection`）在 TCP 连接建立后用 `getpeername()` 取实际对端 IP 二次校验，防解析与连接之间的地址替换
- **升级模块（`updater.py`）复用 api_client 的 Safe Handler**：`build_opener(_SafeHTTPHandler, _SafeHTTPSHandler)`，同样 HTTPS 强制 + SSRF + DNS Rebinding；通道白名单 `_validate_channel`（仅 `main`/`dev`，防路径遍历）
- **releases.json**：结构和通道语义以 `deploy-spec.md` 第 6 节为准；客户端下载后校验 `sslbt.zip` 的 SHA256，无校验和拒绝安装
- **安全解压 `_safe_extract`**：符号链接拒绝（`external_attr >> 28 == 0xA`）、路径遍历防护（`realpath` 前缀校验）、跳过 `data/`、目录 `0700` 文件 `0600`、解压后清除 `__pycache__`；ZIP 大小上限 10MB
- **远程安装脚本 `deploy/install.sh`**：`curl --max-filesize` 限制（releases.json 256KB、ZIP 10MB）、SHA256 校验、解压前用 Python `zipfile` 拒绝含符号链接的 ZIP、`data/` 目录保留不覆盖

## 前端约定

- `sslbt_main.py` 方法名 = 前端 `P._call('method_name', params, callback)` 的 method_name
- 证书编辑用 `update_cert_config`（原子更新 site_name/renew_mode/validation_method，站点唯一绑定校验 + 策略派生；IP 证书 UI 禁用 pull/delegation，后端 `derive_or_validate_renew_policy` 兜底强制 local/file）
- 批量续签策略用 `batch_set_renew_policy`（一次后端调用、逐证书原子更新与派生：含 IP 强制 local/file，DNS 采用请求值，不兼容跳过并报告；非整批事务，中途异常重跑即收敛）；`batch_set_renew_mode`/`batch_set_validation_method` 为兼容旧入口
- `_parse_cert_domains` 优先从证书 PEM 提取域名（DNS + IP SAN），未签发时回退 API 域名
- 部署时若无匹配私钥，返回 `need_key: true`，前端弹窗让用户粘贴 PEM 后重新调用 `deploy_cert(private_key=...)`
- 证书列表支持 checkbox 多选，顶部按钮（部署/删除）操作选中证书
- 状态标签：`_haltInfo()` 的终态判定**优先于**到期判定——CAPPED（含 cap_stage）/EXPIRED/policy_blocked/订单异常显示为「已停更」「需重设续签策略」等，绝不显示「正常」；这些状态不发回调，面板是用户侧唯一提示入口。其余顺序：未绑定 → 待部署 → 已部署（有 last_deploy_at 但无 cert_expires_at）→ 正常 → 即将过期 → 已过期
- 统计条含「已停更」计数；详情页「续签状态」行展示终态、签发/部署计数与环境阻断原因，终态时显示「恢复自动续签」按钮调用 `reset_issue_state`
- 部署互斥：`deploy_cert`/`deploy_all` 与 cron 续签共用 `data/renew.lock`（实例内可重入，跨进程先重试 `LOCK_RETRIES` 次再放弃，提示语 `BUSY_MSG`）。前端「添加并部署」必须**串行**发起，并发会互相抢锁失败
- 部分站点失败时 `deploy_cert`/`deploy_all` 返回 `status: False`，但站点状态已变化，前端一律刷新列表并清缓存（`need_key` 分支除外）
- 添加证书后自动创建计划任务（如果尚未设置），失败不阻塞添加流程
- 在线升级成功后由用户点击按钮刷新页面加载新版本，不自动重启面板；前端以 sessionStorage 记录目标版本，刷新后仅执行一次 `get_config` 健康/版本校验，失败或版本不符时提示用户重启宝塔面板，正常则静默清除标记，不轮询。`sslbt_main.py` 顶部热更新机制负责清除旧 `lib` 子模块缓存

## 命令

```bash
make test          # pytest 单元测试
make build VERSION=1.2.3  # 构建 ZIP
make finish-check  # 自动化完成门禁
make docker-test   # 容器集成测试（nginx/apache 双环境，安装/部署/续签三段）
```

docker-test 说明：mock-api 从同级仓 `../sslctl/docker/test/mock-api` 构建；宝塔容器由官方镜像
`/www.tar.gz` 离线还原面板，entrypoint 注册测试站点并起 loopback 转发（插件经
`http://127.0.0.1:18080` 访问 mock-api，满足「非 loopback 强制 HTTPS」约束）；
容器内插件一律用面板 pyenv（Python 3.7）执行，系统 python3 缺面板依赖。

## 运行时路径

- 插件目录：`/www/server/panel/plugin/sslbt/`
- 数据目录：`data/`（config.json、certs/、logs/、pending-keys/）
