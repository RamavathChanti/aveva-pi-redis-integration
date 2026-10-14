"""Small AVEVA PI Web API mock for the customer demo.

The API shape mirrors the PI Web API endpoints used by the integration service.
Signals are deterministic functions of time so backfill and gap recovery can be
shown without a real PI installation.
"""
from __future__ import annotations

import base64
import hashlib
import math
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="Mock PI Web API")
SAMPLE_STEP_SECONDS = 2


def _web_id(path: str) -> str:
    digest = hashlib.sha1(path.encode()).digest()
    return "F1DP" + base64.urlsafe_b64encode(digest).decode().rstrip("=")[:24]


def _tag_name(path: str) -> str:
    return path.replace("/", "\\").split("\\")[-1] or path


def _stable_number(text: str) -> int:
    return int.from_bytes(hashlib.sha1(text.encode()).digest()[:4], "big")


def _signal(tag: str, secs: float) -> tuple[float, str]:
    """Return a believable demo value and engineering unit for a tag."""
    low = tag.lower()
    seed = _stable_number(tag)
    phase = (seed % 360) * math.pi / 180

    if "temperature" in low:
        base = 66 + (seed % 70) / 10
        value = base + 4.5 * math.sin(secs / 18 + phase)
        return round(value, 2), "degC"

    if "power" in low:
        base = 360 + seed % 120
        value = base + 55 * math.sin(secs / 15 + phase)
        return round(max(value, 0), 2), "MW"

    if "vibration" in low:
        base = 1.2 + (seed % 12) / 10
        value = base + 0.7 * abs(math.sin(secs / 8 + phase))
        return round(value, 3), "mm/s"

    if "pressure" in low:
        base = 95 + seed % 20
        value = base + 7 * math.sin(secs / 20 + phase)
        return round(value, 2), "bar"

    if "sinusoid" in low:
        return round(50 + 50 * math.sin(secs / 10.0), 3), "%"

    if "cdt" in low:
        return round((secs % 30) / 30 * 100, 3), "m3/h"

    bucket = int(secs // SAMPLE_STEP_SECONDS)
    hashed = _stable_number(f"{tag}:{bucket}")
    return round((hashed % 100000) / 1000.0, 3), "unit"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_pi_time(value: str) -> datetime:
    if value in ("*", "", None):
        return _now()
    if value.startswith("*-"):
        suffix = value[2:].lower().strip()
        if suffix.endswith("m"):
            try:
                return _now() - timedelta(minutes=float(suffix[:-1]))
            except ValueError:
                pass
        elif suffix.endswith("h"):
            try:
                return _now() - timedelta(hours=float(suffix[:-1]))
            except ValueError:
                pass
        elif suffix.endswith("d"):
            try:
                return _now() - timedelta(days=float(suffix[:-1]))
            except ValueError:
                pass
        elif suffix.endswith("s"):
            try:
                return _now() - timedelta(seconds=float(suffix[:-1]))
            except ValueError:
                pass
    cleaned = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(cleaned)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _value_item(tag: str, value_time: datetime) -> dict:
    value, units = _signal(tag, value_time.timestamp())
    return {
        "Timestamp": _iso(value_time),
        "Value": value,
        "Good": True,
        "Questionable": False,
        "UnitsAbbreviation": units,
    }


def _recorded(tag: str, start: datetime, end: datetime, max_count: int) -> list[dict]:
    step = timedelta(seconds=SAMPLE_STEP_SECONDS)
    epoch = int(start.timestamp())
    aligned = epoch - (epoch % SAMPLE_STEP_SECONDS)
    current = datetime.fromtimestamp(aligned, tz=timezone.utc)
    if current < start:
        current += step

    items: list[dict] = []
    while current <= end and len(items) < max_count:
        items.append(_value_item(tag, current))
        current += step
    return items


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
    return _value_item(_tag_name(_path_for(web_id)), _now())


@app.get("/piwebapi/streams/{web_id}/recorded")
def stream_recorded(
    web_id: str,
    startTime: str = "*-10m",
    endTime: str = "*",
    maxCount: int = 1000,
):
    tag = _tag_name(_path_for(web_id))
    start = (
        _now() - timedelta(minutes=10)
        if startTime.startswith("*-")
        else _parse_pi_time(startTime)
    )
    return {"Items": _recorded(tag, start, _parse_pi_time(endTime), maxCount)}


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
    for web_id in webId:
        path = _by_web_id.get(web_id)
        if path is None:
            continue
        items.append(
            {
                "WebId": web_id,
                "Name": _tag_name(path),
                "Path": path,
                "Items": _recorded(_tag_name(path), start, end, maxCount),
            }
        )
    return {"Items": items}


@app.get("/piwebapi")
def root():
    return {"Links": {}, "info": "Mock PI Web API for the Redis integration POC"}
