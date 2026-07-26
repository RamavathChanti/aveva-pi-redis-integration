"""Customer-facing web application for the AVEVA PI -> Redis demo.

The browser never connects directly to Redis. This FastAPI service exposes a
small read-only API that demonstrates RedisTimeSeries queries and returns the
exact Redis command used for each visualisation.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from redis import asyncio as aioredis
from redis.exceptions import RedisError, ResponseError

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
KEY_PREFIX = os.getenv("REDIS_KEY_PREFIX", "pi")
STATIC_DIR = Path(__file__).resolve().parent / "web"
TAG_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")

RANGE_AGGREGATORS = {
    "raw",
    "avg",
    "min",
    "max",
    "sum",
    "count",
    "first",
    "last",
    "range",
    "std.p",
    "std.s",
}
REDUCERS = {"avg", "min", "max", "sum", "count", "range", "std.p", "std.s"}
GROUP_LABELS = {"site", "asset", "metric"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    app.state.redis = redis
    try:
        yield
    finally:
        await redis.aclose()


app = FastAPI(
    title="AVEVA PI + Redis TimeSeries Demo",
    version="2.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _redis(request: Request):
    return request.app.state.redis


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
        return None if math.isnan(number) else number
    except (TypeError, ValueError):
        return None


def _labels(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    labels: dict[str, str] = {}
    if isinstance(raw, (list, tuple)):
        for pair in raw:
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                labels[str(pair[0])] = str(pair[1])
    return labels


def _samples(raw: Any) -> list[dict[str, float | int]]:
    output: list[dict[str, float | int]] = []
    if not isinstance(raw, (list, tuple)):
        return output
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        value = _as_float(item[1])
        if value is None:
            continue
        output.append({"ts": int(item[0]), "value": value})
    return output


def _parse_multi(raw: Any) -> list[dict[str, Any]]:
    """Normalise TS.MGET / TS.MRANGE RESP2 and RESP3-ish replies."""
    rows: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        iterable = []
        for key, value in raw.items():
            if isinstance(value, dict):
                labels = value.get("labels", {})
                samples = value.get("samples", value.get("values", []))
            elif isinstance(value, (list, tuple)) and len(value) >= 2:
                labels, samples = value[0], value[-1]
            else:
                labels, samples = {}, []
            iterable.append([key, labels, samples])
    else:
        iterable = raw or []

    for row in iterable:
        if not isinstance(row, (list, tuple)) or len(row) < 3:
            continue
        sample_payload = row[2]
        # TS.MGET returns one [timestamp, value] pair instead of a list of pairs.
        if (
            isinstance(sample_payload, (list, tuple))
            and len(sample_payload) == 2
            and not isinstance(sample_payload[0], (list, tuple, dict))
        ):
            sample_payload = [sample_payload]
        rows.append(
            {
                "key": str(row[0]),
                "labels": _labels(row[1]),
                "points": _samples(sample_payload),
            }
        )
    return rows


def _pairs_to_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    result: dict[str, Any] = {}
    if not isinstance(raw, (list, tuple)):
        return result
    iterator = iter(raw)
    for key in iterator:
        try:
            result[str(key)] = next(iterator)
        except StopIteration:
            break
    return result


def _validate_tag(tag: str) -> str:
    if not TAG_PATTERN.fullmatch(tag):
        raise HTTPException(status_code=400, detail="Invalid demo tag")
    return tag


def _window(window_minutes: int) -> tuple[int, int]:
    end = int(time.time() * 1000)
    return end - window_minutes * 60_000, end


def _series_stats(points: list[dict[str, Any]]) -> dict[str, float | int | None]:
    values = [float(point["value"]) for point in points]
    if not values:
        return {"count": 0, "min": None, "max": None, "avg": None, "last": None}
    return {
        "count": len(values),
        "min": min(values),
        "max": max(values),
        "avg": sum(values) / len(values),
        "last": values[-1],
    }


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health(request: Request) -> dict[str, Any]:
    redis = _redis(request)
    try:
        started = time.perf_counter()
        await redis.ping()
        latency_ms = (time.perf_counter() - started) * 1000
        keys = await redis.execute_command(
            "TS.QUERYINDEX", "source=pi", "kind=raw"
        )
        return {
            "status": "ok",
            "redis_latency_ms": round(latency_ms, 3),
            "raw_series": len(keys or []),
            "dashboard": "connected",
        }
    except RedisError as exc:
        raise HTTPException(status_code=503, detail=f"Redis unavailable: {exc}") from exc


@app.get("/api/tags")
async def tags(request: Request) -> dict[str, Any]:
    """Return the latest sample and metadata for every raw PI series."""
    redis = _redis(request)
    command = "TS.MGET WITHLABELS FILTER source=pi kind=raw"
    try:
        result = await redis.execute_command(
            "TS.MGET", "WITHLABELS", "FILTER", "source=pi", "kind=raw"
        )
        rows = _parse_multi(result)
        pipe = redis.pipeline(transaction=False)
        for row in rows:
            tag = row["labels"].get("tag") or row["key"].split(":")[-1]
            pipe.hgetall(f"{KEY_PREFIX}:latest:{tag}")
        snapshots = await pipe.execute() if rows else []

        items = []
        for row, snapshot in zip(rows, snapshots, strict=False):
            labels = row["labels"]
            point = row["points"][-1] if row["points"] else None
            tag = labels.get("tag") or row["key"].split(":")[-1]
            snapshot = snapshot or {}
            items.append(
                {
                    "tag": tag,
                    "key": row["key"],
                    "site": labels.get("site", snapshot.get("site", "")),
                    "asset": labels.get("asset", snapshot.get("asset", "")),
                    "metric": labels.get("metric", snapshot.get("metric", "")),
                    "path": labels.get("path", snapshot.get("path", "")),
                    "value": _as_float(snapshot.get("value"))
                    if snapshot
                    else (point or {}).get("value"),
                    "ts": snapshot.get("ts", ""),
                    "ts_ms": int(snapshot.get("ts_ms") or (point or {}).get("ts") or 0),
                    "good": str(snapshot.get("good", "1")) == "1",
                    "units": snapshot.get("units", ""),
                }
            )
        items.sort(key=lambda item: (item["site"], item["asset"], item["metric"]))
        return {
            "items": items,
            "command": command,
            "explanation": (
                "TS.MGET uses labels to retrieve the latest value from every PI "
                "series in one round trip. The application does not need to know "
                "each Redis key in advance."
            ),
        }
    except ResponseError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/series/{tag}")
async def series(
    tag: str,
    request: Request,
    window_minutes: int = Query(15, ge=1, le=1440),
    aggregation: str = Query("raw"),
    bucket_ms: int = Query(60_000, ge=1_000, le=86_400_000),
    min_value: float | None = Query(None),
    max_value: float | None = Query(None),
    count: int = Query(500, ge=1, le=5000),
) -> dict[str, Any]:
    """Read one series with optional value filtering and query-time aggregation."""
    tag = _validate_tag(tag)
    aggregation = aggregation.lower()
    if aggregation not in RANGE_AGGREGATORS:
        raise HTTPException(status_code=400, detail="Unsupported aggregation")
    if (min_value is None) != (max_value is None):
        raise HTTPException(
            status_code=400,
            detail="Both min_value and max_value are required for value filtering",
        )

    redis = _redis(request)
    start, end = _window(window_minutes)
    key = f"{KEY_PREFIX}:ts:{tag}"
    args: list[Any] = ["TS.RANGE", key, start, end]
    if min_value is not None and max_value is not None:
        args += ["FILTER_BY_VALUE", min_value, max_value]
    args += ["COUNT", count]
    if aggregation != "raw":
        args += ["AGGREGATION", aggregation, bucket_ms]

    try:
        started = time.perf_counter()
        raw = await redis.execute_command(*args)
        latency_ms = (time.perf_counter() - started) * 1000
        points = _samples(raw)
        snapshot = await redis.hgetall(f"{KEY_PREFIX}:latest:{tag}")
        command = " ".join(str(value) for value in args)
        explanation = (
            "TS.RANGE reads the original samples in the selected time window."
            if aggregation == "raw"
            else (
                f"TS.RANGE calculates {aggregation.upper()} values in "
                f"{bucket_ms:,} ms buckets at read time, so the application "
                "receives a smaller, dashboard-ready result."
            )
        )
        if min_value is not None:
            explanation += " FILTER_BY_VALUE removes readings outside the accepted range before aggregation."
        return {
            "tag": tag,
            "key": key,
            "units": snapshot.get("units", ""),
            "metadata": {
                "site": snapshot.get("site", ""),
                "asset": snapshot.get("asset", ""),
                "metric": snapshot.get("metric", ""),
            },
            "points": points,
            "stats": _series_stats(points),
            "query_latency_ms": round(latency_ms, 3),
            "command": command,
            "explanation": explanation,
        }
    except ResponseError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/multi")
async def multi_series(
    request: Request,
    metric: str = Query("temperature"),
    group_by: str = Query("site"),
    reducer: str = Query("avg"),
    window_minutes: int = Query(15, ge=1, le=1440),
    aggregation: str = Query("avg"),
    bucket_ms: int = Query(60_000, ge=1_000, le=86_400_000),
) -> dict[str, Any]:
    """Run a label-filtered TS.MRANGE and aggregate across multiple series."""
    group_by = group_by.lower()
    reducer = reducer.lower()
    aggregation = aggregation.lower()
    if group_by not in GROUP_LABELS:
        raise HTTPException(status_code=400, detail="Unsupported GROUPBY label")
    if reducer not in REDUCERS:
        raise HTTPException(status_code=400, detail="Unsupported reducer")
    if aggregation not in RANGE_AGGREGATORS - {"raw"}:
        raise HTTPException(status_code=400, detail="Unsupported aggregation")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", metric):
        raise HTTPException(status_code=400, detail="Invalid metric label")

    start, end = _window(window_minutes)
    args: list[Any] = [
        "TS.MRANGE", start, end,
        "WITHLABELS",
        "AGGREGATION", aggregation, bucket_ms,
        "FILTER", "source=pi", "kind=raw", f"metric={metric}",
        "GROUPBY", group_by, "REDUCE", reducer,
    ]
    redis = _redis(request)
    try:
        started = time.perf_counter()
        raw = await redis.execute_command(*args)
        latency_ms = (time.perf_counter() - started) * 1000
        rows = _parse_multi(raw)
        command = " ".join(str(value) for value in args)
        return {
            "series": rows,
            "query_latency_ms": round(latency_ms, 3),
            "command": command,
            "explanation": (
                f"TS.MRANGE selects all raw PI series labelled metric={metric}, "
                f"creates {aggregation.upper()} time buckets, groups them by "
                f"{group_by}, and applies the {reducer.upper()} reducer across "
                "the matching assets in a single Redis query."
            ),
        }
    except ResponseError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/rollup/{tag}")
async def rollup(
    tag: str,
    request: Request,
    window_minutes: int = Query(60, ge=1, le=10_080),
) -> dict[str, Any]:
    """Compare raw storage with the TS.CREATERULE destination series."""
    tag = _validate_tag(tag)
    redis = _redis(request)
    try:
        rollup_keys = await redis.execute_command(
            "TS.QUERYINDEX", f"tag={tag}", "kind=rollup"
        )
        if not rollup_keys:
            raise HTTPException(status_code=404, detail="No rollup found for this tag")
        rollup_key = str(rollup_keys[0])
        raw_key = f"{KEY_PREFIX}:ts:{tag}"
        start, end = _window(window_minutes)
        pipe = redis.pipeline(transaction=False)
        pipe.execute_command("TS.RANGE", rollup_key, start, end)
        pipe.execute_command("TS.INFO", raw_key)
        pipe.execute_command("TS.INFO", rollup_key)
        values, raw_info_payload, rollup_info_payload = await pipe.execute()
        raw_info = _pairs_to_dict(raw_info_payload)
        rollup_info = _pairs_to_dict(rollup_info_payload)
        points = _samples(values)
        rollup_name = rollup_key.rsplit(":", 1)[-1]
        return {
            "tag": tag,
            "raw_key": raw_key,
            "rollup_key": rollup_key,
            "points": points,
            "raw": {
                "total_samples": int(raw_info.get("totalSamples", 0)),
                "memory_bytes": int(raw_info.get("memoryUsage", 0)),
                "chunk_type": str(raw_info.get("chunkType", "COMPRESSED")),
                "retention_ms": int(raw_info.get("retentionTime", 0)),
            },
            "rollup": {
                "name": rollup_name,
                "total_samples": int(rollup_info.get("totalSamples", 0)),
                "memory_bytes": int(rollup_info.get("memoryUsage", 0)),
                "chunk_type": str(rollup_info.get("chunkType", "COMPRESSED")),
                "retention_ms": int(rollup_info.get("retentionTime", 0)),
            },
            "command": f"TS.RANGE {rollup_key} {start} {end}",
            "rule": f"TS.CREATERULE {raw_key} {rollup_key} AGGREGATION avg 60000",
            "explanation": (
                "TS.CREATERULE maintains the rollup automatically as new PI samples "
                "arrive. Long-range dashboards query fewer pre-computed points while "
                "the raw series remains available for detailed investigation."
            ),
        }
    except ResponseError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    """Server-sent event stream backed by Redis Pub/Sub."""
    redis = _redis(request)

    async def stream() -> AsyncIterator[str]:
        pubsub = redis.pubsub()
        channel = f"{KEY_PREFIX}:updates"
        await pubsub.subscribe(channel)
        try:
            while not await request.is_disconnected():
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=1.0,
                )
                if message and message.get("data"):
                    yield f"event: sample\ndata: {message['data']}\n\n"
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.05)
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
