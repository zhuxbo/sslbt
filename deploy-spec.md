# SSL 证书部署工具统一规范

跨平台 SSL 证书自动部署工具的共通行为规范。定义配置文件结构、API 接口、续签状态机、一键部署、部署流程、升级协议、安装/卸载、安全规范和共享常量。

适用项目：sslctl（Linux Nginx/Apache）、sslctlw（Windows IIS）、sslbt（宝塔面板）及未来新平台实现。

## 定位

- **开发参考**：维护现有三个项目时保持行为一致
- **新项目蓝图**：新平台实现时的快速指导
- **范围**：仅定义所有项目共有的交集行为，各项目独有特性自行处理

---

## 1. 配置文件结构

公共字段统一定义，平台特有字段放在各层级的扩展区。扩展字段不得与公共字段命名冲突。

### 1.1 顶层结构

```json
{
  "release_url": "",
  "upgrade_channel": "main",
  "schedule": {},
  "certificates": []
}
```

| 字段              | 类型   | 说明                                        |
| ----------------- | ------ | ------------------------------------------- |
| `release_url`     | string | 升级发布地址                                |
| `upgrade_channel` | string | 升级通道：`main`（稳定版）/ `dev`（测试版） |
| `schedule`        | object | 全局调度配置                                |
| `certificates`    | array  | 证书列表                                    |

### 1.2 schedule

| 字段                | 类型   | 默认值 | 说明                            |
| ------------------- | ------ | ------ | ------------------------------- |
| `renew_mode`        | string | `pull` | 全局续签模式：`pull` / `local`  |
| `renew_before_days` | int    | 14     | 提前续签天数，由 API 返回值覆写 |

- `renew_before_days` 初始值 14，每次 API 交互后用服务端返回值更新
- 证书级 `renew_mode` 优先于全局设置

### 1.3 证书配置（certificates[] 元素）

| 字段                | 类型     | 说明                                                   |
| ------------------- | -------- | ------------------------------------------------------ |
| `cert_name`         | string   | 证书名称（如 `example.com-12345`）                     |
| `order_id`          | int      | 订单 ID                                                |
| `enabled`           | bool     | 是否启用                                               |
| `domains`           | string[] | 域名列表                                               |
| `renew_mode`        | string   | 证书级续签模式，空串表示使用全局 `schedule.renew_mode` |
| `validation_method` | string   | 验证方式：`file` / `delegation`                        |
| `api`               | object   | 证书级 API 配置                                        |
| `metadata`          | object   | 证书元数据                                             |

### 1.4 api

| 字段    | 类型   | 说明         |
| ------- | ------ | ------------ |
| `url`   | string | API 端点地址 |
| `token` | string | Bearer Token |

每个证书独立的 API 配置，支持不同证书来自不同 API 源。

### 1.5 metadata

| 字段                | 类型   | 说明                                                           |
| ------------------- | ------ | -------------------------------------------------------------- |
| `last_deploy_at`    | string | 最后部署时间（RFC3339）                                        |
| `cert_expires_at`   | string | 证书过期时间（RFC3339）                                        |
| `cert_serial`       | string | 证书序列号                                                     |
| `csr_submitted_at`  | string | CSR 提交时间（仅 local 模式）                                  |
| `last_csr_hash`     | string | 上次 CSR 的 SHA256 哈希                                        |
| `last_issue_state`  | string | 签发状态：`""` / `processing` / 其他（异常状态，等待人工处理） |
| `issue_retry_count` | int    | CSR 提交重试计数                                               |

### 1.6 扩展区约定

顶层、证书级、metadata 级均可存在平台特有字段。规范不定义扩展字段的内容，各实现自行添加。

绑定模型是主要的平台差异点，不纳入公共字段：

| 平台         | 证书→站点 | 站点→证书 | 扩展字段示例                  |
| ------------ | --------- | --------- | ----------------------------- |
| Nginx/Apache | 1:N       | 1:1       | `bindings[]`（按站点）        |
| IIS          | 1:N       | 1:N       | `bind_rules[]`（按域名:端口） |
| 宝塔         | 1:N       | 1:1       | `site_name[]`（站点名列表）   |

