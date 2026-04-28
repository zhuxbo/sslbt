# 远程发布 sslbt（宝塔面板插件）

输入版本号: $ARGUMENTS

## 版本号处理

- **不要添加 `v` 前缀**，`build/release.sh` 内部自动处理；用户输入带 `v` 时先去除
- 格式：`X.Y.Z`（正式版）或 `X.Y.Z-beta` / `X.Y.Z-alpha` / `X.Y.Z-rc.1`（预发布版）
- 允许重复发布同一版本（服务器上覆盖 zip，`releases.json` 幂等更新）

## 通道判定

`build/release.sh` 内 `get_channel()`：

- 含 `-`（任意后缀） → **`dev` 通道**，可在任意分支发布
- 不含 `-` → **`main` 通道**，本流程强制 main 分支 + origin 同步

## 关键事实

- 单一产物：`dist/sslbt.zip`（宝塔面板纯 Python 插件）
- 实际构建脚本：`scripts/build.sh`（不是 `build/build.sh`）；也可通过 `make release VERSION=<版本号>` 走 Makefile
- **无 Ed25519 签名**——只有 SHA256 校验（写入 `releases.json` 的 `checksums` 字段）
- `build/release.sh` 中的 `check_tag` 仅警告，不强制：tag 不存在或不指向 HEAD 都只打印 WARN，由本指令手工保证
- 多服务器 SSH 上传，`KEEP_VERSIONS=5` 自动清理旧版本

## 执行步骤

### 1. 验证版本号与凭据

- 去除 `v` 前缀（如有）
- 校验格式：`^[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.]+)?$`
- 未提供则中止
- 检查 `build/release.conf` 存在且权限 600；检查 SSH 私钥（配置里的 `SSH_KEY`）可读

### 2. 预飞：测试 SSH 连通性

```bash
bash build/release.sh --test
```

任一服务器失败则中止。

### 3. 预发布版（dev 通道）

任意分支均可，无需 tag、无需合并 main。

```bash
bash build/release.sh <版本号>
# 等价：make release VERSION=<版本号>
```

完成后留在当前分支，**不创建** v tag、**不移动** latest tag。

### 4. 正式版（main 通道）

#### 4.1 强制前置校验

按顺序中止任意失败项：

1. 当前分支 = `main`（若在 dev 走 4.2 合并流程；其他分支退出）
2. `git status --porcelain` 输出为空
3. `git fetch origin`
4. 本地 `main` HEAD == `origin/main` HEAD

#### 4.2 合并 dev → main（仅当当前在 dev 且 dev 领先 main 时）

```bash
git push origin dev

gh pr create --base main --head dev \
  --title "Release v<版本号>" \
  --body "<参考 git log <上次 tag>..origin/dev 总结>"

gh pr merge <PR#> --merge \
  --subject "Merge pull request #<PR#> from zhuxbo/dev" \
  --body "Release v<版本号>"

git checkout main
git pull --ff-only
```

PR body 用：

```bash
git log $(git describe --tags --abbrev=0 origin/main)..origin/dev --oneline
```

按 feat / fix / docs / refactor / test / ci 归类成 Summary + Commits 两段。

#### 4.3 打 tag 并推送

```bash
git tag -a v<版本号> -m "Release v<版本号>"
git push origin v<版本号>

git tag -f latest v<版本号>
git push -f origin latest
```

⚠️ `latest` 是唯一允许 force-push 的 tag，其他 `vX.Y.Z` 一旦推送禁止 force-push。

#### 4.4 执行远程发布

```bash
bash build/release.sh <版本号>
```

脚本顺序：

1. 加载 `release.conf`（SERVERS / SSH_USER / SSH_KEY）
2. `check_tag v<版本号>`（仅警告，本指令已确保 tag 在 HEAD）
3. SSH 连通性测试
4. 跑 `scripts/build.sh <版本号>` 生成 `dist/sslbt.zip`
5. 计算 SHA256
6. rsync 上传 zip 到每台服务器的 `<release_dir>/main/v<版本号>/sslbt.zip`
7. 上传 `install.sh`（如脚本侧实现）
8. 远端 Python 内联更新 `releases.json`：`main.latest = <版本号>`，`versions[]` 头部插入新版（保留 KEEP_VERSIONS）
9. 远端清理超额旧版本

任意服务器失败 → 退出码非零，用 `--server <名称> <版本号>` 重试单台。

#### 4.5 同步 main 回 dev + 验证

```bash
git checkout dev
git merge --ff-only main
git push origin dev
```

`--ff-only` 失败说明 dev 在合并后又有新提交，改用 `git merge main` 解冲突。

线上验证（任一服务器 host）：

```bash
curl -s https://<release_host>/sslbt/releases.json \
  | python3 -c "import sys, json; d=json.load(sys.stdin); print('main.latest:', d['main']['latest']); print('versions:', [v['version'] for v in d['main']['versions']])"
```

`main.latest` 应等于刚发布版本号。

## 使用示例

```
/remote-release 1.0.0-beta    # 预发布版（dev 通道，任意分支）
/remote-release v1.0.0-beta   # 自动去除 v 前缀
/remote-release 1.0.0         # 正式版（强制 main + 干净工作区 + 同步 origin，自动打 v tag + 移动 latest tag）
```

## 高级用法

```bash
bash build/release.sh --test                  # 仅测试 SSH 连通性
bash build/release.sh --server cn 1.0.0       # 只发到 cn（用于单台失败重试）
bash build/release.sh --upload-only 1.0.0     # 跳过构建直接上传 dist/sslbt.zip
make release VERSION=1.0.0                    # 等价于 build/release.sh 1.0.0
```

## 注意事项

- **不要在脏工作区发布正式版**：未提交的改动不会进 zip，但版本号会上线
- **构建失败立即停止**：`scripts/build.sh` 返回非零时 `release.sh` 不会上传
- **不要手动删除/重写已发布的 `vX.Y.Z` tag**：客户端按 tag 拉历史版本会破坏
- **`latest` tag 例外**：仅本指令通过 force-push 移动
- **宝塔安装入口**：用户通过 `bt 9` → 第三方插件管理拉 `install.sh`，`install.sh` 读 `releases.json` 的 `latest`，因此 4.4 完成即对外可见
