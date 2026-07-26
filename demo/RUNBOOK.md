# Customer Demo Runbook: AVEVA PI + Redis Web Application

This version of the demo is designed to be presented entirely through the custom web application rather than RedisInsight or a sequence of terminal commands.

## 1. Pre-flight

Start Docker Desktop and confirm it is ready:

```bash
docker ps
```

Go to the project directory:

```bash
cd ~/aveva-pi-redis-integration
```

If port 6379 is already in use, stop the conflicting Redis container before continuing.

## 2. Start the stack

```bash
docker compose up --build
```

Wait until the integration logs show that all configured PI points are resolved and new samples are being written.

Open the customer application:

```text
http://localhost:3000
```

The page should show a green Redis connection indicator and eight live PI measurements.

## 3. Suggested customer talk track

### A. Architecture and positioning

Use the architecture panel at the top of the page:

1. AVEVA PI remains the authoritative historian.
2. The integration service reads recorded values in batches through PI Web API.
3. Redis provides the low-latency operational layer.
4. Dashboards, alerting and AI applications read Redis rather than adding load to PI.

### B. Current operational state

Point to the live cards.

Explain that one `TS.MGET` label query retrieves the latest value across all matching PI time series. The application does not maintain a hard-coded list of Redis keys.

### C. Single-series query flexibility

In **Explore one time series**:

1. Select a temperature tag.
2. Set the time window to 15 minutes.
3. Switch between raw samples, average, minimum and maximum.
4. Change the bucket from 10 seconds to one minute or five minutes.
5. Point out that the chart changes while the exact Redis command appears underneath.

Enable **Filter readings by accepted value range** to demonstrate `FILTER_BY_VALUE`. Explain that filtering happens before aggregation.

### D. Multi-series analytics

In **Filter, group and reduce across the fleet**:

1. Select `Temperature`.
2. Group by `Site`.
3. Use `Average` as the reducer.
4. Change the reducer to `Maximum`.
5. Select `Power` and use `Sum` to demonstrate total output by site.

Explain that `TS.MRANGE` uses labels to select many series, then `GROUPBY` and `REDUCE` perform the cross-series calculation inside Redis.

### E. Automatic rollups and compression

Use the **Automatic downsampling** section:

1. Compare the raw sample count with the one-minute rollup count.
2. Show the `TS.CREATERULE` command.
3. Explain that Redis maintains the rollup automatically as new samples arrive.
4. Point out the compressed chunk type and memory values returned by `TS.INFO`.

Avoid promising a fixed compression percentage for the customer's data. Compression depends on timestamp regularity, value patterns, chunk size and the number of samples per series.

### F. Live delivery

Point to the recent-update table. It is updated through Redis Pub/Sub and server-sent events.

Explain that other consumers could independently subscribe for alerting, workflow automation or operational AI use cases.

## 4. Gap-recovery finale

Leave the web page open and stop only the integration service:

```bash
docker compose stop integration
```

Wait 20 to 30 seconds. The mock historian continues to generate recorded values while the integration is down.

Restart it:

```bash
docker compose start integration
docker compose logs -f integration
```

Explain that the service resumes from each Redis checkpoint and backfills the gap. The live dashboard catches up without duplicate samples.

## 5. Teardown

```bash
docker compose down
```

Use this for a completely clean reset:

```bash
docker compose down -v
```
