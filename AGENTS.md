# sslbt 项目智能体规则

## 项目定位

sslbt 是仅使用 Python 标准库和宝塔运行时的证书部署插件。`src/` 是插件 ZIP 的完整内容，需兼容 Python 3.9、Python 3.12 和宝塔面板 7.0+。

## 不可违反的规则

- 跨仓部署、升级和发布语义以 `deploy-spec.md` 为准；不得在本仓静默增加豁免。
- 不得引入运行时第三方 Python 依赖；`panelSite`、`public` 仅由宝塔运行时提供。
- 涉及配置、证书、私钥或状态文件时，保持现有文件锁、原子写、权限和符号链接防护。
- `main`、`dev` 不自动提交或推送；只有用户明确授权时才执行对应 Git 操作。不得自动执行真实发布。
- 匹配开发、检查、构建或发布任务时，先读取 `skills/SKILL.md`，再按路由读取对应叶子资源。

## 权威入口

- 跨仓统一行为：`deploy-spec.md`
- Skill 路由：`skills/SKILL.md`
- 项目开发：`skills/sslbt-dev.md`
- 完成检查：`skills/finish-check.md`
- 构建与资产：`skills/build-release.md`
- 远程发布：`skills/remote-release.md`

## 核心命令与平台边界

- `make test`：单元测试。
- `make lint`：`src/` 和 `tests/` 的 flake8（行宽 120）。
- `make build VERSION=x.y.z`：构建 `dist/sslbt.zip`。
- `make check-agent-config`：检查智能体配置、Skill 和薄工具入口防漂移。
- `make finish-check`：执行可自动化的完整本地门禁；真实宝塔 Docker 集成仍需可用镜像和容器环境。

## 更新原则

- 只记录长期有效、项目级、会影响智能体行为的规则；不写临时决策、调试记录或单一模块实现细节。
- 新增内容前先判断职责：跨仓公共行为写入 `deploy-spec.md`，领域知识和工作流写入对应叶子资源，本文只保留入口和不可违反的项目约束，不复制正文。
- 只直接维护 `AGENTS.md`；`CLAUDE.md` 始终保持固定薄入口，不追加项目规则。
- 新增、删除或重命名 skill 时，同步更新 `skills/SKILL.md` 和受影响的引用入口。
- 修改后删除失效或重复内容，并检查 `CLAUDE.md` 固定模板、skill 路由、引用路径和确定性防漂移门禁；未经明确需求不得新增全局约束。
