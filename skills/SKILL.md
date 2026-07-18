---
name: sslbt-workflows
description: Route sslbt development, finish-check, build, and release tasks to the repository's authoritative leaf resources.
---

# sslbt 工作流路由

本文件只负责路由。选择匹配任务后，完整读取对应叶子资源并遵循其中流程：

- 修改插件后端、前端、配置、部署或续签逻辑：`skills/sslbt-dev.md`
- 完成前检查、CI 模拟或用户要求 `finish-check`：`skills/finish-check.md`
- 构建版本产物、签名、manifest、bundle 或验证资产：`skills/build-release.md`
- 发布 dev/main、恢复中断发布或最终验收：`skills/remote-release.md`

发布任务同时读取 `build-release.md`；发布统一语义以 `deploy-spec.md` 第 8 节为准。工具薄入口不拥有业务规则。
