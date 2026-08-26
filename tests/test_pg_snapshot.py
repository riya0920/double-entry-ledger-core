"""Snapshot-backed balances on Postgres: correctness, staleness, and the drain.

The invariant every test here defends is one sentence: a checkpoint is a cache,
so deleting every row of `d_balance_snapshot` must change no answer. Everything
else is a way of trying to break that.

Skips when Postgres is unreachable, using the same generous probe as
`test_pg_serializable.py` -- a skip that reads like a pass is the worst outcome
a conditional test can produce.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

psycopg = pytest.importorskip("psycopg")

from ledger.pg_nohotrow import DerivedLedger
from ledger.pg_snapshot import SnapshottedLedger, StaleSnapshot
from ledger.pg_store import DSN, PgLedger


def _reachable() -> bool:
    for _ in range(3):
        try:
            with psycopg.connect(DSN, connect_timeout=10) as c:
                c.execute("select 1")
            return True
        except Exception:                                    # noqa: BLE001
            time.sleep(2)
    return False


pytestmark = pytest.mark.skipif(
    not _reachable(),
    reason="no Postgres at {} -- set LEDGER_PG_DSN".format(DSN))

CASH = "ts:cash"
REV = "ts:revenue"


def _entries(amount: int) -> list[dict]:
    return [{"account_id": CASH, "direction": "D",
             "amount_minor": amount, "currency": "USD"},
            {"account_id": REV, "direction": "C",
             "amount_minor": amount, "currency": "USD"}]


@pytest.fixture()
def snap():
    base = PgLedger(dsn=DSN)
    derived = DerivedLedger(base)
    derived.install_schema()
    s = SnapshottedLedger(derived)
    s.install_schema()
    # install_schema() rebuilt the journal, so any surviving checkpoint points
    # past the end of it. This is the bug StaleSnapshot exists for.
    s.clear()
    derived.open_account(CASH, "asset", "USD")
    derived.open_account(REV, "revenue", "USD", overdraft_allowed=True)
    return s


def test_checkpoint_does_not_change_the_answer(snap):
    for i in range(40):
        snap.post(_entries(100 + i), "t", "seed", "s-{}".format(i))
    truth = snap.derived.balance(CASH)

    snap.take_snapshot()
    assert snap.balance(CASH) == truth

    # And after more postings land on top of the checkpoint.
    for i in range(15):
        snap.post(_entries(7), "t", "after", "a-{}".format(i))
    assert snap.balance(CASH) == snap.derived.balance(CASH)


def test_deleting_every_checkpoint_changes_nothing(snap):
    for i in range(30):
        snap.post(_entries(50), "t", "seed", "d-{}".format(i))
    snap.take_snapshot()
    with_cache = snap.balance(CASH)

    assert snap.clear() > 0
    assert snap.balance(CASH) == with_cache


def test_checkpoint_reduces_replay_depth(snap):
    for i in range(25):
        snap.post(_entries(10), "t", "seed", "r-{}".format(i))
    assert snap.replay_depth(CASH) == 25

    snap.take_snapshot()
    assert snap.replay_depth(CASH) == 0

    snap.post(_entries(10), "t", "after", "r-after")
    assert snap.replay_depth(CASH) == 1


def test_undrained_snapshot_loses_an_in_flight_entry(snap):
    """The hazard, forced rather than raced.

    A holds a LOW entry id and stays open; B commits a HIGHER one. An undrained
    snapshot reads MAX(id), sees B, cannot see A, and writes a watermark ABOVE
    A's id with a sum that excludes it. A then commits below the watermark, so
    the delta skips it too, and the entry is in neither half.
    """
    for i in range(5):
        snap.post(_entries(100), "t", "seed", "g-{}".format(i))

    conn_a = psycopg.connect(DSN, autocommit=False)
    try:
        txn = conn_a.execute(
            "INSERT INTO d_journal_txn (actor, reason, request_id)"
            " VALUES ('t','gap','gap-a') RETURNING id").fetchone()[0]
        id_a = conn_a.execute(
            "INSERT INTO d_journal_entry (txn_id, account_id, direction,"
            " amount_minor, currency) VALUES (%s,%s,'D',%s,'USD') RETURNING id",
            (txn, CASH, 7777)).fetchone()[0]
        conn_a.execute("UPDATE d_journal_txn SET sealed = TRUE WHERE id = %s",
                       (txn,))

        snap.post(_entries(11), "t", "gap-b", "gap-b")   # commits a higher id

        report = snap.take_snapshot(drain=False)
        assert id_a <= report["watermark"], (
            "the interleaving did not set up: A's id must fall below the "
            "watermark for the hazard to exist")
    finally:
        conn_a.commit()
        conn_a.close()

    # The entry is real and the checkpoint has lost it, by exactly its amount.
    assert snap.balance(CASH) == snap.derived.balance(CASH) - 7777


def test_draining_keeps_the_in_flight_entry(snap):
    """The same interleaving with the drain on. This is the fix."""
    for i in range(5):
        snap.post(_entries(100), "t", "seed", "k-{}".format(i))

    conn_a = psycopg.connect(DSN, autocommit=False)
    txn = conn_a.execute(
        "INSERT INTO d_journal_txn (actor, reason, request_id)"
        " VALUES ('t','gap','keep-a') RETURNING id").fetchone()[0]
    conn_a.execute(
        "INSERT INTO d_journal_entry (txn_id, account_id, direction,"
        " amount_minor, currency) VALUES (%s,%s,'D',%s,'USD')",
        (txn, CASH, 7777))
    conn_a.execute("UPDATE d_journal_txn SET sealed = TRUE WHERE id = %s",
                   (txn,))
    snap.post(_entries(11), "t", "gap-b", "keep-b")

    def commit_later():
        time.sleep(0.4)
        conn_a.commit()
        conn_a.close()

    t = threading.Thread(target=commit_later)
    t.start()
    report = snap.take_snapshot(drain=True)      # blocks until A is done
    t.join()

    assert report["drained"] is True
    assert snap.balance(CASH) == snap.derived.balance(CASH)


def test_stale_checkpoint_is_refused_not_trusted(snap):
    """A watermark past the end of the journal means the journal was rebuilt.

    Found by running the benchmark twice: `DERIVED_SCHEMA` recreates the journal
    but not `d_balance_snapshot`, so checkpoints from the previous run survived
    with watermarks far above every live entry. The delta matched nothing and
    `balance()` returned the old total -- wrong by 5x, silently.
    """
    for i in range(10):
        snap.post(_entries(100), "t", "seed", "st-{}".format(i))
    snap.take_snapshot()

    with psycopg.connect(DSN, autocommit=True) as c:
        head = c.execute(
            "SELECT MAX(id) FROM d_journal_entry").fetchone()[0]
        c.execute(
            "INSERT INTO d_balance_snapshot (account_id, watermark_id,"
            " balance_minor) VALUES (%s,%s,%s)", (CASH, head + 10_000, 999_999))

    with pytest.raises(StaleSnapshot):
        snap.balance(CASH)

    # Clearing is the documented recovery, and it restores the true answer.
    snap.clear()
    assert snap.balance(CASH) == snap.derived.balance(CASH)


def test_writes_are_not_routed_through_the_checkpoint(snap):
    """`post` must reach DerivedLedger untouched -- a checkpoint that changed
    the write path would hand back the contention the derived design removed."""
    calls = []
    real = snap.derived.post

    def spy(*a, **kw):
        calls.append(a)
        return real(*a, **kw)

    snap.derived.post = spy
    snap.post(_entries(500), "t", "spy", "spy-1")
    assert len(calls) == 1
