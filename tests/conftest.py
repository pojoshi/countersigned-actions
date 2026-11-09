import os
from datetime import datetime, timedelta, UTC

import pytest

from countersigned import Database, Gate
from countersigned.seed import seed_demo

DSN = os.environ.get("COUNTERSIGNED_TEST_DATABASE_URL")
T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)  # noon UTC: outside the prod change window


class Clock:
    def __init__(self, start: datetime = T0) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += timedelta(**kw)

    def set(self, t: datetime) -> None:
        self.t = t


@pytest.fixture(scope="session")
def db():
    if not DSN:
        pytest.skip("set COUNTERSIGNED_TEST_DATABASE_URL to run the Postgres tests")
    d = Database(DSN, max_size=20)
    yield d
    d.close()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def gate(db, clock):
    g = Gate(db, clock)
    seed_demo(g, reset=True)
    return g


def delete_params(segment="eu-erasure-single", count=3, ticket="TKT-1001", **kw):
    return {"segment": segment, "count": count, "ticket_id": ticket, "reason": "GDPR erasure request", **kw}


def op(operation, **params):
    return {"operation": operation, "params": params}
