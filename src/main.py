"""Integration service entrypoint.

Loop (checkpoint-based backfill):
  1. resolve each configured PI point path -> WebId (once, cached)
  2. every POLL_INTERVAL_SECONDS, batch-read *recorded* values for all tags
     since each tag's last checkpoint (one streamset request for all tags)
  3. write new samples to Redis (timeseries + snapshot + pub/sub) and advance
     each tag's checkpoint

This recovers gaps after downtime: on restart it resumes from the last
checkpoint instead of only reading the current value.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from datetime import datetime, timedelta, timezone

from config import settings
from metrics import metrics, serve_health
from pi_client import PIWebAPIClient
from redis_writer import RedisWriter, tag_from_path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s %(message)s",
)
log = logging.getLogger("main")


def _iso_to_ms(ts: str | None) -> int:
    """Convert a PI ISO-8601 timestamp to epoch milliseconds (UTC)."""
    if not ts:
        return int(datetime.now(timezone.utc).timestamp() * 1000)
    cleaned = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _ms_to_iso(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


async def resolve_all(pi: PIWebAPIClient, paths: list[str]) -> dict[str, str]:
    """Map each WebId -> point path, tolerating individual failures."""
    resolved: dict[str, str] = {}
    for path in paths:
        try:
            web_id = await pi.resolve_point(path)
            resolved[web_id] = path
        except Exception as e:  # noqa: BLE001 - POC: log and skip bad tags
            log.error("could not resolve %s: %s", path, e)
    log.info("resolved %d/%d points", len(resolved), len(paths))
    return resolved


async def _window_start_ms(writer: RedisWriter, web_ids: dict[str, str]) -> int:
    """Earliest checkpoint across tags, or the seed window on first run."""
    checkpoints = []
    for path in web_ids.values():
        cp = await writer.get_checkpoint(tag_from_path(path))
        if cp is not None:
            checkpoints.append(cp)
    if checkpoints:
        return min(checkpoints) + 1  # exclusive of the last sample we wrote
    seed = datetime.now(timezone.utc) - timedelta(minutes=settings.backfill_start_minutes)
    return int(seed.timestamp() * 1000)


async def poll_once(pi: PIWebAPIClient, writer: RedisWriter, web_ids: dict[str, str]) -> int:
    """One backfill cycle. Returns the number of samples written."""
    start_ms = await _window_start_ms(writer, web_ids)
    batches = await pi.get_streamset_recorded(
        web_ids,
        start_time=_ms_to_iso(start_ms),
        end_time="*",
        max_count=settings.recorded_max_count,
    )

    written = 0
    for web_id, samples in batches.items():
        path = web_ids.get(web_id, "")
        tag = tag_from_path(path)
        cp = await writer.get_checkpoint(tag)
        max_ts: int | None = None
        for sample in samples:
            ts_ms = _iso_to_ms(sample.timestamp)
            if cp is not None and ts_ms <= cp:
                continue  # already ingested
            await writer.write(sample, ts_ms)
            written += 1
            max_ts = ts_ms if max_ts is None else max(max_ts, ts_ms)
        if max_ts is not None:
            await writer.set_checkpoint(tag, max_ts)
    return written


async def main() -> None:
    if not settings.points:
        log.error("No PI_POINTS configured. Set PI_POINTS in your .env.")
        return

    metrics.points_configured = len(settings.points)

    pi = PIWebAPIClient(
        settings.pi_base_url,
        auth_mode=settings.pi_auth_mode,
        username=settings.pi_username,
        password=settings.pi_password,
        verify_ssl=settings.pi_verify_ssl,
    )
    writer = RedisWriter(
        settings.redis_url,
        key_prefix=settings.redis_key_prefix,
        retention_ms=settings.redis_ts_retention_ms,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    health_server, health_task = await serve_health(settings.health_port)
    log.info("health/metrics on :%d (/healthz, /metrics)", settings.health_port)

    try:
        await writer.ping()
        log.info("connected to Redis at %s", settings.redis_url)

        web_ids = await resolve_all(pi, settings.points)
        metrics.points_resolved = len(web_ids)
        metrics.started = True
        if not web_ids:
            log.error("No points resolved; nothing to do.")
            return

        log.info(
            "backfilling %d point(s) every %ss (seed window %dm)",
            len(web_ids),
            settings.poll_interval_seconds,
            settings.backfill_start_minutes,
        )
        while not stop.is_set():
            try:
                n = await poll_once(pi, writer, web_ids)
                metrics.samples_written_total += n
                metrics.last_poll_ok = True
                if n:
                    log.info("wrote %d new sample(s)", n)
            except Exception as e:  # noqa: BLE001
                metrics.poll_errors_total += 1
                metrics.last_poll_ok = False
                log.error("poll cycle failed: %s", e)
            finally:
                metrics.polls_total += 1
                metrics.last_poll_unixtime = time.time()

            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass
    finally:
        log.info("shutting down")
        health_server.should_exit = True
        await health_task
        await pi.aclose()
        await writer.aclose()


if __name__ == "__main__":
    asyncio.run(main())
