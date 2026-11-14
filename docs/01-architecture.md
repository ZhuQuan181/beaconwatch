# RID 检测系统架构设计

> Remote ID detection system: industrial-PC edge receivers → MQTT broker → FastAPI backend → React/MapLibre web UI.

## 1. 系统目标

- **实时识别** 周边 1–5 km 范围内的民用无人机 (ASTM F3411 / Open Drone ID 兼容机型)
- **多协议接收** Wi-Fi Beacon (主) + BLE 4.x/5.x advertisements
- **边缘自治** 工业 PC (无 GUI, 无公网) 离线仍能缓存, 联网后批量上传
- **态势展示** 服务端 Web 控制台: 实时地图 / 飞行轨迹 / 历史回放 / 告警
- **可扩展** 边缘端可水平扩展 (一台工业 PC 覆盖一个监测区域), 服务端支持多基站聚合

## 2. 总体架构

```
┌────────────────────────────────────────────────────────────────────────────┐
│                          Edge  (工业 PC / 边缘端)                            │
│                                                                            │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │  Wi-Fi 嗅探   │  │  BLE 扫描     │  │  (future)     │  │  Station     │    │
│  │  scapy        │  │  bleak       │  │  LTE/5G modem │  │  info 心跳   │    │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘    │
│         │ raw frame       │ adv report      │                 │            │
│         ▼                 ▼                 ▼                 ▼            │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │  odid_parser  ──  Open Drone ID 消息解码 (Basic ID / Location /       │  │
│  │                   Self-ID / System / Operator ID)                    │  │
│  └─────────────────────────────────┬────────────────────────────────────┘  │
│                                    │ RidObservation (Pydantic)              │
│                                    ▼                                       │
│  ┌─────────────────┐    ┌──────────────────┐    ┌────────────────────┐    │
│  │  dedup          │───▶│  local buffer    │───▶│  MQTT publisher    │    │
│  │  (sliding TTL)  │    │  (disk-backed    │    │  aiomqtt + backoff │    │
│  │                 │    │   SQLite)        │    │                    │    │
│  └─────────────────┘    └──────────────────┘    └─────────┬──────────┘    │
└───────────────────────────────────────────────────────────┼───────────────┘
                                                            │ TLS 8883
                                                            ▼
                                              ┌──────────────────────────┐
                                              │  EMQX (MQTT broker)      │
                                              │  topics:                 │
                                              │   rid/v1/{station}/info  │
                                              │   rid/v1/{station}/obs   │
                                              └─────────┬────────────────┘
                                                        │ sub
                                                        ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                          Server  (服务端 / 云端)                              │
│                                                                           │
│  ┌──────────────────┐    ┌──────────────────┐    ┌────────────────────┐  │
│  │  ingest service  │───▶│  deduplicator    │───▶│  TimescaleDB       │  │
│  │  aiomqtt client  │    │  (Redis SET NX)  │    │  hypertable        │  │
│  └──────────────────┘    └──────────────────┘    │  observations      │  │
│                                                   └────────────────────┘  │
│                                                            │              │
│                                                            ▼              │
│  ┌──────────────────┐    ┌──────────────────┐    ┌────────────────────┐  │
│  │  REST API        │◀──▶│  WebSocket fan-  │◀──▶│  PostGIS spatial   │  │
│  │  FastAPI         │    │  out (Redis pub) │    │  (stations, geofence)│  │
│  └────────┬─────────┘    └────────┬─────────┘    └────────────────────┘  │
│           │                       │                                        │
└───────────┼───────────────────────┼────────────────────────────────────────┘
            │ HTTPS                 │ WSS
            ▼                       ▼
   ┌────────────────────────────────────────────┐
   │  Web Frontend                              │
   │   Vite + React 18 + MapLibre GL JS         │
   │   - 实时地图 (drone / station 位置)         │
   │   - 飞行轨迹 (Polyline)                    │
   │   - 历史回放 (时间轴 scrubber)              │
   │   - 告警面板 (geofence / 高度越界 / 禁飞区)  │
   │   - 站点管理 (CRUD)                        │
   └────────────────────────────────────────────┘
```

## 3. 关键技术选型

