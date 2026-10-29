import logging

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON
from opentelemetry.trace import Status, StatusCode

from avatarweave_agent import startup_tracing


def test_startup_spans_log_duration_and_identity_without_span_payload(caplog):
    provider = TracerProvider(sampler=ALWAYS_ON)
    processor = startup_tracing.StartupSpanLogger("job-test", "room-test")
    provider.add_span_processor(processor)
    tracer = provider.get_tracer("test")
    start = processor.started_ns + 10_000_000
    with caplog.at_level(logging.INFO, logger="avatarweave.agent"):
        span = tracer.start_span("room_connect", start_time=start)
        span.set_attribute("credential", "do-not-log")
        span.set_status(Status(StatusCode.ERROR))
        span.end(end_time=start + 123_400_000)
        tracer.start_span("chat").end()
        processor.active = False
        tracer.start_span("room_connect").end()
    records = [r for r in caplog.records if r.message == "connection_startup_span"]
    assert len(records) == 1
    record = records[0]
    assert record.stage == "room_connect"
    assert record.duration_ms == 123.4
    assert record.start_offset_ms == 10
    assert record.job_id == "job-test"
    assert record.room == "room-test"
    assert record.status == "ERROR"
    assert record.started_at.endswith("+00:00")
    assert len(record.trace_id) == 32
    assert "do-not-log" not in str(vars(record))
    provider.shutdown()


def test_install_preserves_existing_livekit_provider(monkeypatch):
    provider = TracerProvider()
    monkeypatch.setattr(startup_tracing.tracer, "_tracer_provider", provider)

    def must_not_replace(*args, **kwargs):
        raise AssertionError("must preserve the existing LiveKit provider")

    monkeypatch.setattr(startup_tracing, "set_tracer_provider", must_not_replace)
    processor = startup_tracing.install_startup_tracing("job-test", "room-test")
    assert isinstance(processor, startup_tracing.StartupSpanLogger)
    provider.shutdown()
