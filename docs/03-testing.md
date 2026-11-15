# 测试策略

## 1. 原则

- 协议测试以官方 encoder/真实帧为准，不用自制错误报文证明自己；
- 跨包测试必须从一端序列化，再交给另一端的真实模型/解析器；
- E2E 不使用 mock broker 或 mock 数据库；
- 安全修复必须有可回归的构建/审计结果。

## 2. 测试分层

### 边缘单元测试

```bash
PYTHONPATH=edge/src .venv/bin/python -m pytest -q edge/tests
```

覆盖：

- Basic ID、Location、Self-ID、System、Operator ID；
- Message Pack 头部/长度/消息数量；
- 官方完整 Wi-Fi Beacon、NAN、BLE service data；
- `WifiReceiver` 和 `BLEReceiver` 的字节提取；
- 去重、ULID、退避、观测模型；
- SQLite outbox、QoS 确认等待、超时回填和容量淘汰。
- 接收器失败后的监督重启、停止事件和帧不丢失。

### 服务端单元/集成测试

```bash
PYTHONPATH=server/src .venv/bin/python -m pytest -q server/tests
```

使用内存 SQLite 和 FastAPI ASGI transport，覆盖：

- 入库、幂等、drone 聚合、站点心跳；
- 乱序观测不倒退 last_seen、重复消息不重复计数/下发；
- REST 查询、时间窗口、bbox、未识别过滤、游标分页；
- 具名 API key、viewer/operator/admin RBAC、未知/无效 key、key 文件热加载、
  格式错误时保留上一份有效凭据、WebSocket subprotocol 鉴权；
- 已认证操作者覆盖客户端 actor、告警事件中的 subject/request ID、
  跨资源安全审计、角色拒绝、审计过滤和游标分页；
- 可选旧版 admin Bearer token、请求 ID、安全响应头；
- WebSocket 心跳、fan-out、慢消费者丢弃策略；
- 两个 Redis 总线实例的跨副本 fan-out、发布失败后的本地降级与重连、
  非法/未知远端 frame 隔离；
- Redis 必需时的 readiness、总线指标、应用 lifespan 启动/停止；
- MQTT shared subscription topic、共享前缀解析和空实例 ID 的唯一 client id；
- 数据库级告警去重窗口、重复候选不重复计数/广播，以及 PostgreSQL
  advisory lock 路径；
- 禁飞区/限高/未识别告警；
- 稳定 alert ID；
- 告警账本 upsert、重复触发计数、乱序触发不倒退时间；
- 告警 open/acknowledged/resolved 生命周期、解决幂等和非法状态转换；
- 告警确认、解决、重开、未知告警 404 和状态变更 WebSocket 广播；
- 已解决告警再次触发时自动重开、重开计数和系统审计事件；
- 告警状态/规则/无人机/时间窗口筛选、事件时间线；
- 告警专用游标翻页、相同触发时间的 `alert_id` 决胜、过滤条件组合、
  跨类型游标拒绝和向后兼容的 `X-Next-Cursor`；
- 告警状态/严重度/规则统计、最早未关闭时间及 `/alerts/stats` 路由优先级；
- 保留任务只删除过期已解决且没有活跃通知的告警、只删除终态通知、
  批次计数和 worker 指标；
- 告警落库失败时仍下发实时告警；
- 通知 outbox 与告警状态原子写入、幂等生命周期事件、通知写入失败隔离；
- Webhook HMAC 签名、2xx/4xx/5xx/超时、退避重试、最大尝试和死信；
- 投递租约恢复、活跃租约防重复领取、outbox 指标；
- health/readiness/metrics；
- MQTT TLS 上下文与证书配对校验；
- 操作员位置、精度、RSSI 在 DB 和 HTTP 之间的保真。

### 跨包契约测试

```bash
PYTHONPATH=edge/src:server/src .venv/bin/python -m pytest -q tests
```

- `test_wire_contract.py` 从 edge 模型生成 MQTT JSON，再由 server 模型解析；
- `test_official_chain.py` 从官方 802.11 Beacon 字节开始，经过真实 receiver、
  pipeline、MQTT 序列化和 server 模型，锁定字段位置与传输类型。

