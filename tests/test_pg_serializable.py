"""The Postgres port: SERIALIZABLE, the retry loop, and the anomaly it prevents.

Skipped when no Postgres is reachable, so the suite still runs anywhere. When it
IS reachable these are the tests SQLite structurally cannot host -- every one of
them needs two transactions genuinely in flight at once.

The centrepiece is `test_read_committed_admits_the_floor_anomaly`, which
CONSTRUCTS the anomaly rather than hoping load produces one. A concurrency test
that waits for a race to happen by luck is a test that passes on a slow day.
"""
import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

psycopg = pytest.importorskip("psycopg")

from ledger.pg_store import (DSN, PgLedger, RetryStats, SerializationExhausted,
                             is_retryable)


def _reachable():
    """Generous timeout on purpose.

    A 3-second probe reported "no Postgres" against a server that was up and
    still warming its first connection, so fourteen tests skipped silently and
    the suite reported green. A skip that looks like a pass is the worst
    outcome available to a conditional test.
    """
    import time

    # Retry, because the probe is racing a flaky forwarder rather than a down
    # server: WSL's localhost relay accepts the TCP connection and sometimes
    # fails to relay the Postgres startup packet, so a single-shot probe reports
    # "no Postgres" against a server that is demonstrably up. Fourteen tests
    # then skip and the suite reports green -- a skip that reads like a pass is
    # the worst outcome a conditional test can produce.
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

CASH = "t:cash"
REV = "t:revenue"


@pytest.fixture(scope="module")
def led():
    lg = PgLedger(DSN)
    lg.install_schema()
    lg.open_account(REV, "revenue", "USD", floor_minor=-10**15,
                    overdraft_allowed=True)
    lg.open_account(CASH, "asset", "USD", floor_minor=-10**15,
                    overdraft_allowed=True)
    yield lg
    lg.close()


def _entries(amount, account=CASH):
    return [{"account_id": account, "direction": "D",
             "amount_minor": amount, "currency": "USD"},
            {"account_id": REV, "direction": "C",
             "amount_minor": amount, "currency": "USD"}]


# ------------------------------------------------------------- retry policy
def test_only_serialization_states_are_retryable():
    """SQLSTATE, not message text. The strings drift between versions, and a
    floor breach retried forever is a permanent failure wearing a timeout's
    clothes."""
    class E(Exception):
        sqlstate = None

    for state, expected in (("40001", True), ("40P01", True),
                            ("23514", False), ("23505", False), (None, False)):
        e = E()
        e.sqlstate = state
        assert is_retryable(e) is expected


def test_a_constraint_violation_is_raised_not_retried(led):
    """Retrying a check-constraint failure just fails again, more slowly."""
    stats = RetryStats()
    lg = PgLedger(DSN, stats=stats, max_retries=3)
    with pytest.raises(psycopg.errors.CheckViolation):
        lg.post([{"account_id": CASH, "direction": "D",
                  "amount_minor": -5, "currency": "USD"}],
                "t", "negative", "r")
    assert stats.retries == 0, "a permanent failure was retried"
    lg.close()


def test_exhausted_retries_raise_rather_than_pretending_to_succeed():
    """Reporting an exhausted retry as a commit is the one outcome worse than
    failing, because the caller then believes money moved."""
    stats = RetryStats()
    lg = PgLedger(DSN, stats=stats, max_retries=2, base_backoff_ms=0.1)

    def always_conflicts(conn):
        err = psycopg.errors.SerializationFailure("synthetic")
        raise err

    with pytest.raises((SerializationExhausted, psycopg.errors.SerializationFailure)):
        lg.run_serializable(always_conflicts)
    lg.close()


# ------------------------------------------------- the anomaly, constructed
def _floor_probe(isolation, results, account, barrier):
    """Read a balance, decide a withdrawal is legal, then commit.

    Two of these run concurrently against the same account. Each reads a
    balance that permits its own withdrawal; together they overdraw it. This is
    the write-skew anomaly, and it is the exact shape a floor check has.
    """
    conn = psycopg.connect(DSN, autocommit=False)
    conn.isolation_level = getattr(psycopg.IsolationLevel, isolation)
    try:
        bal = conn.execute(
            "SELECT balance_minor FROM account_balance WHERE account_id = %s",
            (account,)).fetchone()[0]
        barrier.wait(timeout=10)          # force the reads to interleave
        if bal >= 100:                    # "there is enough, so this is legal"
            conn.execute(
                "UPDATE account_balance SET balance_minor = balance_minor - 100"
                " WHERE account_id = %s", (account,))
        conn.commit()
        results.append("committed")
    except Exception as exc:                                 # noqa: BLE001
        conn.rollback()
        results.append(getattr(exc, "sqlstate", type(exc).__name__))
    finally:
        conn.close()


