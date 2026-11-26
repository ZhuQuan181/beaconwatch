# BeaconWatch 架构

> 本文描述 **当前仓库已经实现并经过测试** 的能力。规划中的功能会明确标为
> “未实现”，不把设计目标写成现状。

## 1. 系统目标

BeaconWatch 接收无人机广播的 Remote ID（ASTM F3411 / Open Drone ID），在
边缘节点完成协议解析和断网缓存，经 MQTT 汇聚到服务端，并提供：

- 实时地图、轨迹和历史回放；
- 多接收站聚合与覆盖范围展示；
- 禁飞区、限高、未广播操作员 ID 告警；
- 可通过 HTTP/WebSocket 消费的观测与站点数据。

## 2. 当前部署拓扑

```text
┌──────────────────────────── Edge station ────────────────────────────┐
│                                                                      │
│  802.11 monitor-mode adapter        BLE adapter                        │
│            │                           │                              │
│            ▼                           ▼                              │
│   WifiReceiver                    BLEReceiver                         │
│   Beacon / NAN vendor IE          service data 0xFFFA                 │
│            └─────────────┬─────────────┘                              │
│                          ▼                                            │
│              Open Drone ID frame parser                               │
│        Basic ID / Location / Self-ID / System / Operator ID           │
│                          │                                            │
│                          ▼                                            │
│        validation → dedup → per-station rate control                  │
│                          │                                            │
│                          ▼                                            │
│      supervised receivers → SQLite outbox → MQTT publisher            │
│      restart/backoff      QoS wait / timeout / bounded eviction        │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ rid/v1/{station}/obs
                           │ rid/v1/{station}/info
                           ▼
┌────────────────────────── EMQX ──────────────────────────────────────┐
└──────────────────────────┬───────────────────────────────────────────┘
                           │ subscribe, QoS 1 by default
                           ▼
┌──────────────────────────── Server ──────────────────────────────────┐
│                                                                      │
│  API replica 1..N                                                    │
│  MqttSubscriber (shared subscription: one replica per message)        │
│    ├─ Pydantic validation                                            │
│    ├─ station heartbeat upsert                                       │
│    ├─ idempotent observation insert                                  │
│    ├─ alert rules + durable cross-replica dedup                      │
│    └─ RedisObservationBus ────────────────────┐                      │
│                          │                     │                      │
│                          ▼                     ▼                      │
│                    TimescaleDB          Redis Pub/Sub                 │
│                    observations,        │                             │
│                    alerts, drones,      ▼                             │
│                    stations,       each replica's local fan-out       │
│                    geofence_zones  └─> /ws/observations               │
│                          │                                            │
│                          ▼                                            │
│                    FastAPI REST API                                   │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ HTTP / WebSocket
                           ▼
┌──────────────────────────── Frontend ────────────────────────────────┐
│ React + TypeScript + MapLibre                                        │
│ live map / drone list / replay timeline / alerts / station coverage   │
└──────────────────────────────────────────────────────────────────────┘

Redis 是可选的实时共享总线：单副本默认使用进程内 fan-out；多副本同时设置
`RID_BUS_BACKEND=redis` 和同一 `RID_MQTT_SHARED_GROUP`。Redis Pub/Sub 只
承载实时推送，不保存历史；客户端断线后通过 REST 回补。OIDC/JWT、MQTT ACL
和 Redis 生产级 TLS/HA 仍未内置。
```

## 3. 已实现的协议链路

### 3.1 Wi-Fi

- Beacon / probe response 中的 vendor IE：OUI `FA:0B:BC`，OUI type `0x0D`。
- NAN public action frame：Wi-Fi Alliance OUI `50:6F:9A`，读取 NAN service
  descriptor 中的 ODID Message Pack。
- 传输类型分别标记为 `wifi_beacon` 和 `wifi_nan`。

### 3.2 BLE

- 16-bit service UUID `0xFFFA`。
- 接受 bleak 已剥离 UUID 的 service data，也兼容仍带 `FA:FF` 前缀的数据。
- 观测协议标记为 `astm_f3411`。

### 3.3 Open Drone ID

