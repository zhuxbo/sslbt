# sslbt 远程发布

本文件是本仓发布流程唯一权威实现。始终先读取 `deploy-spec.md` 第 8 节和 `skills/build-release.md`；两者冲突时立即停止，不得自行增加豁免。

## 输入、通道与禁止项

版本参数可带一个前导 `v`，进入流程后去除。必须是完整 SemVer：稳定版进入 `main`，带预发布段的版本进入 `dev`。

发布属于高影响外部操作。除非用户明确要求真实发布，否则只允许 `--dry-run`、静态检查、临时目录或 mock；不得连接发布节点、写 GitHub、创建或移动 tag、合并、推送。

## 本仓 required checks 与 release gates

适用于 PR head、合并后精确 `main` commit、回同步后精确 `dev` commit 的 GitHub required checks：

- `test (3.9)`、`test (3.12)`
- `lint`
- `governance`
- `build`

每个阶段必须核对 checks 的 head SHA 等于目标 commit，不能用其他提交的绿灯替代。

正式版额外 release gates：

- 在精确 source commit 运行 `skills/finish-check.md` 全部适用项。
- 在可用的真实宝塔测试镜像中运行 `make docker-test`，覆盖 nginx/apache 的 setup、deploy、renew；若环境不可用，正式发布必须停止，不能把单元测试或 Docker 构建当成替代。
- `bash build/release.sh --dry-run <预发布测试版本>` 通过，证明确定性构建、manifest 和索引逻辑。

## dev 测试版

确认版本含预发布段后执行：

```bash
bash build/release.sh --dev <version>
```

该命令只构建当前快照并发布到全部配置节点。允许脏工作区和同版本覆盖；manifest/index 必须记录实际 `source_commit`、`dirty`。不得 fetch、提交、推送、切换/合并分支、操作 tag 或 GitHub Release。完成声明要求脚本全节点验收成功，并从每个发布节点的公网域名读取 `releases.json.dev.latest`、下载 `sslbt.zip` 再核对 SHA256。

兼容历史命令 `bash build/release.sh <version>`，但只有带预发布段的 SemVer 会路由到上述 dev 流程；稳定版直接传入仍必须拒绝，不得绕过 main 正式发布门禁。

## main 正式版

严格按 `deploy-spec.md` 8.6 的顺序推进，以下是 sslbt 的平台化落点：

1. 校验稳定 SemVer 高于公网 `main.latest`；确认工作区干净且本地 `dev == origin/dev`。
2. 建立 `dev → main` PR，等待上述 required checks 精确对应 PR head，并在同 commit 完成全部 release gates 后才合并。
3. 同步本地 `main`，确认本地/远端 `main`、工作区和合并 commit；等待该精确 commit 的 required checks，再重跑适用的本地 release gates。
4. 在持久位置只构建一次：

   ```bash
   bash build/release.sh --prepare <version> --bundle <durable-path>/v<version>
   bash build/release.sh --stage <durable-path>/v<version>
   ```

5. 核对 manifest 的 commit、唯一资产 `sslbt.zip` 和 SHA256；确认所有节点已 stage 且尚未更新公开索引。
6. 创建并推送不可变 tag `v{version}`，必须指向 manifest commit。创建指向同一 tag/commit 的 draft GitHub Release，只从该 bundle 上传 `assets/sslbt.zip`；验收 draft 的资产名、数量、大小和 SHA256。
7. 执行 `--publish` 公开全部服务器节点。确认全节点成功后公开 GitHub Release（非 draft、非 prerelease、标记 latest），再从每个发布节点的公网域名下载 ZIP 验证。
8. 仅在服务器、GitHub 和公网资产全部对账后，把唯一可移动 tag `latest` 更新到 `v{version}`。
9. 将 `main` 以 fast-forward 同步回 `dev` 并推送；若 `dev` 已前进立即停止，不得 merge/force-push 绕过。等待精确 dev commit 的 required checks。
10. 执行 `deploy-spec.md` 8.9 全部验收。完成前保留 bundle；完成后也建议按发布留档策略保存 manifest。

不得使用 `make release` 或旧的单节点重试路径绕过以上顺序。正式资产只从持久 bundle 上传一次，不为服务器和 GitHub 分别构建。

## 中断恢复

- tag 创建前：保留未公开 staging 和封存候选后，可修复连接问题并对同一 bundle 重复执行 `--stage` 幂等续传；如需重建，先明确废弃整个未打 tag bundle。
- tag 创建后：只可使用原路径 `bash build/release.sh --resume <bundle>`；先校验 tag、manifest、ZIP 和原候选索引，不得运行构建、移动/删除 tag 或覆盖正式资产。
- GitHub draft、服务器索引、Release 公开、`latest`、分支回同步任一步失败：保留既有不可变对象和 bundle，从该步骤继续，最终重新做全部节点/GitHub/公网对账。
- 任一节点异常时不得宣布成功，也不得以 `--server` 只修一个节点后跳过全量验收。

## 完成报告

报告版本、source commit、bundle 路径、manifest SHA256、规范资产及 SHA256、各阶段 checks 的精确 SHA、所有节点/GitHub/公网验收、tag/分支引用状态和恢复证据。未验证项必须明确列出，不能以“脚本成功”代替真实发布验收。
