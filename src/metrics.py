"""In-process metrics + a tiny health/metrics HTTP server.

Exposes:
  GET /healthz  -> 200 {"status": "ok"} once the first poll has succeeded,
                   503 while starting up or if the last poll failed.
  GET /metrics  -> Prometheus text exposition of the counters below.

Runs inside the integration process as an asyncio task (uvicorn), so it
shares the event loop with the poll loop — no extra process needed.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import uvicorn
from fastapi import FastAPI, Response


@dataclass
class Metrics:
    samples_written_total: int = 0
    polls_total: int = 0
    poll_errors_total: int = 0
    points_resolved: int = 0
    points_configured: int = 0
    last_poll_unixtime: float = 0.0
    last_poll_ok: bool = False
    started: bool = False
    _extra_labels: dict = field(default_factory=dict)

    def render_prometheus(self) -> str:
        lines = [
            "# HELP pi_samples_written_total Samples written to Redis.",
            "# TYPE pi_samples_written_total counter",
            f"pi_samples_written_total {self.samples_written_total}",
            "# HELP pi_polls_total Poll cycles executed.",
            "# TYPE pi_polls_total counter",
            f"pi_polls_total {self.polls_total}",
            "# HELP pi_poll_errors_total Errors during polling.",
            "# TYPE pi_poll_errors_total counter",
            f"pi_poll_errors_total {self.poll_errors_total}",
            "# HELP pi_points_resolved Points successfully resolved to WebIds.",
            "# TYPE pi_points_resolved gauge",
            f"pi_points_resolved {self.points_resolved}",
            "# HELP pi_points_configured Points configured for ingestion.",
            "# TYPE pi_points_configured gauge",
            f"pi_points_configured {self.points_configured}",
            "# HELP pi_last_poll_unixtime Timestamp of the last completed poll.",
            "# TYPE pi_last_poll_unixtime gauge",
            f"pi_last_poll_unixtime {self.last_poll_unixtime}",
            "# HELP pi_up 1 if the last poll succeeded, else 0.",
            "# TYPE pi_up gauge",
            f"pi_up {1 if self.last_poll_ok else 0}",
        ]
        return "\n".join(lines) + "\n"


metrics = Metrics()


def build_app() -> FastAPI:
    app = FastAPI(title="PI→Redis integration health")

    @app.get("/healthz")
    def healthz() -> Response:
        healthy = metrics.started and metrics.last_poll_ok
        return Response(
            content='{"status": "%s"}' % ("ok" if healthy else "degraded"),
            media_type="application/json",
            status_code=200 if healthy else 503,
        )

    @app.get("/metrics")
    def prometheus() -> Response:
        return Response(content=metrics.render_prometheus(), media_type="text/plain")

    return app


async def serve_health(port: int) -> tuple[uvicorn.Server, asyncio.Task]:
    """Start the health server as a background task; returns (server, task)."""
    config = uvicorn.Config(
        build_app(), host="0.0.0.0", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    return server, task
