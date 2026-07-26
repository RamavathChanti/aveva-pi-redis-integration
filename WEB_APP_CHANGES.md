# Custom web application update

This update replaces the RedisInsight-based customer demo with a custom dashboard at `http://localhost:3000`.

## Added

- FastAPI read-only dashboard service (`src/dashboard.py`)
- Responsive browser UI (`src/web/`)
- Live latest-value cards
- Raw and aggregated `TS.RANGE` query explorer
- `FILTER_BY_VALUE` controls
- Label-based `TS.MRANGE` queries
- `GROUPBY` and `REDUCE` fleet analytics
- Automatic rollup and `TS.INFO` comparison
- Redis Pub/Sub to server-sent event bridge
- Realistic multi-site generator tags in the mock PI service
- Customer-facing runbook

## Changed

- Docker Compose now exposes the customer application on port 3000.
- Redis uses `redis-stack-server`; RedisInsight is no longer required for the demo.
- Raw and rollup series include `kind`, `site`, `asset` and `metric` labels.
- Time series are explicitly created with compressed encoding.
