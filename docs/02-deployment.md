# 部署与运维

## 1. 运行前置条件

- Docker 24+ 与 Docker Compose v2；
- Python 3.11+ 与 `uv`；
- Node.js 20.19+（前端构建）；
- 边缘节点需要支持 monitor mode 的 Wi-Fi 网卡；BLE 需要 BlueZ；
- 服务端默认只用于开发/受信网络，公网部署前必须完成
  `01-architecture.md` 第 10 节的安全项。

## 2. 本地/单机服务端

```bash
make install
make up
curl -s http://localhost:8000/health/ready
curl -s http://localhost:8000/metrics
```

Compose 启动以下服务：

| 服务 | 容器端口 | 宿主机端口 | 用途 |
|---|---:|---:|---|
| `timescaledb` | 5432 | 5433 | 观测/站点/告警区域 |
| `emqx` | 1883 / 18083 | 1883 / 18083 | MQTT / EMQX 控制台 |
| `rid-server` | 8000 | 8000 | REST、WebSocket、指标 |

`server/docker-entrypoint.sh` 会在 API 启动前执行所有 Alembic 迁移。迁移失败时
容器不会带着旧 schema 启动。

停止服务但保留数据库：

```bash
make down
```

删除本地数据库卷是显式破坏性操作：

```bash
make reset
```

开发/演示的默认数据库凭据写在 Compose 中，生产环境必须替换。

### 可选 API 鉴权

默认不设 token，方便本地演示。设置 `RID_API_TOKEN` 后，所有数据 REST
路由和 `/ws/observations` 都要求 Bearer token；`/health*` 与 `/metrics`
保持公开，供探针和 Prometheus 使用。

```bash
export RID_API_TOKEN="$(openssl rand -hex 24)"
# 浏览器构建时注入同一 token（注意：Vite 变量会进入 JS bundle）
cd frontend
VITE_API_TOKEN="$RID_API_TOKEN" npm run build
```

浏览器 WebSocket 不能自定义 Authorization 头，因此前端通过
`Sec-WebSocket-Protocol` 传递 `bearer.<token>`。共享 token 只适合单租户/
受信网络；公网部署仍应使用反向代理、身份网关和 TLS。

告警查询与生命周期接口使用同一鉴权：

```bash
curl -s -H "Authorization: Bearer $RID_API_TOKEN" \
  http://localhost:8000/alerts
curl -s -X POST \
  -H "Authorization: Bearer $RID_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"acknowledged_by":"operator-7","note":"已派发现场核查"}' \
  "http://localhost:8000/alerts/$ALERT_ID/ack"

curl -s -X POST \
  -H "Authorization: Bearer $RID_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"resolved_by":"operator-7","note":"现场核查完成"}' \
  "http://localhost:8000/alerts/$ALERT_ID/resolve"

curl -s -X POST \
  -H "Authorization: Bearer $RID_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reopened_by":"operator-7","note":"发现新的航迹"}' \
  "http://localhost:8000/alerts/$ALERT_ID/reopen"

curl -s -H "Authorization: Bearer $RID_API_TOKEN" \
  "http://localhost:8000/alerts/$ALERT_ID/events"

curl -s -H "Authorization: Bearer $RID_API_TOKEN" \
  "http://localhost:8000/alerts/page?limit=50"
curl -s -H "Authorization: Bearer $RID_API_TOKEN" \
  "http://localhost:8000/alerts/stats"
```

`status=active` 可以只查询未关闭告警，也支持按 `rule`、`drone_id`、`from`
和 `to` 过滤。首次确认和解决均写入操作员、时间与备注；重复确认或解决返回
同一记录。只有已解决告警可以重新打开，每次状态变化都会进入审计时间线。
`GET /alerts` 在有更早记录时返回 `X-Next-Cursor`；`/alerts/page` 返回
`{items, next_cursor}`，两类游标不能与观测游标混用。`/alerts/stats` 支持
同一组过滤参数，并返回状态、严重度、规则和最早未关闭时间统计。

### 可选告警 Webhook

同时设置 URL 和签名密钥后启用持久化通知；只设置 URL 会在启动时直接报错，
因为未签名的通知不应被当作生产配置：

