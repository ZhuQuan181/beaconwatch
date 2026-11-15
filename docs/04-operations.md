# 生产运维手册

本文面向已经采用 `deploy/production/` 参考拓扑的运维人员。它描述当前
仓库实际提供的健康检查、指标、恢复路径和验证命令；没有实测证据的容量
数字不会被写成承诺。

## 1. 生产拓扑与信任边界

`deploy/production/compose.yaml` 的主要服务：

| 服务 | 暴露方式 | 说明 |
|---|---|---|
| `proxy` | 宿主机 80/443 | TLS、静态前端、REST/WebSocket 反向代理 |
| `api` | 仅内部网络 | 多副本 FastAPI、MQTT ingest、通知/保留 worker |
| `migrate` | 不监听 | 一次性 Alembic 迁移任务 |
| `db` | 仅内部网络 | TimescaleDB/PostgreSQL |
| `mqtt` | 默认仅 `127.0.0.1:1883` | EMQX；远程站点应走 VPN/私有网络 |
| `redis` | 仅内部网络 | 实时 Pub/Sub；不保存历史 |

两个网络分别是 `backend`（internal）和 `frontend`。数据库、MQTT 和 Redis
没有公开宿主机端口。API 同时接入两个网络，以便通过代理接收请求并访问
外部 Webhook；外部流量不能直接访问 API 容器。

前端镜像不包含 API key。代理容器启动时从 Docker secret 读取 viewer key，
生成 `/config.js`。这个 key 仍会被浏览器读取，因此必须是最小权限的
`viewer` key，不能使用 operator/admin key。

启动、证书和密钥准备见 `deploy/production/README.md`。

## 2. SLO 模板

以下是建议起点，不是未经测试的容量承诺。每个生产环境应按实际设备、
载荷、网络和站点数量压测后调整。

| SLI | 建议目标 | 证据 |
|---|---|---|
| API availability | 30 天 ≥ 99.9% | 外部探针 + `up` |
| REST p95 | 预期负载下 ≤ 500 ms | `rid_http_request_duration_seconds` |
| REST p99 | 预期负载下 ≤ 1.5 s | 同上 |
| Ingest p95 | edge `received_at` → DB commit ≤ 2 s | `rid_observation_ingest_latency_seconds` |
| Ingest p99 | ≤ 5 s | 同上 |
| WebSocket fan-out | 正常消费者丢帧率 < 0.1% | `rid_ws_dropped_frames_total` 与 delivered |
| WebSocket handshake | 鉴权正确时 ≥ 99.9% 成功 | connections/rejected |
| Webhook p95 | ≤ 60 s | `rid_notification_delivery_lag_seconds` |
| Webhook 死信 | 持续为 0 | `rid_notification_outbox_dead` |
| 数据保留 | 按合规策略执行且无失败 | retention 删除/失败指标 |

不要只看平均耗时。告警和容量评审应使用 p95/p99、请求速率、并发 WebSocket
连接数、MQTT 重连数和数据库写入速率一起判断。

## 3. 指标地图

### HTTP 与进程

| 指标 | 类型 | 用途 |
|---|---|---|
| `rid_build_info` | gauge | version/env/instance 标签 |
| `rid_http_requests_total` | counter | 总请求数 |
| `rid_http_responses_total` | counter | 按 method/route/status 拆分 |
| `rid_http_request_duration_seconds` | histogram | HTTP 延迟，按 method/route |
| `rid_http_requests_in_flight` | gauge | 当前并发 |
| `rid_http_auth_failures_total` | counter | 无效/缺失 key |
| `rid_http_authz_failures_total` | counter | 权限不足 |
| `rid_database_up` | gauge | `/metrics` 能否完成数据库查询 |

### Ingest 与总线

