# Demo Runbook — day-of, from a fresh laptop restart

Assumes Docker isn't running yet and nothing is up. Pre-flight is ~3 minutes.

## 1. Pre-flight (~10 min before)

**Start Docker Desktop** — it does *not* auto-start after a reboot. Launch it from
Spotlight/Applications and wait for the menu-bar whale icon to stop animating. Then:

```bash
docker ps
```

**Free port 6379** — a leftover `redis-demo` container can grab the port the demo's
Redis needs. Remove it if present (harmless if it isn't):

```bash
docker rm -f redis-demo
```

**Go to the project and pull the latest:**

```bash
cd ~/Claude-Code/aveva-pi-redis-integration && git pull
```

## 2. Launch the stack (one command)

Builds and starts Redis Stack + the mock PI Web API + the integration service:

```bash
docker compose up --build
```

Leave this terminal visible — the audience sees `resolved 3/3 points` and
`wrote N new sample(s)` scroll by. First run takes ~30–60s (image build).

## 3. Open RedisInsight

Browse to **http://localhost:8001**. If prompted, add a database at `localhost:6379`.
Open a time-series key (`pi:ts:Sinusoid`) to show the live chart.

## 4. Demo talk track (two more terminals)

**Terminal A — live commands (the "wow" moment):** keep running the whole time so the
audience watches every `TS.ADD`, `HSET`, `PUBLISH` as it happens:

```bash
docker compose exec redis redis-cli MONITOR
```

**Terminal B — inspect the data:**

```bash
docker compose exec redis redis-cli HGETALL pi:latest:Sinusoid
```
```bash
docker compose exec redis redis-cli TS.RANGE pi:ts:Sinusoid - + COUNT 5
```
```bash
docker compose exec redis redis-cli TS.MRANGE - + FILTER source=pi
```

Auto-maintained downsampling rollup:

```bash
docker compose exec redis redis-cli TS.RANGE pi:ts:Sinusoid:avg_1m - + COUNT 5
```

Health + metrics:

```bash
curl http://localhost:9100/healthz
```
```bash
curl http://localhost:9100/metrics
```

**Gap-recovery finale** — in the `docker compose up` terminal press `Ctrl-C`, wait ~15s
("the historian kept recording; our service was down"), then bring it back:

```bash
docker compose up
```

Point out in the logs that it resumes from the checkpoint and backfills the outage —
zero data loss, zero duplicates.

## 5. Teardown (after)

```bash
docker compose down
```

---

## Tips

- **Do a full dry run this morning:** run steps 1–2, confirm data lands, then
  `docker compose down`. The first `up --build` is the slowest; later launches are seconds.
- **"port is already allocated"** on `docker compose up` → the 6379 conflict. Rerun
  `docker rm -f redis-demo` and try again.
- **Nothing in RedisInsight?** Confirm the integration container is running
  (`docker compose ps`) and that you added the DB as `localhost:6379`.
