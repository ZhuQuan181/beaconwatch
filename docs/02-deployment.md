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
export RID_BUFFER_DB_PATH=/var/lib/rid-edge/buffer.sqlite
export RID_MAX_OBS_PER_SEC=50
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

## 5. 端到端验证

```bash
make e2e
```

脚本读取官方 OpenDroneID encoder 生成的 Beacon/Message Pack，经过真实
receiver、pipeline、EMQX、数据库和 FastAPI，最后通过 HTTP 检查协议、RSSI、
速度、操作员位置与未识别无人机列表。

## 6. 数据库升级与备份

升级镜像：

```bash
docker build -t beaconwatch-server:0.2.0 ./server
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
- [ ] 为 API/WS 增加认证、授权和审计；
- [ ] 使用 HTTPS/WSS 反向代理；
- [ ] 为 MQTT 配置 TLS、独立站点账号和 ACL；
- [ ] 限制 5433/1883/18083/8000 的暴露范围；
- [ ] 为多副本部署引入共享 WebSocket 总线；
- [ ] 配置数据库备份、恢复演练和磁盘容量告警；
- [ ] 将告警从进程内 ring buffer 迁移到持久化存储；
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
