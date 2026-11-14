# 无人机 RID 检测系统实现计划

> **历史设计稿，非当前实现说明。** 文中 Redis、PostGIS、JWT、mTLS、ACL 和
> `deploy/` 文件属于早期规划；当前真实能力与限制以
> `docs/01-architecture.md` 为准。

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 构建端到端无人机 Remote ID (RID) 检测系统: 边缘工控机接收+解析+MQTT上报, 服务器端汇总+存储+实时展示。

**Architecture:**
```
┌──────────────────┐    BLE 5.x / Wi-Fi NaN    ┌──────────────┐
│  无人机 (DJI等)  │ ─────────────────────────▶│  边缘工控机  │
│  周期广播 RID    │   ASTM F3411-22 消息       │  (scanner)  │
└──────────────────┘                           └──────┬───────┘
                                                     │ MQTT 5.0
                                                     │ TLS 1.3
                                                     ▼
                                            ┌────────────────┐
                                            │  Mosquitto     │
                                            │  Broker        │
                                            └────────┬───────┘
                                                     │
                                          ┌──────────┴──────────┐
                                          ▼                     ▼
                                ┌──────────────────┐  ┌─────────────────┐
                                │  FastAPI 接收服务 │  │  WebSocket 推送 │
                                │  REST 历史查询   │  │  实时活跃飞行器 │
                                └────────┬─────────┘  └─────────────────┘
                                         │
                                         ▼
                                ┌──────────────────┐
                                │  TimescaleDB     │
                                │  (PostgreSQL +   │
                                │   PostGIS)       │
                                └────────┬─────────┘
                                         │
                                         ▼
                                ┌──────────────────┐
                                │  Vue 3 前端      │
                                │  Leaflet 地图    │
                                │  暗色 Air-Traffic-Control 风格 │
                                └──────────────────┘
```

**Tech Stack:**
- 边缘: Python 3.11 + bleak (BLE) + scapy (Wi-Fi) + paho-mqtt + pydantic
- 服务器: FastAPI + asyncpg + TimescaleDB + Mosquitto
- 前端: Vue 3 + Vite + TypeScript + Leaflet + Pinia
- 测试: pytest + docker-compose (集成测试)
- 部署: docker-compose

---

## 项目结构