| 指标 | 类型 | 用途 |
|---|---|---|
| `rid_mqtt_connected` | gauge | 当前 MQTT 会话 |
| `rid_mqtt_received_total` | counter | 收到消息 |
| `rid_mqtt_persisted_total` | counter | 新观测入库 |
| `rid_mqtt_duplicates_total` | counter | 幂等重复 |
| `rid_mqtt_invalid_total` | counter | schema/JSON 错误 |
| `rid_mqtt_reconnects_total` | counter | broker 断线重连 |
| `rid_observation_ingest_latency_seconds` | histogram | edge 接收至提交 |
| `rid_bus_backend_redis` | gauge | 是否启用共享总线 |
| `rid_bus_redis_connected` | gauge | 当前 Redis Pub/Sub 状态 |
| `rid_bus_redis_reconnects_total` | counter | Redis 重连 |
| `rid_bus_redis_publish_failures_total` | counter | 发布失败、本地降级 |
| `rid_bus_redis_received_frames_total` | counter | 从共享总线收到的 frame |

### WebSocket

| 指标 | 类型 | 用途 |
|---|---|---|
| `rid_ws_clients` | gauge | 当前本地订阅者 |
| `rid_ws_connections_total` | counter | 接受握手数 |
| `rid_ws_disconnections_total` | counter | 关闭连接数 |
| `rid_ws_rejected_total` | counter | 鉴权失败握手 |
| `rid_ws_malformed_messages_total` | counter | 浏览器发送非法 frame |
| `rid_ws_delivered_frames_total` | counter | 放入健康订阅队列的 frame |
| `rid_ws_dropped_frames_total` | counter | 慢消费者队列淘汰 |

### 告警通知与保留

| 指标 | 类型 | 用途 |
|---|---|---|
| `rid_notification_enabled` | gauge | 是否配置 Webhook |
| `rid_notification_worker_running` | gauge | worker 是否运行 |
| `rid_notification_outbox_pending` | gauge | 待发送 |
| `rid_notification_outbox_delivering` | gauge | 投递中/租约中 |
| `rid_notification_outbox_delivered` | gauge | 已成功 |
| `rid_notification_outbox_dead` | gauge | 死信 |
| `rid_notification_delivery_lag_seconds` | histogram | enqueue → 成功投递 |
| `rid_retention_worker_running` | gauge | 保留任务状态 |
| `rid_retention_failures_total` | counter | 清理失败 |
| `rid_retention_alerts_deleted_total` | counter | 已删除告警 |
| `rid_retention_notifications_deleted_total` | counter | 已删除通知 |

常用 PromQL：

```promql
# 全局 5xx 比例
sum(rate(rid_http_responses_total{status=~"5.."}[5m]))
/
clamp_min(sum(rate(rid_http_requests_total[5m])), 0.001)

# API p95
histogram_quantile(
  0.95,
  sum by (le) (rate(rid_http_request_duration_seconds_bucket[5m]))
)

# Ingest p95
histogram_quantile(
  0.95,
  sum by (le) (rate(rid_observation_ingest_latency_seconds_bucket[10m]))
)

# 当前本地 WebSocket 队列淘汰速率
sum(rate(rid_ws_dropped_frames_total[5m]))
```

仓库自带告警规则：

```text
deploy/observability/prometheus-rules.yml
```

规则中的 `job="beaconwatch-api"` 需要与 Prometheus scrape job 名称一致。
`BeaconWatchHighApiLatency` 和 `BeaconWatchHighIngestLatency` 使用比 SLO
更宽松的告警阈值，避免短时抖动直接触发；SLO 报表仍应使用上面建议的
500 ms/2 s 目标。

## 4. 可重复负载测试

`scripts/load_test.py` 同时驱动：

- HTTP 并发读取 `/observations?limit=50`；
- MQTT QoS 发布合成观测；
- 多个 WebSocket 客户端统计实时观测、ping/pong、重连和非法 frame；
- 服务端持久化、重复、非法消息、Redis 发布失败和 WebSocket 丢帧计数。

它只应连接一次性环境。合成数据使用 `LT-` 前缀，默认保留在数据库中；
测试后应销毁专用 Compose 项目/卷，或按站点的保留策略归档。

基本示例：

