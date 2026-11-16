# 部署与 FAQ

使用 Python 3.11+。锁定依赖的安装元数据已确认 3.10 不满足 FastAPI、LangChain Core 等依赖要求，3.11、3.12 满足；仅本机运行版本有测试执行证据。默认所有业务入口拒绝没有当前可信凭据的请求。显式 `DEMO_MODE=true` 才启用服务端 `AUTH_TOKENS`（Bearer → 合成用户 ID）；演示主体使用合成国内线上场景。没有接入企业身份源，不能把这个映射用作生产 SSO。

| 配置 | 说明 |
|---|---|
| `DATASET_ROOT`、`DATABASE_PATH`、`DICTIONARY_PATH` | 数据包默认 `data/c004-r01/baseline`；SQL 默认读取根目录 `database/ecommerce.db` 和 `database/dictionary.json`，Compose 对应 `/app/database/`。启动前按冻结 manifest 核验数据库、同目录 `schema.sql` 和字典；不一致或出现 WAL/SHM/journal 报 `DATABASE_SNAPSHOT_MISMATCH`。SQLite 以只读方式打开，不在启动时生成或重冻。 |
| `KNOWLEDGE_PATH` | 默认根目录 `knowledge`；Compose 为 `/app/knowledge`，与入库脚本同目录。API 装配及 CLI 入库前核对所选数据包 manifest 的政策文件集合与 SHA-256；不一致报 `KNOWLEDGE_SNAPSHOT_MISMATCH`。 |
| `BUSINESS_BASE_URL`、`BUSINESS_SERVICE_TOKEN` | Agent 访问业务 API 的地址与凭据。Mock 的 `BUSINESS_API_TOKEN` 必须匹配。客户端只持 Demo bearer。 |
| `MODEL_*`、`EMBEDDING_*` | 配置实际 OpenAI 兼容模型，缺失不返回替身答案。模型及 embedding 请求独立限时。 |
| `VECTOR_BACKEND`、`PG_DSN` | 默认 `pgvector`，显式 `memory` 时仅在当前进程建索引。不会静默降级到 memory。 |
| `RUNTIME_DIR` | 必须在业务冻结根目录及部署数据库所在目录之外，否则报 `RUNTIME_MUST_BE_PRIVATE`。存 `runtime.sqlite`、`checkpoint.sqlite` 和 `private-trace.jsonl`，运维应限制 ACL。Mock 工单另库。 |

本机演示命令见 [README](../README.md)。PG 配置和容器脚手架已提供；本轮本机没有 Docker CLI，独占端口 `55435` 未监听，容器启动和 PG 正向验收均为 **NOT_RUN**。

安装 Docker 后，在仓库根目录私下填写 `.env` 的 `POSTGRES_PASSWORD`（URL 安全值）、`BUSINESS_SERVICE_TOKEN` 与模型/embedding 配置。只在准备执行合成演示时填 `DEMO_MODE=true` 与独立 `AUTH_TOKENS`。保留原始 `.env` 密钥。以下命令会挂载已冻结的 baseline，只读，使用本项目独立的 `55435` 端口。

```powershell
docker compose --env-file .env -f docker/docker-compose.yml up -d vector business
docker compose --env-file .env -f docker/docker-compose.yml build api
docker compose --env-file .env -f docker/docker-compose.yml run --rm api python -m knowledge.ingest
docker compose --env-file .env -f docker/docker-compose.yml up -d api
Invoke-RestMethod 'http://127.0.0.1:8080/health?mode=ready'
```

API 绑定本机 `8080`；Mock `8090`；PG 对本机 `55435`。Compose 使用单 worker，关闭时先停止接收操作，再等现有发送或数据库恢复确实结束。保持三个不同的存储边界：只读冻结资料、Agent runtime/checkpoint、Mock 工单和向量库。不要复制单个正在写的 SQLite 文件作为一致备份；runtime 与 checkpoint 无跨库原子性。

| 接口 | 用法与语义 |
|---|---|
| `GET /health?mode=live` | 只证明服务进程可应答。 |
| `GET /health?mode=ready` | 检查 Controller、入口可接受的 Demo 身份、冻结路径及选定索引代。Mock 探针最多两秒，核对数据版本，并以服务凭据和无订单的探针 actor 读 `/orders?limit=1`。模型配置检查不触发付费推理，`chat_provider_probe: not_probed` 不代表模型服务实时可用。 |
| `GET /metrics` | 仅固定结果分类计数。无 actor、订单号、会话号等标签。 |
| `POST /chat` | `Authorization: Bearer …`；JSON 必填 `message` 与 `client_message_id`。继续旧会话用 `session_id`；恢复时同时提供 `interrupt_id`、`expected_revision`。客户端不能提交 `user_id`、内部 `Command` 或自行授权的字段。 |

初始请求和恢复请求使用不同的 `client_message_id`。精确重放要带同一会话、相同消息 ID 和原内容。会话归属失败在 SSE headers 前统一返 404；旧确认或冲突返 409；如果在 SSE 开始后发生竞态，终止请求可经 `error` 与 `done` 返回。确认语句可以使用单独的“确认”，取消使用“取消”。混合查单与确认的新要求需要重新澄清，不能用 `decision` 绕过。

`stage`、`tool_start/tool_end/tool_error`、`interrupt`、`answer_delta`、`error` 与 `done` 都是流内事件。每帧的 `event:` 与 JSON `type` 相同；JSON 顶层为 `id, request_id, conversation_id, timestamp, type, data`。`id` 每帧唯一，`request_id` 同一 HTTP 流内固定；首轮会话标识在收到终帧前为 `null`，恢复流自首帧带已有标识。私有 Trace 的 `correlation_id` 对应公开 `request_id`，`runtime_request_id` 是独立的运行时账本标识。

Demo 流响应标头为 `X-Agent-Demo-Mode: true`。当前的 `answer_delta` 可携带一段完整回答。连接正常时每个流最多尝试一次 `done`；连接中断后没有交付保证。终帧的 `data` 包含 `task_status`（兼容别名 `status`）、`request_status`、`reason`、`operation_refs`、`persisted`。异常收尾如未取得任务状态则返回 `task_status: null`；`persisted: false` 不证明有已持久化状态。

如果写后连接超时或回执无法核实，查账本的 `UNKNOWN` 与固定键占位，并按业务流程人工核对。Mock 不提供远端幂等或反查，系统不会自动重发，也不承诺业务 Exactly-Once。同一订单可以继续只读咨询。首次没有会话 ID 的请求不能按客户端消息 ID 全局去重；写动作仍需后续同会话实时确认。