### 1.7 配置文件迁移

规范演进可能引入新字段、重命名字段或移除废弃字段。各实现在以下时机检查并校正配置文件：

- **升级后首次运行**：检测配置版本，补充缺失的新字段（使用默认值），清除已废弃字段
- **重新执行 setup**：基于 API 返回数据重新生成配置，保留用户已有的绑定和平台扩展字段

迁移原则：

- 升级是非连续的（可能从任意旧版本直接升级到最新版），不依赖中间版本的过渡字段
- 新增字段填入默认值，不影响现有行为
- 废弃字段静默移除，不报错
- 用户数据（证书配置、绑定关系、API 凭据）永远不丢失
- 如果旧配置缺少当前版本必需的数据且无法推导，提示用户重新执行 `setup` 部署

#### 通用迁移方法

各实现应采用**数据驱动**的通用迁移引擎，将迁移知识与迁移逻辑分离。

**设计原则**：

1. **规则与引擎分离**：所有字段变更以声明式规则表达（规则表/映射表），引擎本身不含任何字段名等业务知识
2. **操作原语统一**：各实现支持的基本操作类型一致（具体语法随语言不同）：
   - `delete` — 移除废弃字段
   - `rename` — 字段重命名（目标已存在则保留新值）
   - `move` — 扁平字段移入子对象（目标已存在则不覆盖）
   - `spread` — 顶层字段分发到数组元素（如全局 API → 各证书，仅补全缺失字段）
   - 平台可按需扩展操作类型，但以上四种为公共基础
3. **默认值自动填充**：递归对比当前数据与默认结构，补齐缺失字段、校正类型不匹配（如 string → list）
4. **幂等性**：同一规则重复执行不产生副作用，迁移后的配置再次迁移结果不变
5. **顺序无关**：支持从任意旧版本直接升级，不依赖中间版本的过渡状态
6. **旧文件合并**：配置文件拆分/合并变更通过声明旧文件名和目标字段表达，写入成功后再删除旧文件

**执行流程**（程序加载配置时完成）：

```
1. 读取当前配置文件
2. 合并旧文件（如有）
3. 遍历规则表，对全局配置和每个证书条目执行字段迁移
4. 递归补齐默认值
5. 如有变化，持久化写回（失败不影响本次加载）
```

**扩展方式**：添加新迁移只需在规则表追加条目；默认值变更只需修改默认结构定义。

### 1.8 工作目录结构

各平台安装目录不同，但内部布局统一：

```
{install_dir}/
├── config.json          # 统一配置文件
├── certs/               # 证书存储（按站点名或证书名组织）
├── pending-keys/        # 待确认私钥（local 模式，按证书名组织）
└── logs/                # 日志文件
```

平台可增加额外目录（如 sslctl 的 `backup/`、`scan-result.json`），不做统一要求。

---

## 2. Deploy API 接口规范

### 2.1 认证

`Authorization: Bearer {token}`

### 2.2 通用响应格式

```json
{
  "code": 1,
  "msg": "success",
  "data": {}
}
```

`code = 1` 表示成功，其他值为错误。

### 2.3 查询证书

```
GET /api/deploy?order={order_id}
GET /api/deploy?order={domain}
GET /api/deploy?order={id1,id2,domain1,...}  （批量，逗号分隔，上限 100）
```

响应 data（分页格式）：

```json
{
  "total": 1,
  "page": 1,
  "page_size": 100,
  "data": [CertData, ...],
  "renew_before_days": 14
}
```

空参数时返回最新 active 订单，支持 `page` 和 `page_size` 参数。

### 2.4 CertData 结构