```bash
python scripts/load_test.py \
  --base-url http://127.0.0.1:58000 \
  --mqtt-host 127.0.0.1 --mqtt-port 51883 \
  --token "$VIEWER_OR_OPERATOR_KEY" \
  --duration 60 --http-concurrency 8 \
  --ws-clients 4 --ingest-rate 40 \
  --label "4-api-cpu-local-nvme" \
  --output-json /tmp/beaconwatch-load.json
```

只做读路径压测时设置 `--ingest-rate 0`。测试判据：

- HTTP/MQTT 错误率由 `--max-error-rate`（默认 1%）控制；
- WebSocket 收到的 synthetic 观测比例由 `--min-ws-delivery-ratio`
  （默认 95%）控制；
- 入库比例由 `--min-ingest-ratio`（默认 95%）控制；
- 输出包含 p50/p95/p99、机器 CPU/内存/负载、服务端 counter 差值和错误样本。

发布后停止 MQTT 发布不会立即代表入库完成。脚本会在 `--drain-s` 窗口内
轮询 `rid_mqtt_persisted_total`，直到所有成功发布的消息都已提交，或窗口
耗尽；JSON 中同时记录 `load_elapsed_s`、`drain_elapsed_s`、
`ingest.persisted_ratio`、`websocket.delivery_ratio`（相对已入库观测）和
`websocket.end_to_end_ratio`（相对成功发布的观测）。因此“WebSocket 到达
率 100%”不能替代入库比例，两个指标必须同时看。

不要将一次开发机结果称为生产容量。用相同数据量、相同拓扑、相同磁盘和
相同网络重复三次，记录中位数和磁盘/数据库瓶颈。

## 5. 故障演练

`scripts/e2e_resilience.py` 会实际停止 Compose 服务。它有三层保护：

1. 必须显式传入 `--allow-disruptive`；
2. 必须指定专用 `--compose-project`；
3. 拒绝 `server`、`rid-server`、`production` 等共享/默认项目名。

一键使用专用端口和专用卷：

```bash
make e2e-resilience
```

该目标使用：

- project：`bwresilience`；
- API：`127.0.0.1:58000`；
- MQTT：`127.0.0.1:51883`；
- PostgreSQL：`55433`；
- Redis：`56379`；
- 独立数据卷，并在结束时 `down -v`。

手工运行：

```bash
docker compose -p bwresilience \
  -f server/docker-compose.yml \
  -f deploy/testing/compose.resilience.yml up -d --build

python scripts/e2e_resilience.py \
  --compose-project bwresilience \
  --compose-file server/docker-compose.yml \
  --compose-file deploy/testing/compose.resilience.yml \
  --base-url http://127.0.0.1:58000 \
  --mqtt-host 127.0.0.1 --mqtt-port 51883 \
  --token resilience-token \
  --include-db --include-api --allow-disruptive

docker compose -p bwresilience \
  -f server/docker-compose.yml \
  -f deploy/testing/compose.resilience.yml down -v
```

演练覆盖：

- Redis 停止后 readiness/共享总线状态变化，本地 WebSocket 仍能收到入库帧；
- Redis 恢复后重新连接；
- EMQX 停止后 ingest 断开、恢复后重新入库；
- 数据库停止后 readiness 失败闭锁，恢复后无需重启 API 即重新可读；
- API 停止/启动后 readiness、MQTT 和 WebSocket 恢复。

WebSocket 浏览器重连本身由前端 watchdog/exponential backoff 负责；演练验证
服务端恢复后可重新握手和接收新的观测。

## 6. 备份与恢复

备份文件应写到宿主机或远端对象存储，而不是只留在容器卷中。生产 Compose：

```bash
mkdir -p backups
docker compose --env-file deploy/production/.env \
  -f deploy/production/compose.yaml exec -T db \
  pg_dump -U "$BW_DB_USER" -d "$BW_DB_NAME" -Fc \
  > "backups/beaconwatch-$(date +%Y%m%d-%H%M%S).dump"
sha256sum backups/*.dump > backups/SHA256SUMS
```

恢复前必须：

1. 停止写入或进入维护窗口；
2. 再做一次当前库备份；
3. 验证 dump 的校验和；
4. 在隔离数据库先演练恢复。

恢复到目标数据库：

