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

### 服务端单元/集成测试

```bash
PYTHONPATH=server/src .venv/bin/python -m pytest -q server/tests
```

使用内存 SQLite 和 FastAPI ASGI transport，覆盖：

- 入库、幂等、drone 聚合、站点心跳；
- REST 查询、时间窗口、bbox、未识别过滤；
- WebSocket fan-out；
- 禁飞区/限高/未识别告警；
- 稳定 alert ID；
- health/readiness/metrics；
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
和 FastAPI 路由，验证官方帧最终能通过 HTTP 查询。

### 前端

```bash
cd frontend
npm ci
npm audit --omit=dev --audit-level=high
npm run typecheck
npm run build
```

构建把 MapLibre 拆成独立 vendor chunk。生产依赖审计必须为 0 个 high/critical。

### Docker 冷启动

CI 会构建服务端镜像并校验 Compose 配置。发布前人工验证应使用独立 Compose
project、独立端口和独立数据卷，不要对现有生产卷执行 `down -v`。预期结果：

1. 空库启动时自动执行全部 Alembic 迁移；
2. `/health/ready` 返回 200；
3. `/metrics` 显示 MQTT 已连接；
4. 测试容器和测试卷可以独立删除。

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

- Python 3.11：安装、`pip check`、Ruff、edge/server/contract 测试；
- Node 22：`npm ci`、生产依赖安全审计、TypeScript、生产构建；
- Docker：Compose 配置校验和服务端镜像构建。

推送 `v*` tag 时，`.github/workflows/release.yml` 会再次执行测试和前端构建，
成功后自动创建 GitHub Release。

## 5. 尚未覆盖

- 真实无线电硬件上的长期稳定性；
- 多机高并发/容量压测；
- 多副本服务端和共享总线的故障切换；
- 浏览器端 E2E/视觉回归；
- 生产 TLS、认证和 MQTT ACL 的渗透测试。
