from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from countersigned.proposers import submit
from conftest import delete_params, op


def test_decisions_and_executions_emit_spans(gate):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    import countersigned.telemetry as tel
    tel._tracer = provider.get_tracer("countersigned-test")
    try:
        submit(gate, "support-bot", op("customer_data.delete_records", **delete_params()))
        gate.run_worker_until_empty()
    finally:
        tel._tracer = trace.get_tracer("countersigned")
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert {"gate.propose", "gate.execute"} <= spans.keys()
    assert spans["gate.propose"].attributes["countersigned.outcome"] == "execute"
    assert spans["gate.propose"].attributes["countersigned.agent"] == "support-bot"
    assert spans["gate.execute"].attributes["countersigned.result"] == "executed"