```
rid-system/
├── edge/                          # 边缘端工控机代码
│   ├── pyproject.toml
│   ├── README.md
│   ├── src/rid_edge/
│   │   ├── __init__.py
│   │   ├── __main__.py           # python -m rid_edge 入口
│   │   ├── config.py             # 配置加载 (pydantic-settings)
│   │   ├── models.py             # RID 数据模型 (Pydantic)
│   │   ├── parsers/
│   │   │   ├── __init__.py
│   │   │   ├── base.py           # 抽象解析器接口
│   │   │   ├── astm_f3411.py     # ASTM F3411-22 蓝牙广播解析
│   │   │   └── wifi_nan.py       # Wi-Fi NaN 探测请求解析
│   │   ├── receivers/
│   │   │   ├── __init__.py
│   │   │   ├── base.py           # 接收器抽象
│   │   │   ├── ble_receiver.py   # bleak 蓝牙扫描
│   │   │   └── wifi_receiver.py  # scapy Wi-Fi 监听
│   │   ├── pipeline.py           # 接收 → 解析 → 去重 → 转发
│   │   ├── deduplicator.py       # 滑动窗口去重 (同 RID 多次广播)
│   │   ├── mqtt_publisher.py     # paho-mqtt 异步发布
│   │   ├── metrics.py            # prometheus 指标
│   │   └── station.py            # 工控机自身位置 (GPS锚定)
│   └── tests/
│       ├── conftest.py           # PCAP/BLE capture fixtures
│       ├── fixtures/
│       │   ├── astm_f3411_sample.bin  # 真实BLE广播样本
│       │   └── wifi_nan_sample.pcap  # 真实802.11管理帧样本
│       ├── test_parsers_astm.py
│       ├── test_parsers_wifi.py
│       ├── test_deduplicator.py
│       ├── test_mqtt_publisher.py
│       └── test_pipeline.py
│
├── server/                        # 服务器端
│   ├── pyproject.toml
│   ├── README.md
│   ├── src/rid_server/
│   │   ├── __init__.py
│   │   ├── __main__.py
│   │   ├── config.py
│   │   ├── db.py                 # TimescaleDB 异步连接
│   │   ├── models.py             # ORM + Pydantic
│   │   ├── mqtt_ingest.py        # aiomqtt 订阅 → 写库
│   │   ├── api/
│   │   │   ├── __init__.py
│   │   │   ├── observations.py   # GET /observations 历史
│   │   │   ├── live.py           # WebSocket /ws/live
│   │   │   ├── drones.py         # GET /drones, /drones/{id}
│   │   │   └── stats.py          # GET /stats 汇总
│   │   ├── aggregator.py         # 1s 滑动窗口聚合 (活跃飞行器)
│   │   └── auth.py               # API token 鉴权
│   ├── migrations/
│   │   └── 001_init.sql          # TimescaleDB hypertable
│   └── tests/
│       ├── conftest.py
│       ├── test_api_observations.py
│       ├── test_aggregator.py
│       └── test_mqtt_ingest.py
│
├── frontend/                      # Vue 3 前端
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   ├── index.html
│   ├── src/
│   │   ├── main.ts
│   │   ├── App.vue
│   │   ├── style.css
│   │   ├── api/
│   │   │   ├── client.ts         # fetch wrapper
│   │   │   └── websocket.ts
│   │   ├── stores/
│   │   │   └── drones.ts         # Pinia store
│   │   ├── components/
│   │   │   ├── DroneMap.vue      # Leaflet 实时地图
│   │   │   ├── DroneList.vue     # 活跃列表
│   │   │   ├── DroneDetail.vue   # 详情面板
│   │   │   ├── StatsBar.vue      # 顶部统计
│   │   │   └── Timeline.vue      # 历史轨迹回放
│   │   └── types/
│   │       └── drone.ts
│   └── tests/
│       └── (vitest)
│
├── mosquitto/                     # MQTT broker 配置
│   ├── mosquitto.conf
│   └── passwd
│
├── docker-compose.yml             # 完整 stack
├── .env.example
├── Makefile                       # make up / make test / make e2e
├── README.md
└── .gitignore
```

---

## 任务列表 (TDD 严格遵循)

### Phase 1: 基础设施

**Task 1: 项目骨架与 git 仓库**
- 初始化 git 仓库, 创建目录结构
- 创建 .gitignore, README.md
- Commit: "chore: project skeleton"

**Task 2: 共享数据模型 (Pydantic)**
- 在 `edge/src/rid_edge/models.py` 和 `server/src/rid_server/models.py` 定义:
  - `RidObservation` (统一的 RID 上报数据类)
  - `DroneId`, `DronePosition`, `OperatorLocation`
- 字段: drone_id, operator_id, position(lat,lon,alt), speed, heading,
  timestamp, protocol (astm|wifinan), rssi, station_id
- 测试: test_models.py
- Commit: "feat: shared RID data models"

### Phase 2: 边缘端 (Edge)

**Task 3: ASTM F3411-22 解析器**
- 实现 `parsers/astm_f3411.py`
- 解析 BLE 广播中的 ASTM 消息 (25 byte Basic ID + 扩展消息)
- 包括: UAS ID, Position, Self-ID, System, Operator ID
- 测试: 给定 hex 字节流 → 解析为 RidObservation
- Commit: "feat(edge): ASTM F3411-22 parser"

**Task 4: Wi-Fi NaN 解析器**
- 实现 `parsers/wifi_nan.py`
- 解析 802.11 探测请求中的 RID 字段
- 测试: 给定 PCAP 中的 frame 字节 → 解析
- Commit: "feat(edge): Wi-Fi NaN parser"

**Task 5: 抽象接收器接口**
- `receivers/base.py` 定义 Receiver 协议
- 异步生成器 yield 原始字节流
- 测试: dummy receiver 验证接口
- Commit: "feat(edge): receiver abstraction"

