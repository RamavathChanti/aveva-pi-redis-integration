"""Write AVEVA PI samples into Redis.

Data model
----------
* RedisTimeSeries key   {prefix}:ts:{tag}
    - compressed full-resolution history
    - labels support multi-series filtering and grouping
* Rollup series         {prefix}:ts:{tag}:{rollup}
    - automatically maintained by TS.CREATERULE
* Snapshot hash         {prefix}:latest:{tag}
    - current value and asset metadata for fast dashboards
* Pub/Sub channel       {prefix}:updates
    - JSON notification for live consumers
"""
from __future__ import annotations

import json
import logging

from redis import asyncio as aioredis
from redis.exceptions import ResponseError

from pi_client import PIValue

log = logging.getLogger("redis_writer")


def tag_from_path(path: str) -> str:
    """\\\\PISRV01\\PlantA_Generator01_Temperature -> tag name."""
    return path.replace("/", "\\").split("\\")[-1] or path


# Backwards-compatible alias.
_tag_from_path = tag_from_path


def metadata_from_tag(tag: str) -> dict[str, str]:
    """Derive demo labels from ``Site_Asset_Metric`` tag names.

    Real deployments would normally source these labels from PI Asset Framework
    or a customer-owned metadata mapping rather than relying on naming rules.
    """
    parts = [part for part in tag.split("_") if part]
    if len(parts) >= 3:
        return {
            "site": parts[0],
            "asset": parts[1],
            "metric": "_".join(parts[2:]).lower(),
        }
    return {"site": "Demo", "asset": tag, "metric": "value"}


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
        """Human label for the rollup, e.g. avg_1m for a 60000 ms bucket."""
        value = self._ds_bucket_ms
        unit = "ms"
        for size, suffix in ((3_600_000, "h"), (60_000, "m"), (1_000, "s")):
            if value % size == 0:
                value, unit = value // size, suffix
                break
        return f"{self._ds_agg}_{value}{unit}"

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
        raw = await self._redis.get(self._checkpoint_key(tag))
        return int(raw) if raw is not None else None

    async def set_checkpoint(self, tag: str, ts_ms: int) -> None:
        await self._redis.set(self._checkpoint_key(tag), ts_ms)

    @staticmethod
    def _raw_labels(tag: str, path: str) -> list[str]:
        metadata = metadata_from_tag(tag)
        return [
            "source", "pi",
            "kind", "raw",
            "tag", tag,
            "path", path,
            "site", metadata["site"],
            "asset", metadata["asset"],
            "metric", metadata["metric"],
        ]

    def _rollup_labels(self, tag: str, path: str) -> list[str]:
        metadata = metadata_from_tag(tag)
        return [
            "source", "pi",
            "kind", "rollup",
            "tag", tag,
            "path", path,
            "site", metadata["site"],
            "asset", metadata["asset"],
            "metric", metadata["metric"],
            "rollup", self._rollup_label(),
        ]

    async def _ensure_series(self, tag: str, path: str) -> None:
        if tag in self._ensured:
            return

        key = self._ts_key(tag)
        labels = self._raw_labels(tag, path)
        try:
            await self._redis.execute_command(
                "TS.CREATE", key,
                "RETENTION", self._retention_ms,
                "ENCODING", "COMPRESSED",
                "DUPLICATE_POLICY", "LAST",
                "LABELS", *labels,
            )
            log.info("created timeseries %s", key)
        except ResponseError as exc:
            if "already exists" not in str(exc).lower():
                raise
            # Keep labels aligned when an older demo database is reused.
            await self._redis.execute_command("TS.ALTER", key, "LABELS", *labels)

        if self._ds_enabled:
            await self._ensure_rollup(tag, path, key)
        self._ensured.add(tag)

    async def _ensure_rollup(self, tag: str, path: str, source_key: str) -> None:
        """Create and maintain a downsampled destination series."""
        destination_key = self._rollup_key(tag)
        labels = self._rollup_labels(tag, path)
        try:
            await self._redis.execute_command(
                "TS.CREATE", destination_key,
                "RETENTION", self._retention_ms,
                "ENCODING", "COMPRESSED",
                "LABELS", *labels,
            )
        except ResponseError as exc:
            if "already exists" not in str(exc).lower():
                raise
            await self._redis.execute_command(
                "TS.ALTER", destination_key, "LABELS", *labels
            )

        try:
            await self._redis.execute_command(
                "TS.CREATERULE", source_key, destination_key,
                "AGGREGATION", self._ds_agg, self._ds_bucket_ms,
            )
            log.info(
                "created rollup %s (%s / %dms)",
                destination_key,
                self._ds_agg,
                self._ds_bucket_ms,
            )
        except ResponseError as exc:
            if "already" not in str(exc).lower():
                raise

    async def write(self, sample: PIValue, ts_ms: int) -> None:
        """Persist one numeric PI sample and publish a live update."""
        if sample.value is None:
            log.debug("skipping non-numeric sample for %s", sample.path)
            return

        tag = tag_from_path(sample.path)
        metadata = metadata_from_tag(tag)
        await self._ensure_series(tag, sample.path)

        pipe = self._redis.pipeline(transaction=False)
        pipe.execute_command(
            "TS.ADD", self._ts_key(tag), ts_ms, sample.value,
            "ON_DUPLICATE", "LAST",
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
                **metadata,
            },
        )
        pipe.publish(
            f"{self._prefix}:updates",
            json.dumps(
                {
                    "tag": tag,
                    "path": sample.path,
                    "value": sample.value,
                    "units": sample.units or "",
                    "ts": sample.timestamp,
                    "ts_ms": ts_ms,
                    "good": sample.good,
                    **metadata,
                }
            ),
        )
        await pipe.execute()
        log.debug("wrote %s=%s @ %s", tag, sample.value, ts_ms)