```bash
export RID_NOTIFY_WEBHOOK_URL="https://ops.example.com/hooks/beaconwatch"
export RID_NOTIFY_WEBHOOK_SECRET="$(openssl rand -hex 32)"
export RID_NOTIFY_WEBHOOK_TIMEOUT_S=10
export RID_NOTIFY_MAX_ATTEMPTS=8
export RID_NOTIFY_BASE_BACKOFF_S=2
export RID_NOTIFY_MAX_BACKOFF_S=300
```

通知客户端默认不读取 `HTTP(S)_PROXY`/`ALL_PROXY`，避免全局代理把内部 Webhook
转到非预期路径。若生产出口必须经过 HTTP(S) 代理，显式设置
`RID_NOTIFY_WEBHOOK_TRUST_ENV=true`，并确认代理允许该目标。

支持的事件为 `alert.triggered`、`alert.acknowledged`、`alert.resolved` 和
`alert.reopened`。告警状态变更与 outbox 行在同一事务内提交；通知失败不会
阻断观测入库或告警 UI。投递为至少一次，重试不会改变 `delivery_id`，接收端
必须用它做幂等去重。

### 告警与通知保留

保留清理默认关闭，升级不会自动删除审计记录。需要限制历史规模时显式设置：

```bash
export RID_ALERT_RETENTION_DAYS=90
export RID_NOTIFICATION_RETENTION_DAYS=30
export RID_RETENTION_INTERVAL_S=3600
export RID_RETENTION_BATCH_SIZE=500
```

- 只删除已解决且超过期限的告警；
- 仍有 pending/delivering Webhook 的告警会被保留；
- 通知只删除 delivered/dead 行，投递中的记录不会被清理；
- 每批独立提交，清理失败会写入日志和
  `rid_retention_failures_total`，不会阻断 API 或 MQTT 入库。

接收端验证示例：

```python
import hashlib
import hmac
import json
import time

timestamp = request.headers["X-BeaconWatch-Timestamp"]
body = await request.body()
expected = "sha256=" + hmac.new(
    SECRET.encode(),
    f"{timestamp}.".encode() + body,
    hashlib.sha256,
).hexdigest()
if abs(time.time() - int(timestamp)) > 300:
    raise HTTPException(401, "stale webhook")
if not hmac.compare_digest(
    request.headers["X-BeaconWatch-Signature"], expected
):
    raise HTTPException(401, "invalid signature")

delivery_id = request.headers["X-BeaconWatch-Delivery"]
if not store.remember_once(delivery_id):
    return Response(status_code=204)
enqueue_for_operator(json.loads(body))
return Response(status_code=204)
```

验签使用原始请求字节，不要重新序列化 JSON。`2xx` 表示成功；`408`、`425`、
`429` 和 `5xx` 会重试；其它 `4xx`/`3xx` 视为永久失败并进入死信。慢处理应
先返回 `2xx` 再异步执行，避免超过通知超时。

## 3. 边缘节点

```bash
uv venv
uv pip install -e './edge[dev]'
mkdir -p /var/lib/rid-edge
```

关键环境变量：

```bash
export RID_STATION_ID=SZ-Nanshan-01
export RID_STATION_NAME="Nanshan receiver"
export RID_STATION_LAT=22.5405
export RID_STATION_LON=113.9345
export RID_STATION_ALT_M=18
export RID_WIFI_INTERFACE=wlan1mon
export RID_WIFI_CHANNEL_HOP=true
export RID_BLE_ENABLED=true
export RID_MQTT_HOST=127.0.0.1
export RID_MQTT_PORT=1883
export RID_MQTT_USE_TLS=false
# 生产环境启用 TLS 时：
# export RID_MQTT_USE_TLS=true
# export RID_MQTT_CA_CERT=/etc/ssl/certs/broker-ca.pem
# export RID_MQTT_CLIENT_CERT=/etc/rid-edge/client.crt
# export RID_MQTT_CLIENT_KEY=/etc/rid-edge/client.key
export RID_BUFFER_DB_PATH=/var/lib/rid-edge/buffer.sqlite
export RID_MAX_OBS_PER_SEC=50
export RID_RECEIVER_BACKOFF_BASE_S=1
export RID_RECEIVER_BACKOFF_MAX_S=30
```

启动：

```bash
.venv/bin/rid-edge
```

Wi-Fi 网卡需要先由系统配置成 monitor mode。网卡名、信道和 BLE 权限因硬件
与发行版而异，不属于本仓库自动配置范围。

