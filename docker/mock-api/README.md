# mock-api

docker-test 用的部署 API 假服务端，仅测试资产、不进插件 ZIP。

原先从同级仓 `../sslctl/docker/test/mock-api` 构建，已收编进本仓以消除跨仓引用；
两仓各自维护各自的副本，行为契约共同以 `deploy-spec.md` 为准。

## 契约要点

- 查询无分页（§2.3）：只回 `{data, renew_before_days}`，不输出 `total`/`page`/`page_size`。
- `order` 必填且只接受订单 ID；缺参/空串/域名/混合形态一律 `code=0` + `error_code=invalid_order`。
- 批量未命中静默跳过，全未命中回空数组；单 ID 未命中才 `order_not_found`。
- 错误响应恒 HTTP 200 + `code=0`，分类只经 `errors.error_code`（§2.2）。

## 场景

`POST /admin/scenario/{name}` 切换。除既有的 `active`/`processing`/`renew-flow` 等，
本轮新增：

- `rate_limited`（带 `retry_after`）/ `token_invalid` / `token_disabled` /
  `account_disabled` / `ip_not_allowed`：注入 §2.2 确定性失败，验证客户端停止本轮而非重试。
- `lying-total`：谎报 `total=99999` 且返回满页，验证客户端单次取完、不按自报计数翻页。

## 本地运行测试

Dockerfile 只 COPY `main.go`（镜像不含测试）。本地跑：

```bash
make mock-api-test
```

无 Go 工具链时该目标自动跳过。