- 单条消息固定 25 字节。
- Message Pack 布局为：
  `type/version(1) + singleMessageSize(1) + count(1) + count × 25 bytes`。
- 已解码 Basic ID、Location、Self-ID、System、Operator ID。
- 官方 C encoder 生成的 golden vectors 位于
  `edge/tests/fixtures/odid_vectors.py`。

## 4. 数据契约

`rid_edge.models.RidObservation` 是边缘端发布格式；服务端
`rid_server.models.DroneObservation` 负责接收和 HTTP 输出。

关键约束：

- `speed_mps` 和 `heading_deg` 位于 `position` 内，不在顶层；
- `operator_location` 只含 `lat` / `lon` / `alt_m`，对应 ODID System 消息；
- `received_at` 由接收节点生成，`drone_timestamp` 来自广播方；
- MQTT topic 中的 `station_id` 优先于 payload 中的同名值；
- 观测幂等键为 `(observation_id, received_at)`，适配 TimescaleDB 分区表。

完整契约由 `tests/test_wire_contract.py` 和 `tests/test_official_chain.py`
锁定。

## 5. 边缘节点可靠性

### MQTT

- 所有 QoS 级别统一经过 outbox 和发布确认路径。
- QoS 1/2 会等待 `wait_for_publish()` 和 `is_published()`；超时则保留/回填
  SQLite 行，避免“已从本地删除但 broker 未收到”。
- outbox 上限按 topic + payload 字节数计算，超限时淘汰最旧的四分之一。
- 站点心跳与观测共用 outbox，网络恢复后不会只恢复观测而丢掉身份信息。

### 本地去重和限流

- 同一 drone 的稳定 `observation_id` 在时间窗口内只发布一次。
- pipeline 提供每站每秒观测数上限，防止异常广播或错误抓包撑爆上行链路。

### 已知边界

- SQLite 是单节点本地队列，不是跨节点事务系统。
- 发射速率上限是软上限；极端突发下仍可能经过队列缓冲。
- Wi-Fi/BLE 接收器由监督任务管理，异常退出后会按指数退避重建；硬件彻底
  失效时仍会持续重试，需要日志/外部告警发现长期不可用。

## 6. 服务端行为

### 入库

1. 解析 MQTT topic 和 JSON；
2. Pydantic 校验 `DroneObservation`；
3. 先 upsert station，满足外键；
4. 确保 drone 占位行存在，满足观察表外键；
5. 插入 observation；只有真正插入的新行才更新 drone 汇总；
6. 重复消息直接结束，不重复计数、告警或推送；
7. 提交后执行告警规则，upsert 告警账本、审计事件和通知 outbox；
8. 推送 WebSocket，并唤醒异步通知 worker。

### 站点心跳

`rid/v1/{station_id}/info` 会更新站点名称、坐标、高度、固件版本和
`last_seen`。topic 中的站点 ID 优先，payload 不能冒充其它站点。

### 告警

- `geofence`：点位落在 polygon（可选高度带）内；
- `altitude`：超过配置的全局限高；
- `unidentified`：收到无 `operator_id` 的观测；
- 同一 `(drone, rule, zone)` 在窗口内去重；
- `alert_id` 使用稳定 SHA-256 摘要，不使用 Python 加盐 `hash()`；
- 告警写入 `alerts` 表，服务重启后仍可查询；
- 告警生命周期为 `open → acknowledged → resolved`，也可以直接从
  `open` 解决；
- 同一 `alert_id` 再次触发时累加 `trigger_count`；若告警已解决，则自动
  进入新的处置周期、清空上一周期活动处置字段并累加 `reopen_count`；
- `POST /alerts/{alert_id}/ack` 记录首次确认的操作员、时间和可选备注；
- 重复确认是幂等的，不覆盖第一次确认的审计字段；
- `POST /alerts/{alert_id}/resolve` 记录解决人、时间和可选解决说明；
- `POST /alerts/{alert_id}/reopen` 将已解决告警重新打开，并开始新的确认
  周期；