| 字段             | 类型        | 说明                                       |
| ---------------- | ----------- | ------------------------------------------ |
| `order_id`       | int         | 订单 ID                                    |
| `status`         | string      | 证书状态                                   |
| `domains`        | string      | 域名列表（逗号分隔）                       |
| `certificate`    | string      | 证书 PEM（仅 active）                      |
| `ca_certificate` | string      | 中间证书 PEM（仅 active）                  |
| `private_key`    | string      | 私钥 PEM（仅 active，可选）                |
| `issued_at`      | string      | 签发日期（YYYY-MM-DD）                     |
| `expires_at`     | string      | 过期日期（YYYY-MM-DD）                     |
| `file`           | object/null | 文件验证信息（仅 processing + 文件验证时） |

`certificate`、`ca_certificate`、`private_key`、`issued_at`、`expires_at` 仅在 `status=active` 时返回。

### 2.5 file 结构

| 字段      | 类型   | 说明             |
| --------- | ------ | ---------------- |
| `path`    | string | 验证文件相对路径 |
| `content` | string | 验证文件内容     |

### 2.6 提交 CSR（local 模式）

```
POST /api/deploy
Content-Type: application/json
```

请求体：

| 字段                | 类型   | 说明                  |
| ------------------- | ------ | --------------------- |
| `order_id`          | int    | 订单 ID               |
| `csr`               | string | CSR PEM               |
| `domains`           | string | 域名（逗号分隔）      |
| `validation_method` | string | `file` / `delegation` |

响应 data：单个 CertData + `renew_before_days`。

### 2.7 切换自动重签

```
POST /api/deploy/auto-reissue
Content-Type: application/json
```

请求体：

| 字段           | 类型 | 说明      |
| -------------- | ---- | --------- |
| `order_id`     | int  | 订单 ID   |
| `auto_reissue` | bool | 开启/关闭 |

客户端在首次部署时根据续签模式调用：pull → `true`，local → `false`。

### 2.8 部署回调

```
POST /api/deploy/callback
Content-Type: application/json
```

请求体：

| 字段          | 类型   | 说明                  |
| ------------- | ------ | --------------------- |
| `order_id`    | int    | 订单 ID               |
| `status`      | string | `success` / `failure` |
| `deployed_at` | string | 部署时间（RFC3339）   |

响应 data 包含 `renew_before_days`。

回调为非关键路径，失败仅记日志，不影响部署结果。

### 2.9 renew_before_days 的传递

服务端在所有接口的响应 data 中返回 `renew_before_days`。客户端每次收到后更新本地 `schedule.renew_before_days`。

---

## 3. 续签状态机

### 3.1 定时触发

- 每天执行一次续签检查
- 多证书间随机延迟，分散 API 压力（见常量表）
- 单次最多处理 100 个证书

### 3.2 前置过滤

遍历证书列表，按以下条件跳过：

| 条件                                   | 处理               |
| -------------------------------------- | ------------------ |
| `enabled = false`                      | 跳过               |
| 已过期（剩余天数 < 0）                 | 跳过，等待人工处理 |
| `issue_retry_count > 10`（local 模式） | 跳过，等待人工处理 |
| 无 API 配置                            | 跳过               |

**核心原则：证书到期后不再自动发起任何操作，等待人工处理。**

### 3.3 续签模式确定

```
effective_mode = cert.renew_mode || schedule.renew_mode
```

证书级优先，回退到全局设置。

### 3.4 Pull 模式

```
查询订单 (GET /api/deploy?order={order_id})
  ├─ status=active 且有证书内容
  │   ├─ 剩余天数 ≤ renew_before_days → 部署证书
  │   └─ 剩余天数 > renew_before_days → 跳过
  ├─ status=processing → 跳过，等待下次检查
  └─ 其他状态 → 跳过
```

### 3.5 Local 模式

