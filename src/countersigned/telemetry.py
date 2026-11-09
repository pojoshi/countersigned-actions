"""OpenTelemetry spans. Uses the API only, so it costs nothing until an SDK is configured."""

from contextlib import contextmanager
from typing import Any
from collections.abc import Iterator

from opentelemetry import trace

_tracer = trace.get_tracer("countersigned")


@contextmanager
def span(name: str, **attrs: Any) -> Iterator[Any]:
    with _tracer.start_as_current_span(name) as s:
        for k, v in attrs.items():
            if v is not None:
                s.set_attribute(f"countersigned.{k}", v)
        yield s