def _setup_probe_account(led, name, balance):
    led.open_account(name, "asset", "USD", floor_minor=0, overdraft_allowed=True)
    with psycopg.connect(DSN, autocommit=True) as c:
        c.execute("UPDATE account_balance SET balance_minor = %s"
                  " WHERE account_id = %s", (balance, name))


def test_serializable_refuses_one_of_two_concurrent_withdrawals(led):
    """Both read 150, both would leave 50, together they leave -50. Postgres
    detects the dependency cycle and aborts one."""
    acct = "t:probe_ser"
    _setup_probe_account(led, acct, 150)

    results, barrier = [], threading.Barrier(2)
    threads = [threading.Thread(target=_floor_probe,
                               args=("SERIALIZABLE", results, acct, barrier))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("committed") == 1, results
    assert "40001" in results, results

    with psycopg.connect(DSN, autocommit=True) as c:
        final = c.execute("SELECT balance_minor FROM account_balance"
                          " WHERE account_id = %s", (acct,)).fetchone()[0]
    assert final == 50


def test_read_committed_ADMITS_the_anomaly(led):
    """The negative result that makes the positive one mean something.

    Same code, same load, one word different -- and the account ends BELOW a
    floor that neither transaction ever saw breached. This is what SERIALIZABLE
    is being paid for, and it is why the throughput cost in pg_drift_test.py is
    a price rather than a waste.
    """
    acct = "t:probe_rc"
    _setup_probe_account(led, acct, 150)

    results, barrier = [], threading.Barrier(2)
    threads = [threading.Thread(target=_floor_probe,
                               args=("READ_COMMITTED", results, acct, barrier))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with psycopg.connect(DSN, autocommit=True) as c:
        final = c.execute("SELECT balance_minor FROM account_balance"
                          " WHERE account_id = %s", (acct,)).fetchone()[0]

    assert results.count("committed") == 2, results
    assert final == -50, (
        "READ COMMITTED was expected to admit the write skew; it left {}"
        .format(final))


# ------------------------------------------------------- schema invariants
def test_the_journal_is_append_only(led):
    txn = led.post(_entries(500), "t", "sale", "r-append")
    with psycopg.connect(DSN, autocommit=True) as c:
        with pytest.raises(psycopg.errors.RaiseException):
            c.execute("UPDATE journal_entry SET amount_minor = 1"
                      " WHERE txn_id = %s", (txn,))
        with pytest.raises(psycopg.errors.RaiseException):
            c.execute("DELETE FROM journal_entry WHERE txn_id = %s", (txn,))


def test_an_unbalanced_transaction_cannot_be_sealed(led):
    with psycopg.connect(DSN, autocommit=True) as c:
        txn = c.execute(
            "INSERT INTO journal_txn (actor, reason, request_id)"
            " VALUES ('dba','manual','r') RETURNING id").fetchone()[0]
        c.execute("INSERT INTO journal_entry (txn_id, account_id, direction,"
                  " amount_minor, currency) VALUES (%s,%s,'D',900,'USD')",
                  (txn, CASH))
        with pytest.raises(psycopg.errors.RaiseException, match="unbalanced"):
            c.execute("UPDATE journal_txn SET sealed = TRUE WHERE id = %s",
                      (txn,))


def test_a_sealed_transaction_cannot_be_reopened(led):
    txn = led.post(_entries(300), "t", "sale", "r-reopen")
    with psycopg.connect(DSN, autocommit=True) as c:
        with pytest.raises(psycopg.errors.RaiseException, match="reopened"):
            c.execute("UPDATE journal_txn SET sealed = FALSE WHERE id = %s",
                      (txn,))


def test_an_entry_currency_must_match_its_account(led):
    with psycopg.connect(DSN, autocommit=True) as c:
        txn = c.execute(
            "INSERT INTO journal_txn (actor, reason, request_id)"
            " VALUES ('t','ccy','r') RETURNING id").fetchone()[0]
        with pytest.raises(psycopg.errors.RaiseException, match="currency"):
            c.execute("INSERT INTO journal_entry (txn_id, account_id, direction,"
                      " amount_minor, currency) VALUES (%s,%s,'D',100,'EUR')",
                      (txn, CASH))


def test_the_floor_is_enforced_at_the_seal(led):
    led.open_account("t:strict", "asset", "USD", floor_minor=0,
                     overdraft_allowed=False)
    with pytest.raises(psycopg.errors.RaiseException, match="floor breach"):
        led.post([{"account_id": "t:strict", "direction": "C",
                   "amount_minor": 900, "currency": "USD"},
                  {"account_id": REV, "direction": "D",
                   "amount_minor": 900, "currency": "USD"}],
                 "t", "overdraw", "r-floor")


def test_the_cached_balance_matches_the_journal(led):
    """Scoped to the accounts posted through the ledger, on purpose.

    The write-skew probes above UPDATE account_balance directly, with no
    journal entry behind it -- that is how the anomaly is constructed. I3 then
    correctly reports drift on those two probe accounts, which is the invariant
    doing exactly its job: catching a balance that no journal entry justifies.
    Asserting globally here would be asserting that my own probe did not happen.
    """
    led.post(_entries(777), "t", "sale", "r-derived")
    drift = [p for p in led.check_invariants()
             if p.startswith("I3") and ":probe" not in p]
    assert not drift, drift


def test_i3_catches_a_balance_no_journal_entry_justifies(led):
    """The other half: the probe accounts SHOULD be flagged, and are.

    A cache that can be written without a journal entry is a second source of
    truth. I3 exists to make that impossible to do quietly, and the probes are
    an accidental but genuine test of it.
    """
    flagged = [p for p in led.check_invariants()
               if p.startswith("I3") and ":probe" in p]
    assert flagged, "I3 did not notice a hand-written balance"


# --------------------------------------------------- the hot-row experiment
def test_the_derived_design_conflicts_less_than_the_cached_one(led):
    """The hypothesis pg_drift_test.py raised, as an assertion.

    The README claimed the trigger-maintained balance row was what made
    SERIALIZABLE fail on a hot account. This pins the direction of the effect
    without pinning a magnitude -- the machine is loaded and the retry rate
    moves run to run (cached 77-84%, derived 33-51% across runs), so asserting
    a threshold would be asserting the load.
    """
    import threading

    from ledger.pg_nohotrow import DerivedLedger

    def run(make, stats):
        def work(w):
            lg = make(stats)
            for i in range(12):
                try:
                    lg.post([{"account_id": CASH, "direction": "D",
                              "amount_minor": 100 + i, "currency": "USD"},
                             {"account_id": REV, "direction": "C",
                              "amount_minor": 100 + i, "currency": "USD"}],
                            "t", "hot", "r-{}-{}".format(w, i))
                except Exception:                            # noqa: BLE001
                    pass
            (getattr(lg, "close", None) or lg.base.close)()

        ts = [threading.Thread(target=work, args=(w,)) for w in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

    base = PgLedger(DSN)
    base.install_schema()
    for acct, kind in ((REV, "revenue"), (CASH, "asset")):
        base.open_account(acct, kind, "USD", floor_minor=-10**15,
                          overdraft_allowed=True)
    base.close()
    cached = RetryStats()
    run(lambda st: PgLedger(DSN, stats=st), cached)

    d = DerivedLedger(PgLedger(DSN))
    d.install_schema()
    for acct, kind in ((REV, "revenue"), (CASH, "asset")):
        d.open_account(acct, kind, "USD", floor_minor=-10**15,
                       overdraft_allowed=True)
    d.base.close()
    derived = RetryStats()
    run(lambda st: DerivedLedger(PgLedger(DSN, stats=st)), derived)

    assert derived.retry_rate < cached.retry_rate, (
        "derived {:.1%} vs cached {:.1%}".format(
            derived.retry_rate, cached.retry_rate))
    assert derived.exhausted <= cached.exhausted


def test_the_derived_ledger_is_still_append_only(led):
    """Dropping the cache must not drop the invariant. Corrections stay
    reversing entries."""
    from ledger.pg_nohotrow import DerivedLedger

    d = DerivedLedger(PgLedger(DSN))
    d.install_schema()
    for acct, kind in ((REV, "revenue"), (CASH, "asset")):
        d.open_account(acct, kind, "USD", floor_minor=-10**15,
                       overdraft_allowed=True)
    txn = d.post([{"account_id": CASH, "direction": "D",
                   "amount_minor": 500, "currency": "USD"},
                  {"account_id": REV, "direction": "C",
                   "amount_minor": 500, "currency": "USD"}],
                 "t", "sale", "r-append-d")
    with psycopg.connect(DSN, autocommit=True) as c:
        with pytest.raises(psycopg.errors.RaiseException):
            c.execute("UPDATE d_journal_entry SET amount_minor = 1"
                      " WHERE txn_id = %s", (txn,))
    assert d.balance(CASH) == 500
    d.base.close()
