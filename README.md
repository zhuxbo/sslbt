# sslbt 宝塔面板证书部署插件

宝塔面板 SSL 证书自动部署与续签管理插件，通过部署 API 获取证书，一键部署到站点。

## 功能

- 证书管理：添加、部署、删除证书订单
- IP 证书：支持 IP 地址证书（SAN 中的 IP Address 类型）
- 批量添加：批量查询并添加证书，自动匹配宝塔站点
- 自动续签：支持 Pull 和 Local 两种续签模式
- 文件验证：Local 模式支持文件验证和委托验证，根据域名类型自动限制可选方式
- 批量部署：一键部署所有已绑定站点的证书
- 计划任务：添加证书时自动创建，定期检查证书过期并自动续签，多证书分散执行
- 多站点绑定：一张证书可部署到多个站点，支持域名通配符匹配
- URL 解析添加：粘贴部署链接（含 token 和 order 参数）自动解析证书信息并匹配站点

## 安装

### 一键安装（推荐）

```bash
curl -fsSL https://release.cnssl.com/sslbt/install.sh | bash -s -- release.cnssl.com
```

安装指定版本：

```bash
curl -fsSL https://release.cnssl.com/sslbt/install.sh | bash -s -- release.cnssl.com --version 0.0.2
```

安装测试版：

```bash
curl -fsSL https://release.cnssl.com/sslbt/install.sh | bash -s -- release.cnssl.com --dev
```

升级：重新运行安装命令即可，已有配置和证书数据不会被覆盖。升级后刷新页面即可生效，无需重启面板。

### 手动安装

1. 下载插件包：`make build VERSION=x.x.x`
2. 宝塔面板 → 软件商店 → 第三方应用 → 导入插件
3. 上传 `dist/sslbt.zip`

### 安装后

通过「证书管理」页面粘贴部署链接添加证书，API 配置随证书自动保存。

### 插件内更新

设置页面底部可检查更新并一键升级，支持切换稳定版/测试版通道。

## 开发

```bash
# 单元测试
make test

# 构建 ZIP（需指定版本号）
make build VERSION=1.0.0

# 发布到远程服务器
make release VERSION=1.0.0

# 容器集成测试
make docker-test
```

## 续签模式

| 模式 | 说明 |
|------|------|
| pull | 自动签发（默认）：API 签发后拉取证书 |
| local | 本机提交：本地生成 CSR 提交签发 |

Local 模式需选择验证方式：委托验证（默认）或文件验证。IP 证书仅支持文件验证，通配符证书仅支持委托验证。

提前续签天数由服务端下发（默认 14 天，上限 30 天，超限保留本地配置），两种模式共用。

## 目录结构

```
src/                    # 源代码 = ZIP 包内容
  sslbt_main.py         # 插件入口
  index.html            # 前端 UI
  info.json             # 插件元信息
  install.sh            # 安装脚本
  lib/                  # 核心库
tests/                  # pytest 测试
docker/                 # 容器集成测试
scripts/                # 构建脚本
deploy/                 # 安装脚本
build/                  # 发布脚本及配置
```
