---
name: sslbt-workflows
description: Route sslbt development, finish-check, build, and release tasks to the repository's authoritative leaf resources.
---

# sslbt 工作流路由

本文件只负责路由。只读取任务匹配的叶子资源；工作流读完适用步骤，领域参考按章节读取：

- 修改插件后端、前端、配置、部署或续签逻辑：`skills/sslbt-dev.md`，先读执行边界，再按模块选择章节
- 完成前检查、CI 模拟、用户要求 `finish-check` 或优化检查规则：`skills/finish-check.md`
- 构建版本产物、签名、manifest、bundle 或验证资产：`skills/build-release.md`
- 发布 dev/main、恢复中断发布或最终验收：`skills/remote-release.md`

真实发布同时读取 `skills/build-release.md`；发布统一语义以 `deploy-spec.md` 第 8 节为准。普通修改不加载发布流程；工具薄入口不拥有业务规则。
