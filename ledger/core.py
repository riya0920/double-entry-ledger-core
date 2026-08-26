"""Posting engine for the immutable journal.

Money is *always* an integer count of minor units (cents, pence). There is no
float arithmetic in this module and there must never be -- see docs/DESIGN.md.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

SCHEMA = Path(__file__).with_name("schema.sql")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class LedgerError(Exception):
    pass


class Unbalanced(LedgerError):
    pass


class FloorBreach(LedgerError):
    pass


class IdempotencyConflict(LedgerError):
    """Same key, different payload. Never returns the first caller's result."""


@dataclass(frozen=True)
class Entry:
    account_id: str
    direction: str          # 'D' or 'C'
    amount_minor: int
    currency: str

    def __post_init__(self) -> None:
        if self.direction not in ("D", "C"):
            raise LedgerError("bad direction " + repr(self.direction))
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise LedgerError("amount_minor must be int minor units, not float/Decimal")
        if self.amount_minor <= 0:
            raise LedgerError("amount_minor must be > 0; direction expresses the sign")


def debit(account_id: str, amount_minor: int, currency: str) -> Entry:
    return Entry(account_id, "D", amount_minor, currency)


def credit(account_id: str, amount_minor: int, currency: str) -> Entry:
    return Entry(account_id, "C", amount_minor, currency)


@contextmanager
def _null_ctx(con):
    yield con