```
检查 metadata.last_issue_state：

== ""（初始/已完成）：
  ├─ 剩余天数 ≤ renew_before_days → 提交新 CSR
  │   ├─ 检查重试次数（> 10 → 停止，等待人工处理）
  │   ├─ 生成 CSR（仅 CN，不含 SAN），保存私钥到 pending-keys/
  │   ├─ 递增 issue_retry_count
  │   ├─ POST /api/deploy 提交 CSR
  │   ├─ 响应 status=processing → 放置验证文件，标记 processing
  │   └─ 响应 status=active → 立即部署
  └─ 剩余天数 > renew_before_days → 跳过

== "processing"（等待签发）：
  ├─ 证书已过期 → 停止，等待人工处理
  └─ 查询订单状态
      ├─ status=active → 读取 pending key，部署，清理
      ├─ status=processing → 更新验证文件（如有新的），继续等待
      └─ 其他异常状态 → 持久化实际状态到 `last_issue_state`，停止，等待人工处理
```

### 3.6 文件验证流程（local 模式）

当 API 返回 `status=processing` 且包含 `file` 字段时：

```
1. 验证 file.path 不含 ".."，确保在 .well-known/ 下
2. 收集所有启用绑定的 webroot 目录（去重）
3. 在每个 webroot 下创建验证文件：
   {webroot}/{file.path}
4. 记录已放置的文件路径到 metadata
5. 部署成功后自动清理所有验证文件
```

### 3.7 并发执行保护

防止多个进程同时执行续签（cron 重叠、手动与自动并发）：

- 续签检查开始时获取进程级锁（文件锁或 PID 文件）
- 已有进程在运行时，后来者直接退出
- 进程结束后释放锁

### 3.8 部署成功后

1. 更新 metadata：`last_deploy_at`、`cert_expires_at`、`cert_serial`
2. 清零 CSR 状态：`csr_submitted_at`、`last_csr_hash`、`issue_retry_count`、`last_issue_state`
3. 清理 pending key 和验证文件
4. 发送回调 `POST /api/deploy/callback`（非关键路径）

---

## 4. Setup 一键部署流程

所有平台的主入口，接收最少参数完成完整部署。

### 4.1 参数

| 参数            | 说明                                        |
| --------------- | ------------------------------------------- |
| `url`（必需）   | API 端点地址                                |
| `token`（必需） | Bearer Token                                |
| `order`（可选） | 订单 ID 或逗号分隔的多个 ID，不传时查询全部 |

### 4.2 流程

```
1. 查询证书信息（GET /api/deploy?order=...）
2. 检测/匹配站点（平台特定扩展点）
3. 部署证书到匹配的站点
4. 设置续签模式（调用 toggleAutoReissue）
5. 写入配置文件（api、domains、metadata 等）
6. 注册守护服务/计划任务（平台特定扩展点）
```

未指定 order 时查询该 Token 下所有 active 订单，自动匹配站点部署。

---

## 5. 部署流程

### 5.1 通用步骤

```
1. 验证证书和私钥匹配
2. 验证中间证书存在（API 部署时必需）
3. 构建完整证书链（cert + ca_certificate）
4. 平台特定部署（扩展点）
5. 更新 metadata
6. 发送部署回调
```

### 5.2 续签模式与 API 侧行为

| 模式    | API 自动重签                 | 续签发起方             |
| ------- | ---------------------------- | ---------------------- |
| `pull`  | 开启（`auto_reissue=true`）  | 服务端签发，客户端拉取 |
| `local` | 关闭（`auto_reissue=false`） | 客户端生成 CSR 提交    |

首次部署时调用 `toggleAutoReissue` 接口设置。

### 5.3 私钥来源

按优先级依次尝试：

1. API 返回的 `private_key`
2. 本地存储的私钥（pending-keys/ 或证书目录）
3. 提示用户输入 PEM 私钥内容

三个来源均需验证与证书匹配后才使用。

### 5.4 订单号变更处理

续费时服务端可能返回新的 `order_id`（旧订单状态变为 `renewed`，创建新订单）。客户端在以下场景检测并更新：

- `query` 响应中 `order_id` 与本地不同时
- `update`（CSR 提交）响应中返回新 `order_id` 时