**Task 6: BLE 接收器 (bleak)**
- `receivers/ble_receiver.py` 使用 bleak 扫描
- 过滤 ASTM 厂商 ID (0xFA47)
- 测试: mock bleak 回调, 验证字节流传递
- Commit: "feat(edge): BLE 5.x receiver"

**Task 7: Wi-Fi 接收器 (scapy)**
- `receivers/wifi_receiver.py` 使用 scapy 监听 802.11
- 过滤 NaN 探测请求 (action frame 0x0D subtype)
- 测试: 给定 PCAP 文件 → 输出字节流
- Commit: "feat(edge): Wi-Fi NaN receiver"

**Task 8: 去重器**
- `deduplicator.py` 滑动窗口 (5s) 去重相同 drone_id
- 记录每个 drone_id 的最后观测时间 + 计数
- 测试: 5s 内同 ID 多次 → 1 个 unique; 5s 后重复 → 算新
- Commit: "feat(edge): sliding window deduplicator"

**Task 9: MQTT 发布器 (paho-mqtt)**
- `mqtt_publisher.py` 异步发布 RidObservation JSON
- Topic: `rid/v1/{station_id}/observation`
- 失败重连 + 本地缓存 (内存 ring buffer, 1k 条)
- 测试: mock paho client, 验证发布调用
- Commit: "feat(edge): MQTT publisher"

**Task 10: 流水线整合**
- `pipeline.py` 串接: receiver → parser → dedup → publisher
- 优雅关闭 (SIGINT/SIGTERM)
- 测试: 端到端 pipeline test with dummy receiver
- Commit: "feat(edge): pipeline integration"

**Task 11: 边缘端 CLI 入口**
- `__main__.py` 启动完整流水线
- 配置文件加载 (YAML)
- Commit: "feat(edge): CLI entry point"

### Phase 3: 服务器端 (Server)

**Task 12: 数据库 schema (TimescaleDB)**
- migrations/001_init.sql:
  - `observations` hypertable (time, drone_id, station_id, lat, lon, alt, ...)
  - `drones` 维度表 (drone_id, first_seen, last_seen, total_observations)
  - 索引: drone_id, station_id, time DESC
- 测试: 在临时 TimescaleDB 容器中跑迁移
- Commit: "feat(server): TimescaleDB schema"

**Task 13: 异步数据库访问**
- `db.py` 使用 asyncpg + 连接池
- 写入: `INSERT INTO observations ...`
- 查询: 时间范围 / 区域 / drone_id
- 测试: 容器中插入 + 查询
- Commit: "feat(server): async db layer"

**Task 14: MQTT 接收器 (aiomqtt)**
- `mqtt_ingest.py` 订阅 `rid/v1/+/observation`
- 解析 JSON → 写库
- 测试: 启动嵌入式 broker, 发布消息, 验证写入
- Commit: "feat(server): MQTT ingest"

**Task 15: FastAPI 基础**
- `__main__.py` + 路由挂载
- CORS, 异常处理, 健康检查 /health
- 测试: TestClient 验证 /health
- Commit: "feat(server): FastAPI base"

**Task 16: 历史观测 API**
- GET /observations?from=...&to=...&drone_id=...&station_id=...
- 支持时间范围 + 区域过滤
- 测试: 预填充数据库, 验证查询
- Commit: "feat(server): observations API"

**Task 17: 飞行器 API**
- GET /drones (当前活跃, 5min 内观测)
- GET /drones/{id} (单台飞行器最近观测)
- 测试: 验证活跃判定
- Commit: "feat(server): drones API"

**Task 18: WebSocket 实时推送**
- /ws/live 每 1s 推送当前活跃飞行器
- 使用 in-memory aggregator (1s 滑动窗口)
- 测试: 客户端连接, 收到推送
- Commit: "feat(server): WebSocket live feed"

**Task 19: 统计 API**
- GET /stats 总数/活跃数/覆盖区域
- 测试: 验证计算逻辑
- Commit: "feat(server): stats API"

### Phase 4: 前端 (Frontend)