### E2E

```bash
make e2e
```

前置是 `make up`。脚本使用真实 EMQX、TimescaleDB、MqttSubscriber、repository
和 FastAPI 路由，验证官方帧最终能通过 HTTP 查询，并确认未识别告警已落库、
首次确认审计字段可读且重复确认不会覆盖原记录。脚本继续执行解决、重新打开
和再次解决，并验证完整审计事件序列可从真实 PostgreSQL 读回。它还会启动
一个本地签名 Webhook 接收端，验证 `alert.triggered`、`acknowledged`、
`resolved`、`reopened`、最终 `resolved` 的 HMAC、事件顺序和唯一
`delivery_id`，并检查告警分页响应和最终状态统计。

Redis 总线测试默认尝试 `redis://127.0.0.1:6379/15`，也可显式设置
`RID_TEST_REDIS_URL` 指向隔离实例。测试会为每次运行生成唯一 channel，清理
自己创建的 Pub/Sub 订阅；不会删除 Redis 数据。

多副本 E2E：

```bash
make e2e-multi
```

脚本启动两个真实 uvicorn 副本并验证 MQTT shared subscription 的总消费数、
两个 WebSocket 客户端都收到 Redis fan-out、唯一告警和唯一触发审计事件；
结束后删除本次创建的 station/observation/drone/alert 行。

### 前端

```bash
cd frontend
npm ci
npm audit --omit=dev --audit-level=high
npm run typecheck
npm run build
```

构建把 MapLibre 拆成独立 vendor chunk。生产依赖审计必须为 0 个 high/critical。
静态 mock 构建还必须覆盖告警确认、分页和统计接口，避免 Pages 演示出现
只读或缺失历史行为的回归。

### Docker 冷启动

CI 会构建服务端镜像并校验 Compose 配置。发布前人工验证应使用独立 Compose
project、独立端口和独立数据卷，不要对现有生产卷执行 `down -v`。预期结果：

1. 空库启动时自动执行全部 Alembic 迁移（当前到 `0009`）；
2. `/health/ready` 返回 200；
3. `/metrics` 显示 MQTT 已连接；
4. 测试容器和测试卷可以独立删除。

冷启动还会验证迁移重试：PostgreSQL 容器变为 healthy 后仍可能短暂返回
“database system is starting up”，入口脚本应自行重试并在同一个容器进程内
完成迁移，而不是依赖 restart policy 碰运气。

升级发布还需验证上一版的 schema 升级：已有告警、审计事件和确认状态必须
保留，`notification_outbox` 应新增且为待投递状态，`audit_events` 与
`alert_events` 的新增身份列/索引必须存在；旧进程重启后应继续处理到期通知。

## 3. 官方 golden vectors

`edge/tests/fixtures/odid_vectors.py` 来自：

https://github.com/opendroneid/opendroneid-core-c

包含：

- 五类 25 字节 ODID 子消息；
- 标准 Message Pack；
- 完整 Wi-Fi Beacon 帧；
- 完整 Wi-Fi NAN action frame；
- BLE service-data 载荷。

这些 fixture 用于证明实现接受真实 air format，而不是接受项目自己生成的格式。

## 4. CI

`.github/workflows/ci.yml` 包含：

- Python 3.11 + Redis 7 service：安装、`pip check`、Ruff、edge/server/contract
  测试（跨副本总线测试不会跳过）；
- Node 22：`npm ci`、生产依赖安全审计、TypeScript、生产构建；
- Node 22：真实构建和 mock/Pages 构建；
- Docker：Compose 配置校验和服务端镜像构建。

推送 `v*` tag 时，`.github/workflows/release.yml` 会再次执行测试和前端构建，
成功后自动创建 GitHub Release。

## 5. 尚未覆盖

- 真实无线电硬件上的长期稳定性；
- 多机高并发/容量压测；
- Redis 主从切换、网络分区、长时间断线和大规模多副本故障演练；
- 浏览器端 E2E/视觉回归；
- OIDC/MFA 与生产 TLS、MQTT ACL 的渗透测试。