检测到变更后立即更新本地配置中的 `order_id`，保持证书关联不断。

### 5.5 部署后域名提取

部署成功后从证书 PEM 中解析 CN 和 SAN，更新配置中的 `domains` 列表。

优先级：

1. 从已部署的证书 PEM 提取（权威来源）
2. 回退到 API 返回的 `domains` 字段（逗号分隔字符串）

确保 `domains` 始终反映实际证书内容，而非仅依赖 API 数据。

### 5.6 证书链构建

拼接顺序：服务器证书在前，中间证书在后。

```
fullchain = certificate + "\n" + ca_certificate
```

部署时根据平台需求使用 fullchain 或分别提供 cert 和 intermediate。

### 5.7 域名匹配规则

setup 自动匹配站点时使用的域名匹配逻辑：

| 类型             | 规则                     | 示例                                     |
| ---------------- | ------------------------ | ---------------------------------------- |
| 精确匹配         | 证书域名 = 站点域名      | `example.com` 匹配 `example.com`         |
| 通配符           | `*.x.com` 匹配单级子域   | `*.example.com` 匹配 `www.example.com`   |
| 通配符不匹配裸域 | `*.x.com` ≠ `x.com`      | `*.example.com` 不匹配 `example.com`     |
| 通配符不匹配多级 | `*.x.com` ≠ `a.b.x.com`  | `*.example.com` 不匹配 `a.b.example.com` |
| IP 证书          | 精确匹配，不走通配符     | `192.168.1.1` 仅匹配 `192.168.1.1`       |
| IDN 域名         | 自动转换 Punycode 后匹配 | `中文.com` → `xn--fiq228c.com`           |

### 5.8 CSR 生成规范

- 仅需 Common Name（CN），不含 SAN
- IP 证书的 CN 也写 IP 地址
- 默认密钥类型：RSA 2048
- CSR 生成后计算 SHA256 哈希用于去重

---

## 6. 升级协议

### 6.1 版本信息获取

```
GET {release_url}/releases.json
```

单文件，通道名做顶层 key：

```json
{
  "main": {
    "latest": "1.2.0",
    "versions": [
      {
        "version": "1.2.0",
        "released_at": "2026-03-20",
        "checksums": {
          "sslctl-linux-amd64.gz": "sha256:a1b2c3...",
          "sslctl-linux-arm64.gz": "sha256:d4e5f6..."
        }
      }
    ]
  },
  "dev": {
    "latest": "1.3.0-rc2",
    "versions": [
      {
        "version": "1.3.0-rc2",
        "released_at": "2026-03-28",
        "checksums": {
          "sslctl-linux-amd64.gz": "sha256:m4n5o6..."
        }
      }
    ]
  }
}
```

| 字段                   | 说明                                                   |
| ---------------------- | ------------------------------------------------------ |
| `{channel}`            | 顶层 key 为通道名（`main` / `dev`）                   |
| `{channel}.latest`     | 该通道最新版本号（不带 v 前缀）                        |
| `{channel}.versions`   | 该通道版本列表，按发布时间倒序，每通道最多保留 5 条    |
| `version`              | 版本号（不带 v 前缀），目录名加 v 前缀（`v1.2.0`）    |
| `released_at`          | 发布日期（YYYY-MM-DD）                                 |
| `checksums`            | 按文件名索引的 SHA256 哈希，支持多平台产物              |

平台可在版本条目中增加扩展字段（如 sslctl 的 `signature`），与 `checksums` 同级。

客户端根据自身平台拼出文件名，在 `checksums` 中查找对应哈希。未找到 = 该版本不支持当前平台。

### 6.2 通道

| 通道   | 说明                   |
| ------ | ---------------------- |
| `main` | 正式版，仅含稳定版本   |
| `dev`  | 测试版，含 pre-release |

客户端根据 `upgrade_channel` 配置读取对应通道。`latest` 就是该通道最新版，无需额外过滤。