| 层 | 选型 | 理由 |
|---|---|---|
| 边缘端语言 | Python 3.11+ | 协议解析库成熟 (scapy / bleak), 与 AI 模型兼容 |
| Wi-Fi 接收 | scapy + monitor-mode 网卡 | 标准 802.11 帧解码, 零拷贝 |
| BLE 接收 | bleak (BlueZ) | Linux 原生 BLE 扫描, asyncio 接口 |
| 协议解析 | 自研 (odid_parser) | Open Drone ID 规范明确, 但没有主流 Python 实现 |
| 边缘调度 | asyncio | 单进程多任务, 适合 scapy + bleak + aiomqtt 共存 |
| 边缘消息 | aiomqtt (paho 异步版) | 官方 paho-mqtt 的 asyncio 包装, API 稳定 |
| 边缘持久化 | SQLite (WAL 模式) | 单文件, 零配置, 工业 PC 离线场景合适 |
| MQTT broker | EMQX 5.x | 单机百万连接, 自带 dashboard, 支持 TLS/ACL |
| 服务端框架 | FastAPI | async 原生, Pydantic v2, OpenAPI 自动生成 |
| 服务端消息 | aiomqtt + Redis pub/sub | 横向扩展友好 |
| 关系 + 时序 | PostgreSQL 16 + PostGIS 3.4 + TimescaleDB 2.x | 站点 / geofence 用 PostGIS, 观测用 hypertable |
| 缓存 | Redis 7 | dedup / pub-sub / 限流 |
| 前端 | Vite 5 + React 18 + MapLibre GL JS 4 | MapLibre 开源无 token, 性能足够 |
| 前端状态 | Zustand | 比 Redux 轻, 适合实时小数据流 |
| 部署 | docker-compose (生产) + systemd (边缘) | 边缘 PC 用 systemd, 服务端用容器 |
| 反向代理 | Caddy / Nginx | 自动 HTTPS (Caddy) 或手工证书 (Nginx) |

## 4. 数据模型 (wire schema)

整个系统共享一份 Pydantic schema (`rid_edge.models.RidObservation`):

```json
{
  "observation_id": "01H8XYZ...",        // ULID, 幂等键
  "drone_id":       "1Z0VK9...",          // ASTM UAS ID
  "operator_id":    "OP-42",              // 可能为空
  "protocol":       "astm_f3411",         // astm_f3411 | wifi_nan
  "station_id":     "station-01",
  "rssi_dbm":       -65,
  "position": {
    "lat": 37.7749, "lon": -122.4194, "alt_m": 120.0,
    "accuracy_h_m": 10.0
  },
  "speed_mps":   15.5,
  "heading_deg": 90.0,
  "drone_timestamp": "2026-06-06T12:00:00Z",
  "received_at":     "2026-06-06T12:00:01.234Z",
  "operator_location": null,
  "self_id":         "survey mission"
}
```

服务端入库后额外生成: `id` (BIGSERIAL), `geom` (PostGIS Point), `ingested_at`, `is_alert` 等。

## 5. 通信协议 (MQTT topics)

| Topic | 方向 | Payload | QOS | 频率 |
|---|---|---|---|---|
| `rid/v1/{station_id}/info`  | edge → server | `StationInfo` JSON | 1 | 60 s 心跳 |
| `rid/v1/{station_id}/obs`   | edge → server | `RidObservation` JSON | 1 | 按事件 (1-3 Hz/无人机) |
| `rid/v1/+/info`             | server sub |  |  |  |
| `rid/v1/+/obs`              | server sub |  |  |  |
| `rid/v1/alerts`             | server → web | `AlertEvent` JSON | 0 | 事件 |
| `rid/v1/stream`             | server → web | `RidObservation` JSON | 0 | 实时 |

ACL: 仅 `rid/v1/{自己的station_id}/*` 可发布; `rid/v1/+` 可订阅 (服务端)。

## 6. 数据流时序

```
Wi-Fi Beacon  ──┐
                ├──▶ odid_parser ──▶ dedup ──▶ buffer ──▶ mqtt_pub ──┐
BLE adv       ──┘                                                       │
                                                                        ▼
                                                                   EMQX
                                                                        │
                                                                        ▼
                                                              ingest service
                                                                        │
                                                ┌───────────────────────┤
                                                ▼                       ▼
                                          Redis (dedup)         TimescaleDB
                                                                        │
                                                                        ▼
                                                              fan-out service
                                                                        │
                                                                        ▼
                                                              Redis pub/sub
                                                                        │
                                                                        ▼
                                                              WebSocket hub
                                                                        │
                                                                        ▼
                                                                 React UI
```