class Ledger:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self._local = threading.local()

        # A THREAD-LOCAL CONNECTION TO ":memory:" GIVES EVERY THREAD ITS OWN
        # EMPTY DATABASE, and that is not a tuning detail -- it is a silent,
        # total failure under any threaded server.
        #
        # SQLite's ":memory:" names a database PRIVATE TO THE CONNECTION. With
        # a thread-local connection, thread A runs the schema and thread B
        # opens a fresh blank database that happens to have the same name. The
        # constructor's own schema write lands in whichever thread constructed
        # the Ledger and is invisible to every request thread after it.
        #
        # Found by wiring Idempotency-Key into the payment endpoints: the first
        # request against a TestClient failed with `no such table:
        # idempotency_key`, on a schema that plainly creates it. In-process
        # tests never saw it because they construct and use the Ledger on one
        # thread.
        #
        # A shared cache URI makes ":memory:" mean "the same database for every
        # connection in this process", which is what a caller passing
        # ":memory:" always meant. `uri=True` is required for the connect
        # string to be parsed as one.
        self._memory_uri = None
        if self.path == ":memory:":
            self._memory_uri = "file:ledger-{}?mode=memory&cache=shared".format(
                id(self))
            # One connection held open for the lifetime of the Ledger. A shared
            # in-memory database is destroyed when the LAST connection to it
            # closes, so without this the schema evaporates the moment a
            # request thread finishes and closes its own.
            self._keepalive = self._new_connection()

        con = self._conn()
        con.executescript(SCHEMA.read_text())

    # -- connection handling -------------------------------------------------
    def _new_connection(self) -> sqlite3.Connection:
        target = self._memory_uri or self.path
        con = sqlite3.connect(target, timeout=30, isolation_level=None,
                              uri=bool(self._memory_uri))
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 30000")
        if self.path != ":memory:":
            con.execute("PRAGMA journal_mode = WAL")
            con.execute("PRAGMA synchronous = NORMAL")
        return con

    def _conn(self) -> sqlite3.Connection:
        """One connection per thread, all pointing at the SAME database.

        Per-thread rather than pooled because a sqlite3 connection may not be
        shared across threads, and because the interesting contention here is
        the write lock rather than connection setup -- a pool would queue on
        the same lock one step earlier and measure the queue instead of the
        database. `PgLedger` is where a real pool belongs, and SE-1's README
        still says so.
        """
        con = getattr(self._local, "con", None)
        if con is None:
            con = self._new_connection()
            self._local.con = con
        return con

    @contextmanager
    def tx(self):
        """One write transaction. BEGIN IMMEDIATE so writer conflicts surface at
        BEGIN (retried via busy_timeout) instead of at COMMIT."""
        con = self._conn()
        con.execute("BEGIN IMMEDIATE")
        try:
            yield con
        except Exception:
            con.execute("ROLLBACK")
            raise
        else:
            con.execute("COMMIT")

    # -- chart of accounts ---------------------------------------------------
    def open_account(self, account_id: str, kind: str, currency: str,
                     floor_minor: int = 0, overdraft_allowed: bool = False) -> None:
        with self.tx() as con:
            con.execute(
                "INSERT INTO account (id, kind, currency, floor_minor, overdraft_allowed,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (account_id, kind, currency, floor_minor, int(overdraft_allowed), utcnow()))
            con.execute(
                "INSERT INTO account_balance (account_id, balance_minor) VALUES (?, 0)",
                (account_id,))

    # -- the only way money moves -------------------------------------------
    def post(self, entries: Sequence[Entry], actor: str, reason: str,
             request_id: str, con: sqlite3.Connection | None = None,
             effective_on: str | None = None) -> int:
        """Append one balanced transaction; returns txn_id.

        The balance rule is enforced by the database (trigger
        txn_seal_must_balance), not here. The Python check below only buys a
        typed error with a readable message before paying for round trips.

        THE PERIOD GUARD IS ENFORCED HERE, NOT OFFERED. `periods.guard` refuses
        a posting into a closed month, and callers used to have to remember to
        invoke it -- so the control was AVAILABLE rather than enforced, which is
        the same shape as every other bug this repo has found: a rule nothing
        calls is a rule that is not in effect.

        `effective_on` defaults to today when a caller does not say. That is the
        honest default: a posting with no stated effective date IS being made
        today, and treating an unstated date as "exempt from the close" would
        make the guard optional again by omission rather than by argument.

        The guard runs INSIDE the transaction, before the insert. Checking after
        the write and rolling back would leave the sequence allocated and the
        error arriving after the effect.
        """
        if not entries:
            raise Unbalanced("a transaction needs at least two entries")
        sums: dict[str, int] = {}
        for e in entries:
            delta = e.amount_minor if e.direction == "D" else -e.amount_minor
            sums[e.currency] = sums.get(e.currency, 0) + delta
        bad = {c: v for c, v in sums.items() if v != 0}
        if bad:
            raise Unbalanced("unbalanced by currency (debit-positive minor units): " + str(bad))

        ctx = self.tx() if con is None else _null_ctx(con)
        with ctx as c:
            # Imported here rather than at module scope: `periods` imports from
            # this module, and a top-level import would be circular.
            from . import periods

            eff = effective_on or utcnow()[:10]
            periods.guard(c, eff)

            cur = c.execute(
                "INSERT INTO journal_txn (created_at, actor, reason, request_id, sealed)"
                " VALUES (?,?,?,?,0)", (utcnow(), actor, reason, request_id))
            txn_id = cur.lastrowid
            c.executemany(
                "INSERT INTO journal_entry (txn_id, account_id, direction, amount_minor,"
                " currency) VALUES (?,?,?,?,?)",
                [(txn_id, e.account_id, e.direction, e.amount_minor, e.currency)
                 for e in entries])
            self._check_floors(c, {e.account_id for e in entries})
            c.execute("UPDATE journal_txn SET sealed = 1 WHERE id = ?", (txn_id,))
        return txn_id

    def _check_floors(self, con: sqlite3.Connection, account_ids: Iterable[str]) -> None:
        """Invariant 2, checked inside the write transaction so a breach rolls
        the whole posting back. Not yet a DB trigger -- see README 'remaining'."""
        for aid in account_ids:
            row = con.execute(
                "SELECT b.balance_minor AS bal, a.floor_minor AS flr,"
                "       a.overdraft_allowed AS od"
                "  FROM account_balance b JOIN account a ON a.id = b.account_id"
                " WHERE b.account_id = ?", (aid,)).fetchone()
            if row is None:
                raise LedgerError("unknown account " + repr(aid))
            if not row["od"] and row["bal"] < row["flr"]:
                raise FloorBreach(
                    "{}: balance {} below floor {}".format(aid, row["bal"], row["flr"]))

    # -- idempotency ---------------------------------------------------------
    @staticmethod
    def payload_hash(payload: dict) -> str:
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(blob).hexdigest()

    def run_idempotent(self, key: str, payload: dict, work, *,
                       status_code: int = 201) -> tuple[dict, int, bool]:
        """Any unit of work made replay-safe. `work(con) -> response body`.

        `post_idempotent` below does this for a bare posting. Payments need the
        same protection over MORE than a posting -- authorize also inserts a
        payment row, capture also advances a state machine -- and wrapping only
        the posting would leave the rest of the write outside the guarantee.

        This is the generalisation, and it exists because `run_api_load.py`
        found the gap: 3,200 authorizes over HTTP produced **0 idempotency
        keys**. The endpoint was not idempotent at all, so a client that timed
        out and retried placed a SECOND hold on the card. The repository's
        headline idempotency property was real and was being demonstrated on a
        path the payments API did not use.
        """
        h = self.payload_hash(payload)
        existing = self._lookup_key(key)
        if existing is not None:
            return self._replay(existing, h)

        try:
            with self.tx() as con:
                body = work(con)
                con.execute(
                    "INSERT INTO idempotency_key (key, payload_hash, response_json,"
                    " status_code, txn_id, created_at) VALUES (?,?,?,?,?,?)",
                    (key, h, json.dumps(body), status_code,
                     body.get("txn_id"), utcnow()))
        except sqlite3.IntegrityError:
            # Concurrent duplicate -- OR a genuine constraint violation inside
            # `work`, such as a duplicate payment id. Those are different
            # failures and must not be conflated: if the key is absent the
            # integrity error came from the work, and re-raising is the only
            # honest answer.
            existing = self._lookup_key(key)
            if existing is None:
                raise
            return self._replay(existing, h)

        return body, status_code, False

    def post_idempotent(self, key: str, payload: dict, entries: Sequence[Entry],
                        actor: str, reason: str, request_id: str,
                        crash_after_commit=None) -> tuple[dict, int, bool]:
        """Returns (response_body, status_code, replayed).

        The journal write and the stored response commit together, so a crash at
        ANY point after commit -- including before the caller ever sees the
        response -- replays the identical body on retry.
        """
        h = self.payload_hash(payload)
        existing = self._lookup_key(key)
        if existing is not None:
            return self._replay(existing, h)

        try:
            with self.tx() as con:
                txn_id = self.post(entries, actor, reason, request_id, con=con)
                body = {"txn_id": txn_id, "status": "posted", "reason": reason}
                con.execute(
                    "INSERT INTO idempotency_key (key, payload_hash, response_json,"
                    " status_code, txn_id, created_at) VALUES (?,?,?,?,?,?)",
                    (key, h, json.dumps(body), 201, txn_id, utcnow()))
        except sqlite3.IntegrityError:
            # Concurrent duplicate: the other caller won the unique constraint,
            # its transaction is the only effect, and both callers read it back.
            existing = self._lookup_key(key)
            if existing is None:
                raise
            return self._replay(existing, h)

        if crash_after_commit is not None:
            crash_after_commit()   # test hook: process dies, response undelivered
        return body, 201, False

    def _lookup_key(self, key: str):
        return self._conn().execute(
            "SELECT * FROM idempotency_key WHERE key = ?", (key,)).fetchone()

    def _replay(self, row, payload_hash: str) -> tuple[dict, int, bool]:
        if row["payload_hash"] != payload_hash:
            raise IdempotencyConflict(
                "idempotency key reused with a different payload -> 409; the first "
                "caller's result is deliberately NOT returned")
        return json.loads(row["response_json"]), row["status_code"], True

    # -- derived reads -------------------------------------------------------
    def balance(self, account_id: str) -> int:
        """Materialized balance (the cache)."""
        row = self._conn().execute(
            "SELECT balance_minor FROM account_balance WHERE account_id = ?",
            (account_id,)).fetchone()
        return 0 if row is None else row["balance_minor"]

    def balance_as_of(self, account_id: str, ts: str) -> int:
        """Reconstructed from the journal alone -- the audit surface."""
        row = self._conn().execute(
            "SELECT COALESCE(SUM(CASE e.direction WHEN 'D' THEN e.amount_minor"
            "                    ELSE -e.amount_minor END), 0) AS bal"
            "  FROM journal_entry e JOIN journal_txn t ON t.id = e.txn_id"
            " WHERE e.account_id = ? AND t.sealed = 1 AND t.created_at <= ?",
            (account_id, ts)).fetchone()
        return row["bal"]