```bash
cat backups/beaconwatch-YYYYMMDD-HHMMSS.dump | \
  docker compose --env-file deploy/production/.env \
    -f deploy/production/compose.yaml exec -T db \
    pg_restore -U "$BW_DB_USER" -d "$BW_DB_NAME" \
    --clean --if-exists --exit-on-error
```

恢复后检查：

```bash
curl -k https://127.0.0.1/api/health/ready
docker compose --env-file deploy/production/.env \
  -f deploy/production/compose.yaml exec -T api \
  alembic current
```

再抽查 `audit_events`、`alert_events`、最近观测和通知 outbox。不要把
会覆盖现有库的恢复命令放在自动定时任务中。

## 7. 升级与回滚

发布使用不可变镜像 tag，例如 `1.0.0`，不要依赖 `latest`。

升级顺序：

1. 备份数据库并确认校验和；
2. 更新 `BW_SERVER_IMAGE`、`BW_PROXY_IMAGE`；
3. `docker compose ... up -d --build`；
4. 等待 `migrate` 退出成功；
5. 检查全部 API 副本 `/health/ready`；
6. 验证 MQTT 入库、WebSocket、Webhook 和前端；
7. 观察 5xx、p95、重连和丢帧至少一个业务周期。

`migrate` 在 API 启动前执行一次，API 设置
`RID_SKIP_MIGRATIONS=1`，避免多副本并发迁移。迁移失败时 API 不会启动；
先修复数据库或恢复备份，不要带旧 schema 强行启动新版本。

代码回滚可切回旧镜像 tag。数据库回滚不应假定 Alembic downgrade 安全：
优先恢复升级前的兼容备份。若必须 downgrade，先在隔离环境验证每个迁移
的数据影响。

## 8. API key 轮换

服务端 key 文件热加载，不需要重启 API：

1. 为同一 subject 新建 key，保留旧 key；
2. 更新 operator/admin 客户端和值班终端；
3. 等待至少一个 `RID_AUTH_RELOAD_INTERVAL_S` 并验证新 key；
4. 删除旧 key，再次等待热加载；
5. 检查 `rid_http_auth_failures_total` 和关键操作审计。

viewer key 还会写入前端 `/config.js`。轮换后需要重启代理，使其从 secret
重新生成配置文件；浏览器刷新即可。不要把 viewer key 当成访问控制边界，
它本质上是所有能加载仪表盘的人都能读取的凭据。

数据库、Redis、MQTT 和 Webhook 密码使用 `*_FILE`：

- `RID_DB_PASSWORD_FILE`
- `RID_REDIS_URL_FILE`
- `RID_MQTT_PASSWORD_FILE`
- `RID_API_TOKEN_FILE`
- `RID_NOTIFY_WEBHOOK_SECRET_FILE`

文件在进程启动时读取，最大 64 KiB，缺失/为空/不可读会启动失败。更新这些
secret 后需要滚动重启对应服务。`RID_AUTH_USERS_FILE` 是热加载文件，不读入
内存配置。

## 9. 故障处理

### `/health/ready` 返回 503

- `database unavailable`：检查 PostgreSQL 状态、磁盘、连接数、迁移和
  `rid_database_up`；
- `redis bus unavailable`：检查 Redis secret、ACL、网络和
  `rid_bus_redis_connected`；
- 503 不应导致容器 crash loop；负载均衡器应摘除未就绪副本。

### MQTT 不再入库

1. 看 `rid_mqtt_connected`、`rid_mqtt_reconnects_total`；
2. 检查 broker health、凭据/TLS、topic 和 shared group；
3. `rid_mqtt_received_total` 有增长但 `persisted_total` 不增长时，检查
   `invalid_total`、数据库写入和日志；
4. 多个副本必须使用相同 shared group，实例 ID 必须唯一。

### WebSocket 频繁断线或丢帧

- `rid_ws_dropped_frames_total` 增长：客户端消费太慢或单机推送量过高；
- `rid_ws_rejected_total` 增长：key/role、代理的 subprotocol 或 Header；
- `rid_ws_connections_total` 与 disconnections 同步高速增长：检查负载均衡
  空闲超时、代理 read timeout、浏览器 watchdog 和网络；
