"""Connection pool and schema bootstrap."""

from contextlib import contextmanager
from importlib import resources
from collections.abc import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class Database:
    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self.dsn = dsn
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(resources.files("countersigned").joinpath("schema.sql").read_text())  # type: ignore[arg-type]
        self.pool = ConnectionPool(
            dsn, min_size=min_size, max_size=max_size, kwargs={"row_factory": dict_row}, open=True,
        )

    def close(self) -> None:
        self.pool.close()

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        """One connection, one transaction: commit on success, roll back on any exception."""
        with self.pool.connection() as conn, conn.transaction():
            yield conn

    def reset_all(self) -> None:
        """Wipe everything (tests and demos only). Bypasses the append-only triggers on purpose."""
        with psycopg.connect(self.dsn, autocommit=True) as c:
            c.execute("ALTER TABLE audit_log DISABLE TRIGGER USER")
            try:
                c.execute(
                    "TRUNCATE audit_log, executions, outbox, approvals, actions, principals, policies, "
                    "sim_customers, sim_tickets, sim_payments, sim_services, sim_exports, sim_transfers RESTART IDENTITY CASCADE"
                )
            finally:
                c.execute("ALTER TABLE audit_log ENABLE TRIGGER USER")
            c.execute("UPDATE kill_switch SET active = false, reason = NULL, set_by = NULL, set_at = NULL")