### 断网行为

观测先写入 SQLite outbox。broker 不可达或 QoS 确认超时时，行会保留并在后续
重试；超过 `RID_BUFFER_MAX_BYTES` 后按最旧数据淘汰。站点心跳也走相同 outbox。
Wi-Fi/BLE 接收器崩溃或数据流意外结束时，监督器会重建接收器并按指数退避
重启，不会静默留下一个已经退出的采集任务。

## 4. 健康检查与监控

```bash
curl -f http://localhost:8000/health/live
curl -f http://localhost:8000/health/ready
curl -s http://localhost:8000/metrics
```

- `live` 只表示进程可响应；
- `ready` 会执行数据库 `SELECT 1`；
- Compose healthcheck 使用 `ready`；
- 指标是 Prometheus text 格式，但仓库未附带 Prometheus/Grafana 配置。
- 每个 HTTP 响应带 `X-Request-ID`，并记录请求数、耗时、状态码和鉴权失败数；
- `/observations` 达到一页上限时返回 `X-Next-Cursor`，也可使用
  `/observations/page` 获取 `{items, next_cursor}`；不要再用深 offset 翻页。
- `/alerts` 达到一页上限时返回 `X-Next-Cursor`，也可使用
  `/alerts/page`；`/alerts/stats` 提供服务端聚合计数；
- `rid_retention_alerts_deleted_total`、
  `rid_retention_notifications_deleted_total`、
  `rid_retention_failures_total` 和最近成功时间用于监控清理任务。

## 5. 端到端验证

```bash
make e2e
```

脚本读取官方 OpenDroneID encoder 生成的 Beacon/Message Pack，经过真实
receiver、pipeline、EMQX、数据库和 FastAPI，最后通过 HTTP 检查协议、RSSI、
速度、操作员位置、告警生命周期，并用本地签名接收端验证触发、确认、解决和
重开通知的 HMAC 与投递顺序；同时验证告警分页形状和最终状态统计。

## 6. 数据库升级与备份

升级镜像：

```bash
docker build -t beaconwatch-server:0.7.0 ./server
docker compose -f server/docker-compose.yml up -d --build rid-server
```

容器入口会自动执行 `alembic upgrade head`。升级前仍应先备份：

```bash
docker exec rid-timescaledb pg_dump -U rid -d rid -Fc \
  > beaconwatch-$(date +%Y%m%d-%H%M%S).dump
```

恢复：

```bash
cat beaconwatch-YYYYMMDD-HHMMSS.dump | \
  docker exec -i rid-timescaledb pg_restore -U rid -d rid --clean --if-exists
```

恢复会覆盖目标数据库，必须在维护窗口执行并先做二次备份。

## 7. 生产部署检查表

- [ ] 替换数据库默认密码，使用 secret manager；
- [ ] 设置 `RID_API_TOKEN`，并替换为具备授权/审计/轮换能力的身份方案；
- [ ] 使用 HTTPS/WSS 反向代理；
- [ ] 为 MQTT 配置 TLS（`RID_MQTT_CA_CERT` 等）、独立站点账号和 ACL；
- [ ] 限制 5433/1883/18083/8000 的暴露范围；
- [ ] 为多副本部署引入共享 WebSocket 总线；
- [ ] 配置数据库备份、恢复演练和磁盘容量告警；
- [ ] 启用并验证 Webhook 签名、重试/死信监控、告警保留期限和清理策略；
- [ ] 用真实抓包集和现场无人机完成验收测试。

## 8. 故障排查

```bash
docker compose -f server/docker-compose.yml ps
docker compose -f server/docker-compose.yml logs --tail=200 rid-server
docker compose -f server/docker-compose.yml logs --tail=200 timescaledb
```

- `/health/ready` 返回 503：检查数据库启动、凭据和迁移日志；
- `rid_mqtt_reconnects_total` 持续增长：检查 broker 地址、网络和 MQTT 凭据；
- 边缘端有流量但数据库无新行：检查 topic 年月版本、站点 ID 和 outbox 日志；
- 地图有列表但无位置：检查观测是否只有 Basic ID、尚无 Location 消息。
- `rid_notification_outbox_dead` 增长：检查 Webhook 认证、URL 和返回码；
  `pending`/`failures` 持续增长时检查网络、接收端耗时和 `last_error` 日志。
