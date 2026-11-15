# BeaconWatch production reference

This directory contains a deliberately small but production-shaped
deployment: a TLS reverse proxy, multiple API replicas, Redis fan-out,
MQTT shared subscriptions, a one-shot migration job, and Docker secrets.
It is a reference topology, not a promise that every network or compliance
requirement is already satisfied.

## What is isolated

- `proxy` is the only service with public HTTP/HTTPS ports.
- PostgreSQL and Redis have no host ports.
- MQTT binds to `127.0.0.1` by default so only a local edge process or a
  private VPN/overlay can reach it.
- API replicas have no host ports and are reachable only through the proxy.
- The database password and Redis URL are read from mounted secret files.
- The browser receives a least-privilege viewer key at container startup;
  the key is not baked into the frontend image or `docker history`.

## Prepare secrets and certificates

From the repository root:

```bash
cp deploy/production/.env.example deploy/production/.env
mkdir -p deploy/production/secrets deploy/production/certs
cd deploy/production
umask 077

openssl rand -hex 32 > secrets/db_password
openssl rand -hex 32 > secrets/redis_password
printf 'redis://:%s@redis:6379/0\n' \
  "$(cat secrets/redis_password)" > secrets/redis_url
openssl rand -hex 32 > secrets/webhook_secret
cp secrets.example/auth_users.json secrets/auth_users.json
```

Generate named API keys:

```bash
cd ../..
python scripts/generate_api_key.py \
  --subject wall-display \
  --name "Wall display" \
  --role viewer
```

Save the printed `API_KEY` as `deploy/production/secrets/viewer_api_token`
and add its `KEY_ENTRY` object to the `keys` array in
`deploy/production/secrets/auth_users.json`. Add operator and admin keys the
same way. The viewer key must exist in `auth_users.json`; the proxy injects it
into the dashboard at startup.

Put the private key and certificate chain here:

```text
deploy/production/certs/privkey.pem
deploy/production/certs/fullchain.pem
```

For an internal smoke test only, a short-lived self-signed pair is enough:

```bash
openssl req -x509 -nodes -days 1 -newkey rsa:2048 \
  -keyout deploy/production/certs/privkey.pem \
  -out deploy/production/certs/fullchain.pem \
  -subj /CN=beaconwatch.local
```

## Start

```bash
cd deploy/production
docker compose --env-file .env up -d --build
docker compose ps
curl -k https://127.0.0.1/api/health/ready
docker compose logs --tail=100 api
```

The `migrate` service exits after `alembic upgrade head`; only after that
does Compose start the API replicas. API containers run with
`RID_SKIP_MIGRATIONS=1` so two replicas cannot race the migration.

## Update and rollback

Before an upgrade:

```bash
docker compose exec -T db pg_dump \
  -U "${BW_DB_USER:-beaconwatch}" \
  -d "${BW_DB_NAME:-beaconwatch}" -Fc \
  > "beaconwatch-$(date +%Y%m%d-%H%M%S).dump"
```

Set `BW_SERVER_IMAGE` and `BW_PROXY_IMAGE` to immutable tags, rebuild, then:

```bash
docker compose --env-file .env up -d --build
```

Rollback the image tags and run the same command. Do not roll back a
destructive database migration without first restoring the matching backup.

## Operational boundaries

- The bundled EMQX listener is anonymous and plaintext. It is bound to
  loopback for that reason. For remote stations, put MQTT behind a VPN or
  configure EMQX TLS, per-station credentials and ACLs before broadening the
  bind address.
- The Redis listener requires a password but uses the internal Docker network
  without TLS. Use a controlled Redis deployment with ACL/TLS and high
  availability if Redis crosses a trust boundary.
- Rotate the viewer key by updating `auth_users.json` and restarting the
  proxy so `config.js` is regenerated. Viewer keys are visible to every
  browser that loads the dashboard; never use an operator/admin key there.
- Keep `api` at two or more replicas behind the provided proxy. All replicas
  must share the MQTT shared group, Redis channel and database.
- The reference does not bundle Prometheus/Grafana, a certificate renewal
  controller, an OIDC provider or an off-host backup store. See
  `../../docs/04-operations.md`.

## Validation

```bash
docker compose --env-file .env \
  -f compose.yaml config --quiet
```

Run load and fault drills only against a disposable project with dedicated
volumes and ports. Never run the destructive resilience drill against this
production project.