- 代理 `proxy_read_timeout` 必须大于 `RID_WS_PING_INTERVAL_S`；
- 跨副本时检查 Redis 状态和远端 frame 计数。

### Webhook 积压或死信

- `pending` 高：接收端慢、超时、网络失败或 worker 未运行；
- `dead` 增长：检查 4xx/3xx、签名配置、URL DNS/TLS、接收端大小限制；
- 查看 outbox 的 `last_error`、attempts 和 worker 日志；
- 接收端必须按 `X-BeaconWatch-Delivery` 幂等，先快速返回 2xx 再异步处理；
- outbox 积压时不要直接删除 pending/delivering 行。

### 磁盘与保留

- TimescaleDB 数据、WAL、备份目录和容器日志分别监控；
- 保留策略默认关闭，启用后从保守期限开始；
- 有 pending/delivering Webhook 的告警不会被保留任务删除；
- 清理失败看 `rid_retention_failures_total`，不要用 `TRUNCATE` 绕过审计。

### 通知与审计存储

告警事件和跨资源安全审计是不可变记录。排障时不要直接 UPDATE 历史事件；
通过新的操作或管理流程修正状态，并在外部工单中记录原因。

## 10. 发布前检查表

- [ ] `python -m pytest`、Ruff、前端 typecheck/build/mock build 全绿；
- [ ] Python/Node 依赖审计无 high/critical；
- [ ] 开发 Compose、故障演练 Compose、生产 Compose 均 `config --quiet`；
- [ ] server 与 proxy 镜像构建成功，Nginx `-t` 和 Prometheus rules check 通过；
- [ ] 独立空卷冷启动自动迁移到最新版本，readiness 200；
- [ ] 官方帧 E2E、两副本 E2E、故障演练全部通过；
- [ ] load test 记录机器规格、参数、p50/p95/p99、错误率和丢帧；
- [ ] 数据库备份/恢复至少完成一次隔离环境演练；
- [ ] MQTT TLS/ACL、Redis ACL/TLS、OIDC/MFA、外部备份和证书续期按实际
      部署边界补齐；
- [ ] 发布后检查 CI、Pages、Release、Demo 和线上 readiness。

## 11. 当前实测基线

`v1.0.0` 在单机隔离 Compose 项目（`bwresilience`）上完成了一次
`scripts/load_test.py` 验收基线。环境为 Linux x86_64、40 vCPU、
31,828 MiB 内存、Python 3.12；API、TimescaleDB、EMQX、Redis 同机运行，
单 API 副本，未设置容器 CPU/内存配额。测试参数：

- duration 30 s，drain 上限 15 s；
- HTTP 8 并发，请求 `/observations?limit=50`；
- WebSocket 4 个客户端，3 s 连接预热；
- MQTT QoS 1，8 个合成无人机，站内观测 Topic；
- 每种速率单独运行，测试之间不销毁数据库。

| 目标速率 | 实际发布 | 最终入库 | HTTP 吞吐 / p95 / p99 | WS 到达 | drain |
|---:|---:|---:|---:|---:|---:|
| 10 msg/s | 294 | 294（100%） | 202.5 req/s / 50.9 ms / 93.7 ms | 100% | 2.1 s |
| 15 msg/s | 439 | 439（100%） | 199.5 req/s / 51.0 ms / 94.7 ms | 100% | 2.6 s |
| 20 msg/s | 580 | 580（100%） | 197.3 req/s / 51.3 ms / 94.0 ms | 100% | 5.4 s |

三档均无 HTTP、MQTT、WebSocket 错误，WebSocket 无丢帧，最终入库和实时
fan-out 均为 100%。但 20 msg/s 的排空时间明显上升，说明该点已经出现
持续积压，不能据此宣称更高持续吞吐或跨机器容量。这个表只用于同一主机、
同一 Compose 拓扑下的回归比较；生产容量必须按实际峰值流量、磁盘、副本数、
MQTT/Redis 拓扑重复三次并保留结果。
