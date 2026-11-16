# 自建 Mock HTTP 契约 v1

版本 `after-sales-mock-v1`。2026-11-15 用户已授权实施；这是候选人自建服务，不是招聘方交付的外部程序。实现后冻结源码摘要，集成测试核对摘要；修改缺陷须新版本/新基线并重验，不静默放宽行为。

## 信任和数据

- Agent→Mock 使用独立服务凭据 `Authorization: Bearer <BUSINESS_API_TOKEN>`，固定可信租户 `demo`；`X-Actor-ID` 由已认证的服务端 Principal 转发。终端用户不能直接指定该头来改变 Agent 身份。缺/错服务凭据 401；无 actor 400。
- 读取从 C-004-v1 数据目录的原三表只读 SQLite 获取。启动校验 manifest 和数据文件 hash；不生成自己的随机订单。冻结 dataset_id 使用已有 snapshot 模块的算法，禁止重新定义另一个同名 ID。
- 成功响应顶层保留业务字段，附 `_meta={api_version,dataset_id,data_contract_version,snapshot_as_of,observed_at}`。金额整数分；时间/状态/NULL 遵循数据契约。observed_at 仅表示观测时间。
- Agent 对当前对象归属、版本和重叠字段做独立比对。Mock 的 `user_id` 不能覆盖 Principal；同版本不一致为 RESPONSE_MISMATCH，不能用于建单。
- 工单写到独立 Mock 私有 SQLite。Agent 不读该库；仅隔离测试可检查实际接受次数。服务没有工单查询接口，也没有远端幂等能力；Idempotency-Key 不产生幂等保证。

## 路径和字段

| 方法 / 路径 | 输入 | 成功响应 |
|---|---|---|
| GET /health | 无业务参数；liveness 不回传秘密 | `{status,api_version,dataset_id}`；初始化失败不就绪 |
| GET /orders/{order_id} | URL 安全标识符 `[A-Za-z0-9_-]{1,64}` | 200：`order_id,user_id,amount,status,create_time,_meta` |
| GET /logistics/{order_id} | 已授权订单号 | 200：`logistics_id,order_id,status,update_time,_meta`；快照最多一条/订单，不能伪造轨迹 |
| GET /orders | `limit` 1..100（默认 50），可选不透明 `cursor` | 200：`items,next_cursor,_meta`；按 order_id 排序，仅 actor 的订单；cursor 绑定 actor/dataset/offset，非法或跨快照游标 400 |
| POST /tickets | JSON `{order_id,user_id,reason}`，reason 去首尾空格后 1..500 字，user_id 必须等于可信 actor | 201：`ticket_id,order_id,status:"ACCEPTED",_meta`；ticket_id 是远端编号，非 operation_id |

订单不存在与不属于当前 actor 均 404 `order_unavailable`，不泄露差异。已有订单但无物流 404 `logistics_unavailable`。错误体 `{error:{code,accepted:false}}` 仅在契约确认未接受时使用；错误正文不反射凭据或任意输入。400/401/403/404/422 为确定未接受；POST 429、500、网络中断、超时、非法成功响应均按可能接受处理，Agent 不重试。GET 的 429/500/502/503/504 可受限重试。

## 故障脚本与冻结

只从服务启动时读取的独立配置加载故障序列，按 `METHOD /path` 的尝试次序消费；尾部耗尽恢复正常。Agent 请求无故障开关，普通 API 无管理故障入口。

动作支持 `ok`、`429`（Retry-After）、`500`（接受前失败但不向 Agent 提供未接受证明）、`timeout_before`（延迟且不接受）、`timeout_after`（POST 先落工单再延迟）、`500_after`（POST 落库后 500）。延迟秒数可配置，用短时限的真实 HTTP 测试验证。全部保持三表快照不变。随机故障仅可做额外探索，不作为固定验收。

冻结清单记录：契约文件 SHA256、服务与所依赖项目模块 SHA256、API 版本、数据 manifest SHA256/dataset_id、固定故障配置 SHA256、启动命令。正式测试使用 uvicorn 的真实 loopback HTTP；httpx transport/ASGI 测试单独标记组件测试。新增业务代码无需修改冻结服务；冻结依赖发生合法变更须明确重建并重新跑正式 HTTP 基线。

## 对应验收

C03/C04：对象、分页、金额/状态/NULL 与 SQL 一致，无越权或遗漏；C05/C06：版本/hash/字段不一致拒绝；C07/C08：实际 HTTP 次数、远端已落库但客户端 UNKNOWN、跨会话禁止第二次发送；C09：服务不热换业务快照。C01/C02 复用 C-004 数据报告，不冒充 HTTP 测试。