**Task 20: Vite + Vue 3 + TypeScript 初始化**
- package.json, vite.config.ts, tsconfig.json
- 基础 main.ts, App.vue
- Commit: "feat(frontend): Vite + Vue 3 scaffold"

**Task 21: API 客户端 + WebSocket hook**
- api/client.ts (fetch wrapper, 鉴权)
- api/websocket.ts (自动重连)
- 测试: vitest
- Commit: "feat(frontend): API + WS client"

**Task 22: Pinia store (drones)**
- 维护实时活跃飞行器
- WebSocket 推送更新
- 历史查询 actions
- Commit: "feat(frontend): drones store"

**Task 23: Leaflet 实时地图**
- DroneMap.vue
- 暗色 tiles (CartoDB Dark Matter)
- 飞行器 marker + 轨迹
- 实时位置更新
- Commit: "feat(frontend): live drone map"

**Task 24: 飞行器列表 + 详情**
- DroneList.vue (左栏列表)
- DroneDetail.vue (右侧详情面板)
- 点击 marker 选中
- Commit: "feat(frontend): drone list and detail"

**Task 25: 顶部统计栏**
- StatsBar.vue (总观测数, 活跃数, 覆盖站点数)
- Commit: "feat(frontend): stats bar"

**Task 26: 历史轨迹回放**
- Timeline.vue (时间轴 slider)
- 回放某台飞行器过去 24h 轨迹
- Commit: "feat(frontend): timeline playback"

**Task 27: 暗色 ATC 风格 polish**
- frontend-design 风格: 暗色主题 + 蓝绿色 accents
- 字体: JetBrains Mono (技术感)
- 雷达扫描动画 (decoration)
- 启动动画 (staggered fade-in)
- Commit: "feat(frontend): dark ATC aesthetic polish"

### Phase 5: 部署与集成

**Task 28: Mosquitto broker 配置**
- mosquitto/mosquitto.conf (TLS, ACL)
- mosquitto/passwd (auth)
- Commit: "feat(deploy): mosquitto config"

**Task 29: docker-compose.yml**
- 服务: mosquitto, server, frontend (nginx), timescaledb
- 网络配置, 卷挂载
- Commit: "feat(deploy): docker-compose"

**Task 30: Makefile + README**
- make up / make down / make test / make e2e / make logs
- README 完整部署文档
- Commit: "docs: Makefile and README"

**Task 31: 端到端测试**
- 启动完整 docker-compose
- 模拟飞行器发布 ASTM 消息
- 验证: 服务器收到 → 写库 → WebSocket 推送 → 前端显示
- Commit: "test: end-to-end pipeline"

---

## 关键设计决策

1. **协议双模**: ASTM 主导, Wi-Fi NaN 作为补充, 抽象 Parser 接口
2. **MQTT QoS 1**: 至少一次, 服务器侧用 (drone_id, station_id, time) 唯一索引去重
3. **TimescaleDB**: 适合时间序列 + GIS 查询
4. **WebSocket 聚合**: 1s 推一次, 避免高频推送阻塞前端
5. **前端暗色 ATC 风格**: 符合 Air Traffic Control 行业惯例
6. **测试驱动**: 每个解析器/接收器都有真实样本测试, mock 最小化

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| 真实 BLE/Wi-Fi 硬件依赖 | 测试用 PCAP/BIN fixture, 硬件在 prod 环境验证 |
| TimescaleDB 容器化复杂 | 用官方镜像, migration 在 entrypoint 执行 |
| WebSocket 断线 | 前端自动重连 + 指数退避 |
| 大数据量写库 | TimescaleDB chunk + 批量 insert |

## 验证

- `make test` 单元测试 100% 通过
- `make e2e` 端到端: 启动 stack → 模拟飞行器 → 看到地图标记
- `make lint` ruff + mypy + eslint 通过

## 工时估计

- Phase 1: 0.5h
- Phase 2 (Edge): 4-5h
- Phase 3 (Server): 3-4h
- Phase 4 (Frontend): 3-4h
- Phase 5 (Deploy + E2E): 1-2h
- **总计: 12-15h** (按 TDD + subagent-driven-development 执行)