- 每次触发、确认、解决和重开都会写入不可变的 `alert_events` 审计表；
- `GET /alerts/{alert_id}/events` 返回完整生命周期时间线；
- 状态变更通过 WebSocket 广播，其他在线操作员无需刷新即可看到；
- `GET /alerts` 在还有更早记录时返回 `X-Next-Cursor`，
  `GET /alerts/page` 返回显式的 `{items, next_cursor}`；游标基于
  `(triggered_at, alert_id)`，不会随历史增长退化为深 offset 扫描；
- `GET /alerts/stats` 按同一组状态/规则/无人机/时间过滤条件返回总数、
  open/acknowledged/resolved/active、严重度与规则分布以及最早未关闭时间；
- 告警落库失败会记录日志并降级为仅实时推送，不阻断观测入库；
- 进程内 ring 只作为最近告警的轻量缓存，不再是数据源。

### 告警保留

- 告警保留和通知 outbox 保留默认关闭，避免升级时静默删除审计数据；
- 设置 `RID_ALERT_RETENTION_DAYS` 后，只清理已解决超过期限、且没有
  pending/delivering Webhook 投递的告警；
- 设置 `RID_NOTIFICATION_RETENTION_DAYS` 后，只清理 delivered/dead 状态的
  通知行，pending/delivering 永不按期限删除；
- 清理按 `RID_RETENTION_BATCH_SIZE` 分批提交，失败只记录指标和日志，
  不影响 API、MQTT 入库或通知 worker。

### 告警通知

- 配置 `RID_NOTIFY_WEBHOOK_URL` 与 `RID_NOTIFY_WEBHOOK_SECRET` 后启用；
- 告警触发、确认、解决和重开会在同一数据库事务内写入
  `notification_outbox`，不会出现“告警已提交但通知意图丢失”；
- outbox 写入失败只回滚通知保存点，告警和审计仍会提交，并记录
  `notification.enqueue.failed`；
- 后台 worker 使用租约领取到期行，按 alert 保持串行、跨 alert 并发投递；
- 同一 alert 只要还有更早的未完成通知，后续事件就不会被领取，避免重试时
  `acknowledged`/`resolved` 越过尚未成功的 `triggered`；
- `2xx` 视为成功；`408`、`425`、`429` 和 `5xx` 重试；其它 `4xx`/`3xx`
  直接进入死信；网络错误和超时重试；
- 重试使用有上限的指数退避和抖动，达到
  `RID_NOTIFY_MAX_ATTEMPTS` 后状态变为 `dead`；
- 投递为至少一次语义，`delivery_id` 在重试间保持不变，接收端必须按它
  幂等去重；进程崩溃后租约到期即可继续投递；
- 请求头包含 `X-BeaconWatch-Event`、`X-BeaconWatch-Delivery`、
  `X-BeaconWatch-Timestamp`、`X-BeaconWatch-Signature` 和尝试次数；
- 签名算法为
  `HMAC-SHA256(secret, timestamp + "." + raw_request_body)`，输出
  `sha256=<hex>`。接收端应校验时间窗并拒绝重放。

### 身份、权限与审计

- `RID_AUTH_USERS_FILE` 中的每个条目是独立主体，key 只保存 SHA-256
  摘要；文件内容变化会在轮询周期内原子替换，坏文件不会淘汰上一份有效
  凭据。
- 角色层级为 `viewer < operator < admin`。viewer 只能读取；operator
  额外可以确认、解决和重开告警；admin 还可以修改禁飞区并读取跨资源
  审计。
- HTTP 使用 `Authorization: Bearer <key>`；浏览器 WebSocket 使用
  `Sec-WebSocket-Protocol: bearer.<key>`，服务端只回显 `rid.v1`，不会
  把 key 写回握手响应。
- 认证主体由服务端写入告警状态和审计记录，请求体中的处置人字段不能
  覆盖它。`alert_events` 保留业务时间线，`audit_events` 保留跨资源的
  安全访问审计，两者都带 `X-Request-ID` 对应的 request id。

### HTTP / WebSocket

- `GET /stations`
- `GET /drones`
- `GET /drones/unidentified`
- `GET /drones/{drone_id}`
- `GET /observations`
- `GET /observations/page`
- `GET /zones`, `PUT /zones/{zone_id}`
- `GET /alerts`
- `GET /alerts/page`
- `GET /alerts/stats`
- `GET /alerts/{alert_id}`
- `GET /alerts/{alert_id}/events`
- `POST /alerts/{alert_id}/ack`
- `POST /alerts/{alert_id}/resolve`
- `POST /alerts/{alert_id}/reopen`
- `WS /ws/observations`
- `GET /health/live`, `GET /health/ready`, `GET /healthz`
- `GET /metrics`

