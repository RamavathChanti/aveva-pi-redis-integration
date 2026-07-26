"""Writes PI samples into Redis.

Data model
----------
* RedisTimeSeries key   {prefix}:ts:{tag}   -> full history (add(ts, value))
    - labeled with source=pi, tag, path so you can MRANGE by label
    - configurable retention
* Snapshot hash         {prefix}:latest:{tag} -> {value, ts, good, units, path}
    - cheap "current value" lookup without scanning the series
* Pub/Sub channel       {prefix}:updates    -> JSON of each new sample
    - lets downstream consumers react in real time
"""
from __future__ import annotations

import json
import logging

from redis import asyncio as aioredis
from redis.exceptions import ResponseError

from pi_client import PIValue

log = logging.getLogger("redis_writer")


def tag_from_path(path: str) -> str:
    """\\\\PISRV01\\Sinusoid -> Sinusoid (last path segment)."""
    return path.replace("/", "\\").split("\\")[-1] or path


# Backwards-compatible alias.
_tag_from_path = tag_from_path


class RedisWriter:
    def __init__(
        self,
        url: str,
        *,
        key_prefix: str = "pi",
        retention_ms: int = 0,
        downsample_enabled: bool = True,
        downsample_bucket_ms: int = 60_000,
        downsample_aggregation: str = "avg",
    ) -> None:
        self._redis = aioredis.from_url(url, decode_responses=True)
        self._prefix = key_prefix
        self._retention_ms = retention_ms
        self._ds_enabled = downsample_enabled
        self._ds_bucket_ms = downsample_bucket_ms
        self._ds_agg = downsample_aggregation.lower()
        self._ensured: set[str] = set()

    def _rollup_label(self) -> str:
        """Human label for the rollup, e.g. avg_1m for a 60000 ms avg bucket."""
        ms = self._ds_bucket_ms
        unit = "ms"
        for size, u in ((3_600_000, "h"), (60_000, "m"), (1_000, "s")):
            if ms % size == 0:
                ms, unit = ms // size, u
                break
        return f"{self._ds_agg}_{ms}{unit}"

    async def aclose(self) -> None:
        await self._redis.aclose()

    async def ping(self) -> None:
        await self._redis.ping()

    def _ts_key(self, tag: str) -> str:
        return f"{self._prefix}:ts:{tag}"

    def _rollup_key(self, tag: str) -> str:
        return f"{self._prefix}:ts:{tag}:{self._rollup_label()}"

    def _latest_key(self, tag: str) -> str:
        return f"{self._prefix}:latest:{tag}"

    def _checkpoint_key(self, tag: str) -> str:
        return f"{self._prefix}:checkpoint:{tag}"

    async def get_checkpoint(self, tag: str) -> int | None:
        """Last-written sample time (epoch ms) for a tag, or None if never seen."""
        raw = await self._redis.get(self._checkpoint_key(tag))
        return int(raw) if raw is not None else None

    async def set_checkpoint(self, tag: str, ts_ms: int) -> None:
        await self._redis.set(self._checkpoint_key(tag), ts_ms)

    async def _ensure_series(self, tag: str, path: str) -> None:
        if tag in self._ensured:
            return
        key = self._ts_key(tag)
        try:
            await self._redis.execute_command(
                "TS.CREATE", key,
                "RETENTION", self._retention_ms,
                "DUPLICATE_POLICY", "LAST",
                "LABELS", "source", "pi", "tag", tag, "path", path,
            )
            log.info("created timeseries %s", key)
        except ResponseError as e:
            if "already exists" not in str(e).lower():
                raise

        if self._ds_enabled:
            await self._ensure_rollup(tag, path, key)
        self._ensured.add(tag)

    async def _ensure_rollup(self, tag: str, path: str, src_key: str) -> None:
        """Create a downsampled series + TS.CREATERULE compaction on the source.

        Every `bucket_ms` window of raw samples is aggregated (e.g. avg) into the
        rollup series automatically by RedisTimeSeries — cheap long-range queries.
        """
        rollup = self._rollup_label()
        dst_key = self._rollup_key(tag)
        try:
            await self._redis.execute_command(
                "TS.CREATE", dst_key,
                "RETENTION", self._retention_ms,
                "LABELS", "source", "pi", "tag", tag, "path", path, "rollup", rollup,
            )
        except ResponseError as e:
            if "already exists" not in str(e).lower():
                raise
        try:
            await self._redis.execute_command(
                "TS.CREATERULE", src_key, dst_key,
                "AGGREGATION", self._ds_agg, self._ds_bucket_ms,
            )
            log.info("created rollup %s (%s / %dms)", dst_key, self._ds_agg, self._ds_bucket_ms)
        except ResponseError as e:
            # Rule already exists between these keys — fine on restart.
            if "already" not in str(e).lower():
                raise

    async def write(self, sample: PIValue, ts_ms: int) -> None:
        """Persist one sample. ts_ms is the sample time in epoch milliseconds."""
        if sample.value is None:
            log.debug("skipping non-numeric sample for %s", sample.path)
            return

        tag = _tag_from_path(sample.path)
        await self._ensure_series(tag, sample.path)

        pipe = self._redis.pipeline(transaction=False)
        pipe.execute_command(
            "TS.ADD", self._ts_key(tag), ts_ms, sample.value, "ON_DUPLICATE", "LAST"
        )
        pipe.hset(
            self._latest_key(tag),
            mapping={
                "value": sample.value,
                "ts": sample.timestamp or "",
                "ts_ms": ts_ms,
                "good": int(sample.good),
                "units": sample.units or "",
                "path": sample.path,
            },
        )
        pipe.publish(
            f"{self._prefix}:updates",
            json.dumps(
                {
                    "tag": tag,
                    "path": sample.path,
                    "value": sample.value,
                    "ts": sample.timestamp,
                    "ts_ms": ts_ms,
                    "good": sample.good,
                }
            ),
        )
        await pipe.execute()
        log.debug("wrote %s=%s @ %s", tag, sample.value, ts_ms)