## 7. 关键流程

### 7.1 接收 → 上报

1. `wifi_sniffer` 在 monitor mode 网卡上抓 802.11 beacon/action frame
2. 提取 vendor-specific IE (OUI `FA:0B:BC` = Open Drone ID)
3. 解析为 ODID messages, 缓存同一帧的多个 sub-message
4. 拼装成 `BasicIDMessage + LocationMessage` 对
5. `dedup.seen((mac, drone_timestamp), obs)` → 命中则丢弃
6. 写入本地 SQLite buffer (offline-safe)
7. `mqtt_publisher` 异步从 buffer 读取并发送, 失败则保留在 buffer
8. 服务端 ack (pub-ack) 后才从 buffer 删

### 7.2 服务端入库

1. `ingest` 订阅 `rid/v1/+/obs` 与 `rid/v1/+/info`
2. 收到 obs: 先 Pydantic 验证 → 查 Redis `SET NX obs:{observation_id}` (去重) → 写 TimescaleDB
3. 写库成功后 fan-out 到 Redis `stream:observations`
4. WebSocket 端点从 Redis 订阅, 推给前端
5. 同时启动 geofence / altitude / speed 规则检查, 命中则发 `alerts`

### 7.3 离线容灾

- 边缘端断网时: obs 全部入 SQLite, 不丢
- 重连后: 批量回放, 服务端 `observation_id` 幂等去重
- 服务端断电: TimescaleDB WAL + 每日全量备份 (`pg_dump` cron)

## 8. 安全模型

| 层 | 措施 |
|---|---|
| MQTT | TLS 1.2+, 客户端证书 (每台边缘 PC 一张) |
| MQTT ACL | EMQX 内置规则, 按 `station_id` 限定发布权限 |
| 服务端 API | JWT (前端登录) + mTLS (边缘上报) 双轨 |
| 前端 | HTTPS only, CSP `default-src 'self'`, 地图 tile 限同源代理 |
| 数据库 | 仅本机监听, 通过 `pg_hba.conf` 限制来源 |
| 容器 | rootless, `read_only` 根 fs, 显式 cap-drop |

## 9. 性能预算

| 指标 | 目标 |
|---|---|
| 单台边缘 PC 并发识别无人机数 | ≥ 50 |
| obs 入库延迟 (端到端) | < 500 ms (P95) |
| WebSocket 推送延迟 | < 200 ms (P95) |
| Web UI 首屏 | < 1.5 s (4G) |
| TimescaleDB 单日写入 | ≥ 1 亿行 (50 站 × 50 机 × 4Hz × 24h ≈ 2.16 亿) |
| 历史回放查询 (24h 窗口, 1 万点) | < 1 s |

## 10. 项目目录