`GET /observations` 在还有下一页时返回 `X-Next-Cursor`，也接受
`cursor=`；`/observations/page` 返回 `{items, next_cursor}`。游标基于
`(received_at, observation_id)`，避免深 offset 扫描和翻页期间的新数据漂移。

告警使用独立的带类型游标：`GET /alerts` 保持原有数组响应并在还有更早
记录时返回 `X-Next-Cursor`；`GET /alerts/page` 返回
`{items, next_cursor}`。把观测游标传给告警接口会得到 400，避免两类游标
被误用。

WebSocket 推送以下 frame：

```json
{"type": "obs", "data": {"...": "DroneObservation"}}
{"type": "alert", "data": {"...": "Alert"}}
{"type": "ping"}
```

客户端可以发送 `{"type":"ping"}`，服务端回复 `{"type":"pong"}`。服务端按
`RID_WS_PING_INTERVAL_S` 定时 ping，浏览器据此检测半开连接并重连。

### 多副本与实时总线

- 默认 `RID_BUS_BACKEND=memory` 只适用于单进程；它没有跨进程共享状态。
- 多副本设置 `RID_BUS_BACKEND=redis` 后，任何副本产生的观测和告警 frame
  都会发布到同一个 Redis channel，各副本再投递给自己的 WebSocket 客户端；
  负载均衡器不要求 WebSocket 粘性会话。
- `RID_MQTT_SHARED_GROUP` 在多副本间启用 MQTT shared subscription，让每条
  MQTT 消息只被一个副本消费。默认留空以保持单副本行为。
- MQTT 客户端 ID 自动追加 `<hostname>-<pid>` 实例后缀；即使 Compose 传入
  空的 `RID_INSTANCE_ID`，副本也不会互相踢下线。
- 告警去重不依赖进程内状态：数据库 `alerts.triggered_at` 是跨副本的发射
  闸门，PostgreSQL 事务级 advisory lock 串行化同一 `alert_id` 的首次插入。
  去重窗口内的重复候选不会增加 `trigger_count`、写审计或排队通知。
- Redis 不可用时，发布失败会降级为当前副本本地 fan-out，并指数退避重连；
  其他副本暂时收不到实时 frame，但 REST 历史仍然完整。
- `RID_REDIS_REQUIRED=true` 不会让进程崩溃，而是让 `/health/ready` 返回
  503，直到共享总线恢复。Redis Pub/Sub 是 at-most-once，不能替代数据库
  或 durable outbox。
- 每个副本都可以运行通知和保留 worker；通知通过带租约的行锁和
  `SKIP LOCKED` 避免同一投递被并发处理。共享总线部署仍需要受控的 Redis
  逻辑实例、访问控制和网络隔离。

## 7. 前端

- WebSocket 只保留最近 200 条实时观测；
- HTTP GET 有 8 秒超时、最多 2 次指数退避重试，并支持 AbortController；
- 页面隐藏时停止 presence/health 轮询，回到前台立即刷新；
- WebSocket 45 秒收不到任何 frame 时主动关闭并重连；
- 历史时间轴使用 REST 查询，不把完整数据库复制进浏览器；
- 历史/未识别列表按固定 4 秒节流刷新，避免每条 WS 消息触发一次大查询；
- 告警面板读取服务端聚合统计，并在需要时通过游标加载更早的持久化告警；
- 地图弹窗使用 DOM `textContent`/`setDOMContent`，不把无人机字段拼进 HTML；
- MapLibre 独立分包，主 bundle 不包含地图引擎。
- 生产代理可在运行时生成 viewer key 的 `config.js`，构建产物不包含 key；
  浏览器仍可读取该 viewer key，因此只能授予只读权限。

## 8. 数据库

