"""Print selected LiveKit startup spans to the existing agent log."""

import logging
from datetime import UTC, datetime
from time import time_ns

from livekit.agents.telemetry import set_tracer_provider, tracer
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider

logger = logging.getLogger("avatarweave.agent")
STARTUP_SPANS = frozenset(
    {"session_start", "room_connect", "start_agent_activity", "wait_for_participant", "setup_toolsets"}
)


class StartupSpanLogger(SpanProcessor):
    def __init__(self, job_id: str, room: str):
        self.job_id = job_id
        self.room = room
        self.started_ns = time_ns()
        self.active = True

    def on_end(self, span: ReadableSpan) -> None:
        if (
            not self.active
            or span.name not in STARTUP_SPANS
            or span.start_time is None
            or span.end_time is None
        ):
            return
        context = span.context
        logger.info(
            "connection_startup_span",
            extra={
                "stage": span.name,
                "job_id": self.job_id,
                "room": self.room,
                "started_at": datetime.fromtimestamp(span.start_time / 1e9, UTC).isoformat(
                    timespec="milliseconds"
                ),
                "ended_at": datetime.fromtimestamp(span.end_time / 1e9, UTC).isoformat(
                    timespec="milliseconds"
                ),
                "duration_ms": round((span.end_time - span.start_time) / 1e6, 1),
                "start_offset_ms": round((span.start_time - self.started_ns) / 1e6, 1),
                "trace_id": format(context.trace_id, "032x") if context else None,
                "span_id": format(context.span_id, "016x") if context else None,
                "parent_span_id": format(span.parent.span_id, "016x") if span.parent else None,
                "status": span.status.status_code.name,
            },
        )


def install_startup_tracing(job_id: str, room: str) -> StartupSpanLogger | None:
    # LiveKit keeps its provider separate from the global OpenTelemetry provider.
    # This SDK has no public getter; preserve its current exporters when attaching.
    provider = getattr(tracer, "_tracer_provider", None)
    if isinstance(provider, (trace.ProxyTracerProvider, trace.NoOpTracerProvider)):
        provider = TracerProvider()
        set_tracer_provider(provider)
    if not isinstance(provider, TracerProvider):
        logger.warning("connection_startup_tracing_unavailable", extra={"job_id": job_id})
        return None
    processor = StartupSpanLogger(job_id, room)
    provider.add_span_processor(processor)
    return processor