```
/home/zq/code/beaconwatch/
├── edge/                        # 边缘端
│   ├── pyproject.toml
│   ├── src/rid_edge/
│   │   ├── __init__.py
│   │   ├── models.py            # Pydantic schema (共享)
│   │   ├── common/
│   │   │   ├── config.py        # pydantic-settings
│   │   │   ├── logger.py        # structlog
│   │   │   ├── ulid.py          # 自研 ULID
│   │   │   ├── dedup.py         # 滑动窗口去重
│   │   │   └── backoff.py       # 指数退避
│   │   ├── odid_parser/         # Open Drone ID 解码
│   │   │   ├── basic_id.py
│   │   │   ├── location.py
│   │   │   ├── self_id.py
│   │   │   ├── system.py
│   │   │   ├── operator_id.py
│   │   │   ├── message_pack.py  # 帧级拼装
│   │   │   └── constants.py
│   │   ├── receivers/
│   │   │   ├── wifi_sniffer.py  # scapy
│   │   │   ├── ble_scanner.py   # bleak
│   │   │   └── base.py
│   │   ├── pipeline/
│   │   │   ├── parser.py        # raw frame → RidObservation
│   │   │   ├── dedup_filter.py
│   │   │   ├── buffer.py        # SQLite buffer
│   │   │   └── publisher.py     # aiomqtt
│   │   ├── station.py           # station 心跳
│   │   └── main.py              # asyncio 编排
│   ├── systemd/rid-edge.service
│   ├── tests/                   # pytest
│   └── README.md
│
├── server/                      # 服务端
│   ├── pyproject.toml
│   ├── alembic/                 # 数据库迁移
│   ├── app/
│   │   ├── main.py              # FastAPI 入口
│   │   ├── settings.py
│   │   ├── db/
│   │   │   ├── session.py
│   │   │   ├── models.py        # SQLAlchemy
│   │   │   └── timeseries.py    # Timescale helpers
│   │   ├── ingest/
│   │   │   ├── mqtt_subscriber.py
│   │   │   ├── deduplicator.py
│   │   │   └── rules/
│   │   │       ├── geofence.py
│   │   │       ├── altitude.py
│   │   │       └── speed.py
│   │   ├── api/
│   │   │   ├── observations.py
│   │   │   ├── stations.py
│   │   │   ├── geofences.py
│   │   │   └── alerts.py
│   │   ├── ws/
│   │   │   └── stream.py        # WebSocket
│   │   └── core/
│   │       ├── security.py
│   │       └── logging.py
│   └── tests/
│
├── web/                         # 前端
│   ├── package.json
│   ├── vite.config.ts
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx
│   │   ├── api/
│   │   ├── components/
│   │   │   ├── MapView.tsx
│   │   │   ├── DroneList.tsx
│   │   │   ├── AlertPanel.tsx
│   │   │   └── Timeline.tsx
│   │   ├── stores/              # zustand
│   │   └── hooks/
│   └── index.html
│
├── deploy/
│   ├── docker-compose.yml
│   ├── docker-compose.prod.yml
│   ├── Caddyfile
│   ├── mosquitto/               # (可选) 内置 broker
│   └── postgres/
│       └── init.sql             # PostGIS + TimescaleDB extensions
│
├── docs/
│   ├── 01-architecture.md       # 本文件
│   ├── 02-deployment.md
│   ├── 03-testing.md
│   └── 04-protocol-cheatsheet.md
│
└── scripts/
    ├── gen-cert.sh              # 自签 TLS
    ├── install-edge.sh
    └── dev-up.sh
```

## 11. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| monitor-mode 网卡驱动兼容性 | 边缘端无法收 Wi-Fi | 提供 rtl8812au / ath9k_htc / mt76 三种推荐硬件清单 |
| BLE 扫描在 Linux 上需 root | 部署复杂 | systemd unit 加 `CAP_NET_RAW` 而非 root |
| 协议库与真实无人机不一致 | 漏报 | 提供 `pcap` 测试样本 + golden test 锁住格式 |
| MQTT 断线堆积撑爆磁盘 | 边缘端停摆 | buffer 设硬上限 100 MB, 触顶按时间淘汰最旧 |
| WebSocket 客户端掉线丢消息 | UI 漏更新 | 服务端每条消息带 seq, 客户端请求 `since_seq` 补偿 |
| 高并发入库拖慢查询 | 前端卡 | Timescale 连续聚合 (continuous aggregate) + 物化视图 |
| 误报 (邻机/未授权机型被识别为禁飞) | 用户疲劳 | 规则可配, 提供 "确认/忽略" 反馈回路 |

## 12. 里程碑

| Phase | 交付 | 状态 |
|---|---|---|
| P0 | 数据模型 + 单元测试 | ✅ 完成 |
| P1 | 边缘端: Wi-Fi + 解析 + MQTT 端到端 | ⏳ 进行中 |
| P2 | 边缘端: BLE 通道 | ⏳ |
| P3 | 服务端: FastAPI + 入库 + REST | ⏳ |
| P4 | 服务端: WebSocket 实时推送 | ⏳ |
| P5 | 前端: 实时地图 + 列表 | ⏳ |
| P6 | 前端: 历史回放 + 告警 | ⏳ |
| P7 | 部署: docker-compose + systemd | ⏳ |
| P8 | 文档 + 端到端测试 | ⏳ |
