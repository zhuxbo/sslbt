# 完成前检查清单 — 逐项执行，每项完成后标记结果

---

## 0. 检查范围

先确定本次检查的 diff 范围，后续所有"审查改动"的步骤都以此范围为准：

- **工作区模式**（默认）：改动尚未提交，范围是 `git diff` + `git diff --cached`。
- **工作分支模式**：改动已按批次提交到特性分支，以基线分支（通常 `dev`）为对照：

```bash
git log --oneline <base>..HEAD    # 逐提交清单
git diff <base>...HEAD            # 全量改动
```

分支模式还需逐提交检查：每个提交只含单一主题的相关文件；提交信息为 `type: 中文主题` + 2–10 条要点式 body；无任何 AI 署名。

## 1. 单元测试

运行 `python3 -m pytest tests/ -v`，确认全部通过、无跳过、无警告。

如果有测试失败：分析失败原因，修复后重跑，直到全部通过。

## 2. Lint 检查

运行 `python3 -m flake8 src/ --max-line-length=120 --exclude=__pycache__`，确认零告警。

`tests/` 目录同样保持零告警：`python3 -m flake8 tests/ --max-line-length=120 --exclude=__pycache__`。

注意本项目行宽限制是 **120**，不是默认 79。

## 3. 标准库依赖审计

扫描 `src/` 下所有 `.py` 文件的 import 语句：

- 确认**没有引入任何第三方包**（本项目零依赖，仅用 Python 标准库）
- 允许的宝塔运行时模块：`panelSite`、`public`（在测试环境中由 `tests/mock_bt/` 提供）
- 如果发现第三方 import（如 requests、cryptography 等），必须移除并改用标准库实现

## 4. 敏感信息泄露检查

在本次 `git diff` 的变更中搜索以下内容：

- 硬编码的 API token、密码、密钥
- 真实域名或 IP 地址（测试中应使用 `example.com` 等占位域名）
- 未经 `logger.sanitize()` 过滤就写入日志的 token/密钥字段

确认 `src/lib/logger.py` 中 `_FILTERS` 正则能覆盖所有新增的敏感字段格式。

## 5. 文件锁与权限检查

审查涉及文件读写的改动：

- 配置文件写入是否使用了 `fcntl.flock()` 文件锁（读 `LOCK_SH`，写 `LOCK_EX`）
- 配置文件是否保持 `0o600` 权限（`os.chmod(tmp_path, 0o600)`）
- 写入是否经过 tmp + `os.replace()` 原子操作，而非直接覆盖
- 写入前是否检查了符号链接（`os.path.islink()` 拒绝写入）

## 6. 宝塔 API 兼容性

如果修改了与宝塔交互的代码：

- `panelSite.SetSSL()` 调用参数是否与宝塔文档一致（`siteName`, `key`, `csr`, `type`）
- 数据库查询（`sqlite3`）是否使用参数化查询，避免 SQL 注入
- 站点名称匹配是否正确处理了通配符域名（`*.example.com`）
- `site_name` 字段是否统一为列表格式（历史兼容：旧数据可能是字符串）
- 站点清单查询失败（`SiteQueryError`）是否与"确认零站点"严格区分？**绝不能把查询失败当站点不存在**；解绑/清空绑定等破坏性操作不得基于单次失败探测

## 7. 续签逻辑边界检查

如果修改了 `renew.py` 或相关续签流程：

- 续签窗口是否由服务端主导（`renew_before_days`，默认 `RENEW_DEFAULT_DAYS = 14`，每次 API 交互回填），本地不得硬编码提前天数
- 重试计数是否有上限保护（`MAX_ISSUE_RETRY_COUNT = 10`）
- 证书过期判断是否用 UTC 时间，避免时区问题
- 回调语义是否与 deploy-spec 一致（callback status 仅 success/failure，无 pending；message 仅 failure 携带且已脱敏截断至 ≤256）？metadata 写入失败是否绝不回调 success？
- 空/不可解析的 `cert_expires_at` 是否按"未知需处理"进入查询回填，而非静默跳过？
- 对服务端的 HTTP 出口是否统一走 `APIClient`（HTTPS 强制 + SSRF 防线），无裸 urlopen？

## 8. Mock 模块同步

如果修改了 `src/lib/` 中调用 `panelSite` 或 `public` 的方式：

- `tests/mock_bt/panelSite.py` 和 `tests/mock_bt/public.py` 是否同步更新
- `conftest.py` 中的 mock 注入（`sys.modules`）是否覆盖新模块

## 9. 前端一致性

如果修改了 `sslbt_main.py` 中的接口方法：

- `src/index.html` 中对应的 AJAX 调用是否同步更新（方法名、参数、返回字段）
- 新增的后端方法是否在前端有对应的调用入口
- 返回格式是否统一为 `{status: bool, msg: str, data: ...}`

## 10. Git Diff 审查

按第 0 步确定的范围审查（工作区模式用 `git diff` + `git diff --cached`；工作分支模式用 `git diff <base>...HEAD` 并逐提交 `git show`），逐文件审查：

- 是否有调试代码残留（`print()`、`breakpoint()`、`import pdb`）
- 是否有被意外修改的文件（与本次任务无关的改动）
- 是否有意外删除的代码行
- `.gitignore` 中的文件（`dist/`、`build/release.conf`、`*.log`、`.venv/`）是否未被追踪

## 11. info.json 与 install.sh 完整性

确认 `src/info.json` 和 `src/install.sh` 没有被意外修改（版本号由构建脚本注入，不应手动改动）。

如果确实需要修改 `install.sh`，确认：

- `pip install` 的包列表与实际依赖一致
- 脚本中路径使用 `/www/server/panel/plugin/sslbt/`

## 12. 已知局限性与潜在风险

列出本次改动的已知局限性和风险，按以下分类输出：

### 安全风险

- 涉及 token/密钥处理的改动是否有泄露路径
- 文件权限是否可能被降级

### 兼容性风险

- 是否影响 Python 3.9 ~ 3.14 的兼容性（CI 矩阵覆盖 3.9 和 3.12）
- 是否影响宝塔面板 7.0+ 的兼容性
- 配置文件格式变更是否向后兼容（旧配置能否正常读取）

### 运行时风险

- 文件锁死锁场景
- 网络超时和重试行为是否合理
- 证书续签失败时的回退策略是否完善

### 测试覆盖不足

- 本次改动中哪些分支/路径缺少测试
- 是否需要补充边界条件测试

如果某个分类下无风险则注明"无"，但不要省略分类。
