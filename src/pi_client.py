"""Thin async client for the AVEVA PI Web API.

Only the endpoints needed for this POC are implemented:
  * resolve a PI point path -> WebId          (GET /points?path=...)
  * read the current value of a stream        (GET /streams/{webId}/value)

The shapes mirror the real PI Web API so this client works unchanged
against a production server (given valid auth + network access).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger("pi_client")


@dataclass
class PIValue:
    """A single time-series sample read from PI."""

    web_id: str
    path: str
    timestamp: str          # ISO-8601 UTC, as PI returns it
    value: float | None     # None if the value is not numeric / bad
    good: bool
    units: str | None = None


class PIWebAPIClient:
    def __init__(
        self,
        base_url: str,
        *,
        auth_mode: str = "none",
        username: str = "",
        password: str = "",
        verify_ssl: bool = True,
        timeout: float = 15.0,
    ) -> None:
        auth = None
        if auth_mode == "basic" and username:
            auth = httpx.BasicAuth(username, password)
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=auth,
            verify=verify_ssl,
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def resolve_point(self, path: str) -> str:
        """Resolve a PI point path (\\\\SERVER\\TAG) to its WebId."""
        resp = await self._client.get("/points", params={"path": path})
        resp.raise_for_status()
        web_id = resp.json()["WebId"]
        log.debug("resolved %s -> %s", path, web_id)
        return web_id

    @staticmethod
    def _to_pi_value(item: dict, web_id: str, path: str) -> PIValue:
        raw = item.get("Value")
        # PI encodes bad/digital values as objects; only keep plain numbers.
        numeric = raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None
        return PIValue(
            web_id=web_id,
            path=path,
            timestamp=item.get("Timestamp"),
            value=float(numeric) if numeric is not None else None,
            good=bool(item.get("Good", False)),
            units=item.get("UnitsAbbreviation") or None,
        )

    async def get_current_value(self, web_id: str, path: str) -> PIValue:
        """Read the latest recorded value for a stream."""
        resp = await self._client.get(f"/streams/{web_id}/value")
        resp.raise_for_status()
        return self._to_pi_value(resp.json(), web_id, path)

    async def get_streamset_recorded(
        self,
        web_ids: dict[str, str],
        start_time: str,
        end_time: str = "*",
        max_count: int = 1000,
    ) -> dict[str, list[PIValue]]:
        """Batch-read recorded (archived) values for many streams in ONE request.

        Mirrors PI's ``GET /streamsets/recorded?webId=…&webId=…``. Used for
        backfill/gap-recovery: fetch everything archived in [start_time, end_time]
        for all tags at once instead of one request per tag.

        ``web_ids`` maps WebId -> point path. Returns WebId -> [PIValue...].
        """
        params = [("webId", w) for w in web_ids]
        params += [
            ("startTime", start_time),
            ("endTime", end_time),
            ("maxCount", str(max_count)),
        ]
        resp = await self._client.get("/streamsets/recorded", params=params)
        resp.raise_for_status()

        out: dict[str, list[PIValue]] = {}
        for stream in resp.json().get("Items", []):
            web_id = stream.get("WebId", "")
            path = web_ids.get(web_id, stream.get("Path", ""))
            out[web_id] = [
                self._to_pi_value(it, web_id, path) for it in stream.get("Items", [])
            ]
        return out