### 6.3 升级流程

```
1. 获取 {release_url}/releases.json
2. 读取 [upgrade_channel] 通道，比较 latest 与当前版本
3. 无更新则退出；有更新则在 versions 中找到 latest 对应条目
4. 拼出文件名，从 checksums 获取哈希
5. 下载：GET {release_url}/{channel}/v{version}/{filename}
6. SHA256 校验
7. 平台特定安装（扩展点）
```

### 6.4 安全要求

- 下载必须 HTTPS
- SHA256 校验必过
- 各平台可增加额外验证（如 Ed25519 签名、Authenticode 签名）

---

## 7. 安装脚本规范

### 7.1 参数

| 参数                    | 说明                                   |
| ----------------------- | -------------------------------------- |
| `releaseDomain`（必需） | 发布服务器域名，脚本自动拼接为完整 URL |
| `--version <ver>`       | 安装指定版本                           |
| `--dev`                 | 安装测试通道版本                       |
| `--force`               | 强制重新安装（即使已安装相同版本）     |

### 7.2 安装流程

```
1. 解析参数，确定通道（main/dev）和版本
2. 获取 {release_url}/releases.json，从对应通道确定目标版本
3. 下载安装包
4. SHA256 校验
5. 解压安装到目标目录
6. 写入 release_url、upgrade_channel 到配置文件（已有配置和证书数据不覆盖）
7. 注册守护服务/计划任务（平台特定扩展点）
```

### 7.3 幂等性

- 重复执行 = 升级
- 已有配置文件（`config.json`）和证书数据目录不覆盖
- 服务/计划任务已存在时更新而非重复创建

---

## 8. 构建与发布

### 8.1 构建

各平台构建方式不同（Go binary / Python zip），但遵守统一约定：

- **版本号注入**：构建时注入版本号（语义化版本 x.y.z），运行时可通过 `--version` 查看
- **产物命名**：`{product}-{os}-{arch}.{ext}`，版本在目录路径 `{channel}/v{version}/` 中体现。单二进制产物仅 gz 压缩（如 `sslctl-linux-amd64.gz`），多文件产物使用 tar.gz 或 zip
- **校验文件**：每个产物附带 SHA256 校验文件（`{filename}.sha256`）

### 8.2 发布目录结构

发布服务器上的目录布局：

```
{releaseDomain}/{product}/
├── releases.json                    # 版本索引（单文件，含所有通道）
├── main/
│   ├── v1.2.0/
│   │   ├── sslctl-linux-amd64.gz
│   │   └── sslctl-linux-arm64.gz
│   └── v1.1.0/
│       └── ...
└── dev/
    └── v1.3.0-rc2/
        └── ...
```

### 8.3 发布流程

```
1. 构建产物，注入版本号
2. 生成 SHA256 校验文件
3. 上传产物和校验文件到对应通道目录
4. 更新 releases.json（追加版本、更新 latest 字段）
5. 各平台可增加额外签名步骤（Ed25519、Authenticode 等）
```

### 8.4 releases.json 维护

格式定义见 6.1 节。发布脚本负责：
- 根据版本类型写入对应通道（正式版 → `main`，pre-release → `dev`）
- 追加新版本条目（含 checksums、released_at）到对应通道 `versions` 首位，更新 `latest`
- 每通道保留最近 5 个版本条目，清理超出的旧条目及 `{channel}/v{version}/` 产物目录

---

## 9. 卸载流程

### 9.1 卸载步骤

```
1. 停止并移除守护服务/计划任务
2. 删除程序文件
3. 可选：清理配置和证书数据（需用户确认）
```

### 9.2 卸载原则

- 默认保留配置和证书数据，防止误删
- 提供 `--purge` 或交互确认选项，允许完全清理
- 卸载不影响已部署到 Web 服务器的证书（证书已复制到站点目录）

---

## 10. 安全规范

各平台实现必须遵守的共通安全要求。

### 10.1 网络安全

