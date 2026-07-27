# sslbt 构建、资产与发布原语

本资源只定义本仓平台构建、完整性和服务器事务；完整发布编排以 `skills/remote-release.md` 为唯一权威实现，统一语义以 `deploy-spec.md` 第 8 节为准。

## 平台资产与签名

- 规范正式资产集合：仅 `sslbt.zip`，公开文件名固定，不含版本号；版本由目录 `v{version}/` 和 ZIP 内 `info.json.versions` 表达。
- GitHub-only 附加资产：无。
- 服务器级可变引导文件：`deploy/install.sh`，只随 main 正式事务同步到每个节点根目录，不属于版本资产和 `checksums`；dev 不得改写稳定安装入口。
- 平台签名：无 Ed25519、Authenticode 或代码签名。完整性机制是 manifest 和 `releases.json` 中的 SHA256；客户端缺少或无法匹配 SHA256 时必须拒绝在线更新。
- 运行时版本：`python sslbt_main.py --version` 输出 ZIP 内注入版本；宝塔 UI 读取同一 `info.json`。

## 确定性构建

```bash
make build VERSION=1.2.3
bash scripts/build.sh 1.2.3 /tmp/sslbt.zip
```

`scripts/build.sh` 对文件排序、规范化 ZIP 时间戳和权限，并只在内存中注入版本号，不修改 `src/info.json`。相同 source tree、版本和 `SOURCE_DATE_EPOCH` 必须生成字节一致的 ZIP。

## 持久 bundle

```text
<bundle>/
├── manifest.json
├── assets/sslbt.zip
├── bootstrap/install.sh
├── releases-baseline.json       # stage 时生成
├── release-candidate.json       # stage 时生成，恢复时不可重建
├── transaction.json             # 封存 manifest、基线、候选、ZIP、installer 哈希
└── index-baseline/*.json        # 各节点原始索引证据
```

`manifest.json` 绑定版本、通道、source commit、dirty、创建时间、唯一规范资产、大小和 SHA256。正式 bundle 默认位于 `.release-bundles/main/v{version}/`；发布窗口结束前不得删除。推荐正式发布时用 `--bundle` 指向发布机持久盘。

## 脚本原语

- `bash build/release.sh --prepare VERSION [--bundle DIR]`：只构建一次并生成 manifest；稳定版要求当前 `main`、工作区干净且本地 `main == origin/main`，并拒绝已存在的版本 tag。
- `bash build/release.sh --stage BUNDLE`：不构建；要求各节点索引语义一致，生成并封存一次候选索引，将同一 ZIP、manifest、候选索引（main 另含 installer）暂存到全部节点并校验 SHA256。中断后可在 tag 前用同一命令幂等续传，main 要求公开版本目录尚不存在。
- `bash build/release.sh --publish BUNDLE`：不构建；main 要求本地和远端 `v{version}` 都精确指向 manifest commit。获取全节点事务锁并以 stage 基线做 CAS 后，先在全部节点准备资产，再原子替换各节点索引；失败时尝试把所有已处理节点恢复到原索引、installer 原始存在状态和版本目录。
- `bash build/release.sh --verify BUNDLE`：逐节点核对公开资产 SHA256、`latest`、版本条目和 checksums。
- `bash build/release.sh --resume BUNDLE`：仅 main；验证原 manifest、资产、候选索引和 tag 后，从暂存/公开状态继续，禁止调用构建。
- `bash build/release.sh --dev VERSION [--bundle DIR]`：dev 快捷路径，允许脏工作区和同版本远端覆盖，不提交、不切分支、不操作 tag/GitHub。
- `bash build/release.sh VERSION [--bundle DIR]`：兼容历史 dev 入口，仅接受带预发布段的 SemVer，行为与 `--dev` 完全相同；稳定版仍拒绝执行。
- `bash build/release.sh --dry-run VERSION`：只在临时目录验证两次构建字节一致、manifest 和候选索引；必须使用预发布 SemVer，不读取发布配置、不联网。

不提供 `--server` 单节点发布：恢复也必须面向全部节点重新验收，避免单节点 `latest` 长期漂移。`make release` 固定拒绝真实发布，防止绕过 `remote-release` 门禁。

## 服务器事务和恢复边界

发布配置 `SERVERS` 的每个节点都是正式一致性范围。stage 前各节点 `releases.json` 必须语义一致；候选索引只生成一次并由 `transaction.json` 绑定。publish 先取得 bundle 本地进程锁，再用带本次运行唯一 owner 的远端锁锁住全部节点；只有当前完整索引仍等于封存基线或候选时才能推进，防止同 bundle 并发、旧 bundle 重放或覆盖另一通道更新。读取失败不能伪装成空索引。任何失败都不得报告成功；保留 bundle、manifest、候选索引、远端 `.staging/`/`.rollback/` 和锁证据，修复节点后对 main 使用 `--resume`。

全部节点完整索引达到封存候选后，事务进入 forward-only：后续清理中断只能继续验收和清理，不能重新创建回滚基线。版本 tag 创建前失败可复用原事务重新 stage；tag 创建后不得重新 `--prepare`、不得移动 tag、不得用新构建替换 bundle。已完成正式版只能用更高版本修复。
