# sslbt 完成前检查

本文件是本仓 finish-check 的唯一检查清单。工具入口只转发参数，不复制步骤。先确定工作区 diff 或 `<base>...HEAD` 范围；逐提交确认单一主题、`type: 中文主题` 和无 AI 署名。

## 自动门禁

1. `python3 -m pytest tests/ -v -W error`：全部通过；测试钩子令任何 skip 失败，警告按错误处理。发布脚本测试同时解包核对必要文件、`info.json.versions` 和运行时 `--version`。
2. `python3 -m flake8 src/ --max-line-length=120 --exclude=__pycache__`。
3. `python3 -m flake8 tests/ --max-line-length=120 --exclude=__pycache__`。
4. `python3 scripts/check-agent-config.py`：Codex 原生薄 Skill、固定 `CLAUDE.md`、Claude 薄命令、扁平领域 Skill 路由、旧路径引用及 Make/CI 接线全部通过。
5. `bash build/release.sh --dry-run 0.0.0-finish-check`：确定性 ZIP、单资产 manifest、候选索引和 SHA256 通过且无网络操作。
6. `git diff --check`。

`make finish-check` 汇总以上全部自动门禁；下列按风险审查仍需明确记录。

## 代码与契约审查

8. 标准库依赖：扫描 `src/**/*.py` import，只允许 Python 标准库及宝塔 `panelSite`/`public`；同步任何 mock 调用变化。
9. 敏感信息：diff 不含真实 token、密码、私钥、域名/IP；日志新增字段经过 `Logger` 脱敏，回调失败信息先脱敏再截断至 256。
10. 文件安全：涉及写入时保留 `flock`、0600、tmp + `os.replace`、符号链接拒绝和路径组件校验。
11. 宝塔契约：SetSSL 参数/返回白名单、参数化 SQLite、站点查询失败与零站点区分、两轮解绑确认、前后端方法与 `{status,msg,data}` 同步。
12. 续签契约：服务端 `renew_before_days`、UTC、重试上限、Local CSR 异步状态、pending 私钥生命周期、订单变更、failure 回调和部署失败顶层状态符合 `deploy-spec.md`。
13. 发布与治理（若相关）：正式资产仅 `sslbt.zip`；SHA256 与文件名一致；main 不可覆盖/重建，tag 后只能原 bundle 恢复；所有节点先 stage 后 publish；Skill/工具/README 不复制冲突流程。
14. diff 与文档：无调试残留、误删、被忽略文件或无关改动；`src/info.json` 不手改。只有本次触碰 `deploy-spec.md` 时才检查 `ssl-manager`、`sslctl`、`sslctlw`、`sslbt` 四仓字节一致，本任务未修改则标记不适用。

## 平台验证与风险报告

影响宝塔交互、安装、部署、续签或正式发布时，在可用环境运行 `make docker-test`，覆盖 nginx/apache。镜像或真机不可用时必须列为未验证，不能用主机单测代替。

最终按安全、兼容性、运行时、测试覆盖不足四类列出风险；无则写“无”。Python CI 两端为 3.9/3.12，但真实宝塔 pyenv、真实面板版本、SSH 多节点事务和 GitHub Release 只能在对应环境验证。