- **HTTPS 强制**：API 请求必须使用 HTTPS，仅 localhost/127.0.0.1 允许 HTTP
- **TLS 版本**：最低 TLS 1.2
- **SSRF 防护**：阻止访问内网 IP（10.0.0.0/8、172.16.0.0/12、192.168.0.0/16）、未指定地址（0.0.0.0、::）和云元数据地址（169.254.169.254）
- **DNS Rebinding 防护**：TCP 连接时二次校验目标 IP

### 10.2 文件系统安全

- **符号链接防护**：读写文件前检查路径是否为符号链接，拒绝操作符号链接目标
- **路径遍历防护**：拼接路径后验证结果仍在预期目录内，拒绝包含 `..` 的输入
- **原子写入**：配置文件和证书写入使用临时文件 + rename，防止写入中断导致数据损坏
- **文件权限**：私钥文件 0600，配置文件 0600，目录 0700
- **配置文件锁**：并发操作时使用文件锁保护，防止多进程同时写入

### 10.3 证书与密钥

- **证书验证**：部署前验证证书格式、有效性、证书与私钥匹配
- **中间证书必需**：API 部署时必须包含中间证书，缺失则拒绝部署
- **私钥保护**：私钥写入使用原子操作，local 模式下新私钥先存 pending-keys/，签发成功后再移到正式位置
- **大小限制**：私钥 ≤ 16 KB，证书链 ≤ 64 KB，超过则拒绝

### 10.4 日志与敏感信息

- **日志脱敏**：自动过滤私钥内容、Bearer Token、API Token、密码等敏感信息
- **路径脱敏**：错误消息中使用相对路径，避免泄露服务器目录结构

### 10.5 升级安全

- **HTTPS 下载**：升级包必须通过 HTTPS 下载
- **完整性校验**：SHA256 校验必过，校验失败拒绝安装
- **通道白名单**：仅允许 `main`/`dev` 通道，防止路径遍历
- **各平台可增加额外验证**（如 Ed25519 签名、Authenticode 签名）

---

## 11. 共享常量

### 续签相关

| 常量             | 值  | 说明                                                            |
| ---------------- | --- | --------------------------------------------------------------- |
| 默认提前续签天数 | 14  | `schedule.renew_before_days` 初始值，后续由 API 返回值覆写      |
| CSR 最大重试次数 | 10  | local 模式 CSR 提交失败的上限，每天一次，超过后停止等待人工处理 |
| 单次续签批量上限 | 100 | 单次续签检查最多处理的证书数量，防止长时间阻塞                  |

### 分散延迟

| 常量       | 值     | 说明                                                  |
| ---------- | ------ | ----------------------------------------------------- |
| 延迟最小值 | 5 秒   | 证书间延迟下限，保证即使证书很多也有基本间隔          |
| 延迟最大值 | 120 秒 | 证书间延迟上限，防止证书少时等待过久                  |
| 延迟总预算 | 600 秒 | 所有证书间延迟总上限，per-cert = clamp(600/N, 5, 120) |

### 文件大小限制

| 常量       | 值    | 说明                                        |
| ---------- | ----- | ------------------------------------------- |
| 私钥最大   | 16 KB | 私钥 PEM 文件大小上限，超过则拒绝           |
| 证书链最大 | 64 KB | 完整证书链（cert + intermediate）总大小上限 |

### API 超时与重试

| 常量          | 值    | 说明                                                |
| ------------- | ----- | --------------------------------------------------- |
| 查询超时      | 30 秒 | GET 请求的超时时间                                  |
| 提交/回调超时 | 60 秒 | POST 请求的超时时间                                 |
| 最大重试次数  | 3     | HTTP 5xx/网络错误时的重试次数，指数退避（1s→2s→4s） |

### 升级通道

| 常量       | 值             | 说明                           |
| ---------- | -------------- | ------------------------------ |
| 通道白名单 | `main` / `dev` | 允许的升级通道值，防止路径遍历 |