连接池使用 `RID_DB_POOL_MIN` 个常驻连接和
`RID_DB_POOL_MAX - RID_DB_POOL_MIN` 个溢出连接；等待上限由
`RID_DB_POOL_TIMEOUT_S` 控制。PostgreSQL 查询默认带
`RID_DB_STATEMENT_TIMEOUT_MS=30000` 语句超时，避免慢查询长期占用连接。
`/drones/unidentified` 使用只覆盖 `operator_id IS NULL` 行的部分索引。

- TimescaleDB extension 与 `observations` hypertable；
- 经纬度使用普通 Float 列和组合索引，不依赖 PostGIS；
- 最近站点、station/operator 时间窗和 `(received_at, observation_id)` 游标
  查询都有对应索引；
- geofence polygon 以 JSON/JSONB 保存，API、规则引擎和地图读取同一份几何；
- `alerts` 表保存当前状态、触发次数、首次/最近触发时间、确认与解决字段，
  并按 `(triggered_at, alert_id)`、状态/触发时间、状态/解决时间、无人机/
  触发时间、规则/触发时间和严重度/触发时间建立索引；
- `alert_events` 表以追加方式保存触发、确认、解决和重开事件，并记录
  认证主体与请求 ID；事件不覆盖历史；
- `audit_events` 表以追加方式保存跨资源的写操作，包括操作者 subject/
  显示名/角色、请求 ID、动作、资源、结果和请求路径；告警业务事件与
  安全访问审计相互独立；
- `notification_outbox` 保存待投递 payload、尝试/失败次数、下次尝试时间、
  租约、死信原因和投递时间；状态与下次尝试时间、状态与更新时间、
  告警与状态均有组合索引；
- Alembic 在容器启动时自动执行 `upgrade head`。

## 9. 运维接口

- `/health/live`：进程存活，不访问数据库；
- `/health/ready`：执行 `SELECT 1`，数据库不可用返回 503；配置 Redis 总线
  且 `RID_REDIS_REQUIRED=true` 时，Redis 未连接也会返回 503；
- `/metrics`：MQTT 连接、接收/入库/重复/非法消息、重连、站点心跳、WS
  客户端/连接/拒绝/非法 frame/丢弃帧、HTTP 请求/延迟 histogram/状态码/
  鉴权失败、数据库可用性、ingest 延迟 histogram、通知 outbox 各状态/
  尝试/失败/投递延迟 histogram、Redis 总线连接/重连/发布失败/远端 frame
  计数、worker 状态，以及保留任务删除数/失败数/最近成功时间。

生产参考拓扑、Prometheus 告警规则、容量测试和故障演练见
`deploy/production/` 与 `04-operations.md`。

## 10. 安全现状与生产限制

当前 API 支持具名 API key、viewer/operator/admin RBAC、热加载轮换、请求级
安全审计和旧版单一 admin token；服务端 MQTT 客户端支持校验 TLS。Compose
默认仍是开发凭据和明文 MQTT。在公网或不可信网络部署前必须补齐：

1. OIDC/SSO、MFA 或统一身份网关（API key 适合服务和本地运维，不替代人员登录）；
2. HTTPS/WSS 反向代理；
3. MQTT ACL、每站客户端证书/账号和 Broker 侧 TLS 配置；
4. 密钥管理、数据库最小权限、非默认密码；
5. Redis 的生产级 ACL/TLS、Sentinel/Cluster 高可用和故障演练；
6. 告警通知的多渠道分发、值班升级策略和跨区域审计归档。

这些是部署前置条件，不是仓库当前已经实现的功能。

## 11. 目录

```text
edge/       接收器、ODID parser、SQLite outbox、MQTT publisher
server/     FastAPI、MQTT ingest、Alembic、TimescaleDB、规则引擎
frontend/   React + TypeScript + MapLibre 控制台
tests/      跨包 wire contract 与官方帧全链路测试
scripts/    真实 E2E 验证脚本
docs/       架构、部署、测试文档
```

## 12. 后续优先级

1. OIDC/SSO、MQTT ACL、密钥托管与生产 TLS 渗透测试；
2. 多站定位/时差融合与轨迹质量评分；
3. Redis 高可用、连接池容量和跨区域部署；
4. 真实无人机/抓包回放集与现场验收基线。
