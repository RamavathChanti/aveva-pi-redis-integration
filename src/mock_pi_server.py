"""A tiny mock of the AVEVA PI Web API for local testing.

Implements just enough of the real API surface for the integration service:
  GET /piwebapi/points?path=...                 -> { WebId, Name, Path, ... }
  GET /piwebapi/streams/{webId}/value           -> current value
  GET /piwebapi/streams/{webId}/recorded        -> recorded values in a window
  GET /piwebapi/streamsets/recorded?webId=...    -> recorded values, many streams

Signals are pure functions of time, so recorded history is reproducible and
matches the current value at any instant. Run with:
    uvicorn mock_pi_server:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import base64
import hashlib
import math
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="Mock PI Web API")

# Emit a synthetic archived sample every this many seconds within a window.
SAMPLE_STEP_SECONDS = 2


def _web_id(path: str) -> str:
    """Stable pseudo-WebId derived from the path (mirrors PI's opaque ids)."""
    digest = hashlib.sha1(path.encode()).digest()
    return "F1DP" + base64.urlsafe_b64encode(digest).decode().rstrip("=")[:24]


def _tag_name(path: str) -> str:
    return path.replace("/", "\\").split("\\")[-1] or path


def _signal(tag: str, secs: float) -> tuple[float, str]:
    """Pure (value, units) for a tag at epoch-second `secs`."""
    low = tag.lower()
    if "sinusoid" in low:
        return round(50 + 50 * math.sin(secs / 10.0), 3), "%"
    if "cdt" in low:  # sawtooth 0..100 over 30s
        return round((secs % 30) / 30 * 100, 3), "m3/h"
    # deterministic pseudo-random walk: hash the time bucket for reproducibility
    bucket = int(secs // SAMPLE_STEP_SECONDS)
    h = int.from_bytes(hashlib.sha1(f"{tag}:{bucket}".encode()).digest()[:4], "big")
    return round((h % 100000) / 1000.0, 3), "unit"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_pi_time(value: str) -> datetime:
    """Parse an ISO-8601 timestamp or PI '*' (= now)."""
    if value in ("*", "", None):
        return _now()
    cleaned = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _value_item(tag: str, dt: datetime) -> dict:
    value, units = _signal(tag, dt.timestamp())
    return {
        "Timestamp": _iso(dt),
        "Value": value,
        "Good": True,
        "Questionable": False,
        "UnitsAbbreviation": units,
    }


def _recorded(tag: str, start: datetime, end: datetime, max_count: int) -> list[dict]:
    """Generate archived samples on a fixed grid within [start, end]."""
    step = timedelta(seconds=SAMPLE_STEP_SECONDS)
    # Align to the grid so restarts don't produce off-by-a-fraction duplicates.
    epoch = int(start.timestamp())
    aligned = epoch - (epoch % SAMPLE_STEP_SECONDS)
    t = datetime.fromtimestamp(aligned, tz=timezone.utc)
    if t < start:
        t += step
    items: list[dict] = []
    while t <= end and len(items) < max_count:
        items.append(_value_item(tag, t))
        t += step
    return items


# WebId -> path registry, populated as points are resolved.
_by_web_id: dict[str, str] = {}


def _path_for(web_id: str) -> str:
    path = _by_web_id.get(web_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Unknown WebId")
    return path


@app.get("/piwebapi/points")
def resolve_point(path: str):
    web_id = _web_id(path)
    _by_web_id[web_id] = path
    return {
        "WebId": web_id,
        "Name": _tag_name(path),
        "Path": path,
        "PointClass": "classic",
        "PointType": "Float64",
    }


@app.get("/piwebapi/streams/{web_id}/value")
def stream_value(web_id: str):
    tag = _tag_name(_path_for(web_id))
    return _value_item(tag, _now())


@app.get("/piwebapi/streams/{web_id}/recorded")
def stream_recorded(
    web_id: str,
    startTime: str = "*-10m",
    endTime: str = "*",
    maxCount: int = 1000,
):
    tag = _tag_name(_path_for(web_id))
    start = _parse_pi_time(startTime) if not startTime.startswith("*-") else _now() - timedelta(minutes=10)
    end = _parse_pi_time(endTime)
    return {"Items": _recorded(tag, start, end, maxCount)}


@app.get("/piwebapi/streamsets/recorded")
def streamset_recorded(
    webId: list[str] = Query(default=[]),
    startTime: str = "*",
    endTime: str = "*",
    maxCount: int = 1000,
):
    start = _parse_pi_time(startTime)
    end = _parse_pi_time(endTime)
    items = []
    for wid in webId:
        path = _by_web_id.get(wid)
        if path is None:
            continue
        items.append(
            {
                "WebId": wid,
                "Name": _tag_name(path),
                "Path": path,
                "Items": _recorded(_tag_name(path), start, end, maxCount),
            }
        )
    return {"Items": items}


@app.get("/piwebapi")
def root():
    return {"Links": {}, "info": "Mock PI Web API for the Redis integration POC"}
