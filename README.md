# AVEVA PI Web API to Redis Integration Demo

A customer-facing proof of concept that reads recorded values from the **AVEVA PI Web API**, writes them into **Redis TimeSeries**, and serves the resulting operational data through a purpose-built web application.

The browser demo replaces RedisInsight and terminal-driven querying. It lets customers explore live values, raw history, query-time aggregations, value filters, label-based multi-series queries, cross-series grouping, automatic rollups and Redis Pub/Sub from one interface.

## Architecture

```text
AVEVA PI / mock PI Web API
          |
          | batch recorded values
          v
Async integration service
  - per-tag checkpoints
  - gap recovery
  - duplicate-safe writes
          |
          v
Redis
  - TimeSeries: compressed history
  - Hashes: latest state
  - Pub/Sub: live updates
  - TS.CREATERULE: 1-minute rollups
          |
          v
Customer web application (http://localhost:3000)
```

AVEVA PI remains the long-term system of record. Redis acts as the low-latency serving and real-time layer for applications, dashboards, alerting and AI use cases.

## What the web application demonstrates

### Current operational state

The landing page uses a label query to retrieve the latest value across all PI series:

```text
TS.MGET WITHLABELS FILTER source=pi kind=raw
```

### Flexible single-series reads

Customers can select a PI tag, time range, bucket size and aggregation. The application generates commands such as:

```text
TS.RANGE pi:ts:PlantA_Generator01_Temperature <from> <to>
  COUNT 1000
  AGGREGATION avg 60000
```

Available read-time aggregations include average, minimum, maximum, sum, range, count, first, last and standard deviation.

The UI can also exclude out-of-range sensor readings before aggregation:

```text
TS.RANGE pi:ts:PlantA_Generator01_Temperature <from> <to>
  FILTER_BY_VALUE 0 100
  COUNT 1000
  AGGREGATION avg 60000
```

### Multi-series filtering, grouping and reduction

The fleet view uses labels such as `site`, `asset` and `metric` to query many series without knowing their key names:

```text
TS.MRANGE <from> <to>
  WITHLABELS
  AGGREGATION avg 60000
  FILTER source=pi kind=raw metric=temperature
  GROUPBY site REDUCE avg
```

This can answer questions such as:

- Average generator temperature by site
- Total power by site
- Maximum vibration by asset

### Automatic downsampling

Each raw series has an automatically maintained one-minute average rollup:

```text
TS.CREATERULE <raw-series> <rollup-series> AGGREGATION avg 60000
```

The web application compares the raw and rollup sample counts and shows how a dashboard can query the smaller, pre-computed series for longer time windows.

### Live updates

Every newly ingested sample is published on:

```text
pi:updates
```

The dashboard subscribes through a server-sent event endpoint and updates live without polling Redis from the browser.

## Data model

For a PI tag named `PlantA_Generator01_Temperature`:

| Redis object | Type | Purpose |
|---|---|---|
| `pi:ts:PlantA_Generator01_Temperature` | TimeSeries | Compressed raw history |
| `pi:ts:PlantA_Generator01_Temperature:avg_1m` | TimeSeries | Automatic one-minute rollup |
| `pi:latest:PlantA_Generator01_Temperature` | Hash | Latest value, quality, units and metadata |
| `pi:checkpoint:PlantA_Generator01_Temperature` | String | Last ingested timestamp for gap recovery |
| `pi:updates` | Pub/Sub | Live update notifications |

Raw and rollup time series are labelled with:

```text
source=pi
kind=raw | rollup
tag=<PI tag>
site=<site>
asset=<asset>
metric=<metric>
path=<PI point path>
```

For the local demo, labels are derived from the `Site_Asset_Metric` tag naming convention. In a real deployment, this metadata would normally come from PI Asset Framework or an enterprise metadata mapping.

## Quick start

### Prerequisites

- Docker Desktop
- Git

### Start the full demo

```bash
git clone https://github.com/amitmknit/aveva-pi-redis-integration.git
cd aveva-pi-redis-integration
docker compose up --build
```

Open:

```text
http://localhost:3000
```

The stack starts:

| Service | Port | Purpose |
|---|---:|---|
| Redis Stack Server | 6379 | Redis with TimeSeries support |
| Mock PI Web API | 8080 | Reproducible local historian data |
| Integration health and metrics | 9100 | `/healthz` and `/metrics` |
| Customer web application | 3000 | Demo dashboard and query explorer |

### Stop the demo

```bash
docker compose down
```

To remove the Redis data as well:

```bash
docker compose down -v
```

## Gap-recovery demonstration

Stop only the integration service while leaving the PI mock and Redis running:

```bash
docker compose stop integration
```

Wait 20 to 30 seconds, then restart it:

```bash
docker compose start integration
docker compose logs -f integration
```

The service resumes from each tag's checkpoint, backfills the missing recorded values and uses `TS.ADD ... ON_DUPLICATE LAST` to avoid duplicate data.

## Point the POC at a real PI Web API

Update the environment values in `docker-compose.yml` or use `.env` when running locally:

```ini
PI_BASE_URL=https://your-pi-server/piwebapi
PI_AUTH_MODE=basic
PI_USERNAME=svc_pi
PI_PASSWORD=your-password
PI_VERIFY_SSL=true
PI_POINTS=\\PISRV01\Tag1,\\PISRV01\Tag2
```

The POC currently supports no authentication or Basic authentication. Production PI environments commonly use Kerberos/SPNEGO, which should be added before a production deployment.

## Why Redis TimeSeries

- **Fast application reads:** dashboards and APIs query Redis instead of repeatedly loading the PI historian.
- **Compressed time-series chunks:** the series are created with `ENCODING COMPRESSED` to reduce memory use for typical telemetry.
- **Native query-time aggregation:** applications receive dashboard-ready buckets instead of transferring every raw sample.
- **Secondary indexes through labels:** `TS.MGET`, `TS.MRANGE` and `TS.QUERYINDEX` find series by business metadata.
- **Cross-series analytics:** `GROUPBY` and `REDUCE` aggregate matching assets inside Redis.
- **Automatic rollups:** `TS.CREATERULE` maintains summary series as new samples arrive.
- **Real-time delivery:** Pub/Sub notifies the application of new values immediately.
- **Protection for OT systems:** application traffic scales independently from the historian.

## Repository layout

| Path | Role |
|---|---|
| `src/main.py` | Checkpoint-based ingestion loop |
| `src/pi_client.py` | PI Web API client |
| `src/redis_writer.py` | TimeSeries, snapshot, rollup and Pub/Sub writes |
| `src/mock_pi_server.py` | Local PI Web API simulator |
| `src/dashboard.py` | Read-only customer web API and SSE bridge |
| `src/web/` | HTML, CSS and JavaScript dashboard |
| `demo/RUNBOOK.md` | Customer-demo talk track |

## POC to production considerations

- Add Kerberos/SPNEGO for PI Web API authentication.
- Enable Redis TLS, ACLs and secrets management.
- Source labels from PI Asset Framework or governed enterprise metadata.
- Add user authentication and authorisation to the dashboard.
- Partition tags across workers for higher ingestion scale.
- Add structured logs, tracing and production monitoring.
- Validate retention, rollup and memory sizing against the customer's actual tag count and sampling frequency.
