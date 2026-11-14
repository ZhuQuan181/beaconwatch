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
│           SQLite outbox → aiomqtt publisher                           │
│           QoS wait / timeout re-queue / bounded eviction              │
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
│  MqttSubscriber                                                      │
│    ├─ Pydantic validation                                            │
│    ├─ station heartbeat upsert                                      │
│    ├─ idempotent observation insert                                  │
│    ├─ alert rules                                                    │
│    └─ in-process ObservationBus ──────────────┐                      │
│                          │                     │                      │
│                          ▼                     ▼                      │
│                    TimescaleDB          /ws/observations              │
│                    observations,        WebSocket fan-out             │
│                    drones, stations,                                  │
│                    geofence_zones                                     │
│                          │                                            │
│                          ▼                                            │
│                    FastAPI REST API                                    │
└──────────────────────────┬───────────────────────────────────────────┘
                           │ HTTP / WebSocket
                           ▼
┌──────────────────────────── Frontend ────────────────────────────────┐
│ React + TypeScript + MapLibre                                        │
│ live map / drone list / replay timeline / alerts / station coverage   │
└──────────────────────────────────────────────────────────────────────┘

未使用：Redis、PostGIS、JWT、mTLS、MQTT ACL 配置、服务端多副本共享总线。
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

## 6. 服务端行为

### 入库

1. 解析 MQTT topic 和 JSON；
2. Pydantic 校验 `DroneObservation`；
3. 先 upsert station，满足外键；
4. upsert drone 汇总；
5. 插入 observation；重复键不重复计数；
6. 提交后执行告警规则并推送 WebSocket。

### 站点心跳

`rid/v1/{station_id}/info` 会更新站点名称、坐标、高度、固件版本和
`last_seen`。topic 中的站点 ID 优先，payload 不能冒充其它站点。

### 告警

- `geofence`：点位落在 polygon（可选高度带）内；
- `altitude`：超过配置的全局限高；
- `unidentified`：收到无 `operator_id` 的观测；
- 同一 `(drone, rule, zone)` 在窗口内去重；
- `alert_id` 使用稳定 SHA-256 摘要，不使用 Python 加盐 `hash()`；
- 告警当前保存在进程内有界 ring buffer，服务重启后丢失；原始观测仍在数据库。

### HTTP / WebSocket

- `GET /stations`
- `GET /drones`
- `GET /drones/unidentified`
- `GET /drones/{drone_id}`
- `GET /observations`
- `GET /zones`, `PUT /zones/{zone_id}`
- `GET /alerts`
- `WS /ws/observations`
- `GET /health/live`, `GET /health/ready`, `GET /healthz`
- `GET /metrics`

WebSocket 推送两类 frame：

```json
{"type": "obs", "data": {"...": "DroneObservation"}}
{"type": "alert", "data": {"...": "Alert"}}
```

## 7. 前端

- WebSocket 只保留最近 200 条实时观测；
- 历史时间轴使用 REST 查询，不把完整数据库复制进浏览器；
- 历史/未识别列表按固定 4 秒节流刷新，避免每条 WS 消息触发一次大查询；
- 地图弹窗使用 DOM `textContent`/`setDOMContent`，不把无人机字段拼进 HTML；
- MapLibre 独立分包，主 bundle 不包含地图引擎。

## 8. 数据库

- TimescaleDB extension 与 `observations` hypertable；
- 经纬度使用普通 Float 列和组合索引，不依赖 PostGIS；
- geofence polygon 以 JSON/JSONB 保存，API、规则引擎和地图读取同一份几何；
- Alembic 在容器启动时自动执行 `upgrade head`。

## 9. 运维接口

- `/health/live`：进程存活，不访问数据库；
- `/health/ready`：执行 `SELECT 1`，数据库不可用返回 503；
- `/metrics`：MQTT 连接、接收/入库/非法消息、重连、站点心跳、WS 客户端数。

## 10. 安全现状与生产限制

当前 API 和 MQTT 没有内建身份认证；Compose 使用开发凭据和明文 MQTT。
在公网或不可信网络部署前必须补齐：

1. API 登录、RBAC、审计日志；
2. HTTPS/WSS 反向代理；
3. MQTT TLS、每站客户端证书/账号和 topic ACL；
4. 密钥管理、数据库最小权限、非默认密码；
5. 多副本时用 Redis/NATS 等共享总线替代进程内 `ObservationBus`；
6. 告警持久化、确认/关闭流程和通知集成。

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

1. API 鉴权、MQTT TLS/ACL 与密钥管理；
2. 多站定位/时差融合与轨迹质量评分；
3. 告警持久化、确认流程和通知渠道；
4. 多副本共享 WebSocket 总线；
5. 真实无人机/抓包回放集与现场验收基线。
