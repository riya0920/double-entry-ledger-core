"""Postgres backend, and the retry loop SQLite could never justify.

SQLite admits one writer, so a concurrent posting BLOCKS. Under SERIALIZABLE,
Postgres lets the transactions interleave and then refuses to commit one of
them: `SQLSTATE 40001, could not serialize access due to read/write
dependencies`. That is not an error in the usual sense -- nothing is broken, the
database has detected that the two transactions cannot both be true and has
picked one to abort. **The correct response is to retry, and a caller that does
not is the bug.**

WHY THE RETRY LOOP IS A FIRST-CLASS OBJECT HERE AND NOT AN `except` CLAUSE:

  IT MUST BE BOUNDED     an unbounded retry under sustained contention is a
                         livelock that presents as a hung request.
  IT MUST BACK OFF       immediate retry re-creates the same collision with the
                         same peers; jitter is what breaks the convoy.
  IT MUST BE COUNTED     a serialization-failure rate that has moved is a
                         workload change nobody was told about, and it is
                         invisible if the retry is silent.
  IT MUST NOT SWALLOW    a 40001 is retryable, a check-constraint violation is
                         NOT -- retrying a floor breach just breaches the floor
                         again, more slowly. Conflating them turns a permanent
                         failure into a timeout.

That last one is the distinction most retry wrappers get wrong, and it is why
`is_retryable` inspects SQLSTATE rather than matching on the message text.

WHAT THIS PORT IS NOT. It is not a drop-in replacement for `ledger/core.py`.
The payments lifecycle, FX, snapshots and the idempotency machinery all still
run on SQLite; this exists to answer one question the SQLite store cannot --
what happens when two writers genuinely interleave -- and `pg_drift_test.py` is
the thing that asks it.
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

# 40001 serialization_failure, 40P01 deadlock_detected.
# Both mean "nothing is wrong, try again"; everything else does not.
RETRYABLE_SQLSTATES = {"40001", "40P01"}

DSN = os.environ.get(
    "LEDGER_PG_DSN",
    "host=127.0.0.1 port=5432 dbname=ledger user=ledger password=ledger")


def is_retryable(exc: BaseException) -> bool:
    """SQLSTATE, not message text.

    Matching on the message is how a retry wrapper ends up retrying a constraint
    violation: the strings drift between versions, and a floor breach retried
    forever is a permanent failure wearing a timeout's clothes.
    """
    return getattr(exc, "sqlstate", None) in RETRYABLE_SQLSTATES


@dataclass
class RetryStats:
    attempts: int = 0
    retries: int = 0
    serialization_failures: int = 0
    deadlocks: int = 0
    exhausted: int = 0
    committed: int = 0
    backoff_ms: list = field(default_factory=list)

    @property
    def retry_rate(self) -> float:
        return self.retries / self.attempts if self.attempts else 0.0


class PgLedger:
    """Minimal Postgres-backed ledger: open accounts, post balanced entries."""

    def __init__(self, dsn: str = DSN, max_retries: int = 8,
                 base_backoff_ms: float = 2.0, stats: RetryStats | None = None,
                 isolation: str = "SERIALIZABLE"):
        import psycopg

        self._psycopg = psycopg
        self.dsn = dsn
        self.isolation = isolation
        self.max_retries = max_retries
        self.base_backoff_ms = base_backoff_ms
        self.stats = stats if stats is not None else RetryStats()
        self._conn = None

    # ---------------------------------------------------------------- setup
    def connect(self):
        conn = self._psycopg.connect(self.dsn, autocommit=False)
        # SERIALIZABLE is the default and the point of this port. READ COMMITTED
        # is selectable ONLY so the drill can measure what it costs to give it
        # up -- see section 3 of pg_drift_test.py, where the two are run on the
        # same workload and the answer is not the one I expected.
        conn.isolation_level = getattr(
            self._psycopg.IsolationLevel, self.isolation)
        return conn

    @property
    def conn(self):
        """One long-lived connection per instance, reused across transactions.

        A connection PER TRANSACTION is the shape this first had, and it made
        the drill 30x slower than the database: every posting paid a TCP
        handshake plus SCRAM authentication, so the benchmark was measuring
        connection setup and reporting it as ledger throughput.

        It matters more than performance. Retrying a serialization failure on a
        FRESH connection loses the very thing that makes the retry meaningful --
        the retry is supposed to re-read the state that changed underneath it,
        on a connection whose session settings are already what they were. Real
        services hold a pool for exactly this reason, and a pool is what this
        stands in for at one connection per worker thread.
        """
        if self._conn is None or self._conn.closed:
            self._conn = self.connect()
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def install_schema(self, path: Path | None = None) -> None:
        path = path or Path(__file__).with_name("pg_schema.sql")
        sql = path.read_text(encoding="utf-8")
        with self._psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(sql)

    def open_account(self, account_id: str, kind: str, currency: str,
                     floor_minor: int = 0, overdraft_allowed: bool = False) -> None:
        with self._psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO account (id, kind, currency, floor_minor,"
                " overdraft_allowed) VALUES (%s,%s,%s,%s,%s)"
                " ON CONFLICT (id) DO NOTHING",
                (account_id, kind, currency, floor_minor, overdraft_allowed))
            conn.execute(
                "INSERT INTO account_balance (account_id, balance_minor)"
                " VALUES (%s, 0) ON CONFLICT (account_id) DO NOTHING",
                (account_id,))

    # ---------------------------------------------------------- the retry loop
    def run_serializable(self, work):
        """Run `work(conn)` under SERIALIZABLE, retrying only what is retryable.

        Returns whatever `work` returns. Raises the original exception if it is
        not a serialization failure, or `SerializationExhausted` if the bounded
        retries run out -- because pretending an exhausted retry succeeded is
        the one outcome worse than failing.
        """
        last = None
        for attempt in range(self.max_retries + 1):
            self.stats.attempts += 1
            conn = self.conn
            try:
                result = work(conn)
                conn.commit()
                self.stats.committed += 1
                return result
            except BaseException as exc:                      # noqa: BLE001
                try:
                    conn.rollback()
                except Exception:                             # noqa: BLE001
                    self.close()
                if not is_retryable(exc):
                    raise
                last = exc
                if getattr(exc, "sqlstate", None) == "40001":
                    self.stats.serialization_failures += 1
                else:
                    self.stats.deadlocks += 1
                self.stats.retries += 1
                # Exponential backoff with FULL jitter. Without jitter the
                # aborted peers wake together and collide again -- the retry
                # storm is a convoy, and randomising the wake is what breaks it.
                delay = self.base_backoff_ms * (2 ** attempt)
                delay = random.uniform(0, delay)
                self.stats.backoff_ms.append(delay)
                time.sleep(delay / 1000.0)

        self.stats.exhausted += 1
        raise SerializationExhausted(
            "gave up after {} retries; last: {}".format(self.max_retries, last))

    # ------------------------------------------------------------- posting
    def post(self, entries, actor: str, reason: str, request_id: str) -> int:
        """One balanced posting, sealed, under SERIALIZABLE with retries."""

        def work(conn):
            cur = conn.execute(
                "INSERT INTO journal_txn (actor, reason, request_id)"
                " VALUES (%s,%s,%s) RETURNING id", (actor, reason, request_id))
            txn_id = cur.fetchone()[0]
            for e in entries:
                conn.execute(
                    "INSERT INTO journal_entry (txn_id, account_id, direction,"
                    " amount_minor, currency) VALUES (%s,%s,%s,%s,%s)",
                    (txn_id, e["account_id"], e["direction"],
                     e["amount_minor"], e["currency"]))
            conn.execute("UPDATE journal_txn SET sealed = TRUE WHERE id = %s",
                         (txn_id,))
            return txn_id

        return self.run_serializable(work)

    def balance(self, account_id: str) -> int:
        with self._psycopg.connect(self.dsn, autocommit=True) as conn:
            row = conn.execute(
                "SELECT balance_minor FROM account_balance WHERE account_id = %s",
                (account_id,)).fetchone()
        return int(row[0]) if row else 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ---------------------------------------------------------- invariants
    def check_invariants(self) -> list:
        """The same four as the SQLite port, re-derived from the journal."""
        problems = []
        with self._psycopg.connect(self.dsn, autocommit=True) as conn:
            unbalanced = conn.execute(
                "SELECT t.id, c.currency FROM journal_txn t"
                " JOIN txn_currency_total c ON c.txn_id = t.id"
                " WHERE t.sealed AND c.debit_minor <> c.credit_minor").fetchall()
            for txn_id, ccy in unbalanced:
                problems.append(
                    "I1 unbalanced sealed txn {} in {}".format(txn_id, ccy))

            floors = conn.execute(
                "SELECT a.id, b.balance_minor, a.floor_minor FROM account a"
                " JOIN account_balance b ON b.account_id = a.id"
                " WHERE NOT a.overdraft_allowed"
                "   AND b.balance_minor < a.floor_minor").fetchall()
            for acct, bal, floor in floors:
                problems.append(
                    "I2 floor breach {}: {} < {}".format(acct, bal, floor))

            # I3: the cache must equal the journal. This is the invariant that
            # catches a trigger that raced, and it is the reason the cache is
            # never trusted as a source.
            drift = conn.execute(
                "SELECT b.account_id, b.balance_minor, COALESCE(j.derived, 0)"
                "  FROM account_balance b"
                "  LEFT JOIN (SELECT account_id,"
                "               SUM(CASE WHEN direction='D' THEN amount_minor"
                "                        ELSE -amount_minor END) AS derived"
                "               FROM journal_entry GROUP BY account_id) j"
                "    ON j.account_id = b.account_id"
                " WHERE b.balance_minor <> COALESCE(j.derived, 0)").fetchall()
            for acct, cached, derived in drift:
                problems.append(
                    "I3 cache drift {}: cached {} vs journal {}".format(
                        acct, cached, derived))

            unsealed = conn.execute(
                "SELECT count(*) FROM journal_txn WHERE NOT sealed").fetchone()[0]
            if unsealed:
                problems.append(
                    "I4 {} transactions left unsealed".format(unsealed))
        return problems


class SerializationExhausted(RuntimeError):
    """Bounded retries ran out. Reported, never swallowed."""
