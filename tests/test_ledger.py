import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger import invariants
from ledger.core import (FloorBreach, IdempotencyConflict, Ledger, LedgerError,
                         Unbalanced, credit, debit)
from ledger.payments import (FEE_REVENUE, HOLD_ASSET, HOLD_LIAB, NETWORK_RECEIVABLE,
                             PaymentError, authorize, bootstrap_accounts, capture,
                             merchant_payable)

BIG = -10**15


@pytest.fixture
def led(tmp_path):
    lg = Ledger(tmp_path / "t.db")
    lg.open_account("cash", "asset", "USD")
    lg.open_account("revenue", "revenue", "USD", floor_minor=BIG, overdraft_allowed=True)
    lg.open_account("customer", "liability", "USD", floor_minor=BIG, overdraft_allowed=True)
    return lg


def test_balanced_posting_updates_derived_balance(led):
    led.post([debit("cash", 12_50, "USD"), credit("revenue", 12_50, "USD")],
             actor="test", reason="sale", request_id="r1")
    assert led.balance("cash") == 1250
    assert led.balance("revenue") == -1250      # debit-positive convention
    assert invariants.check_all(led._conn()) == []


def test_unbalanced_posting_is_rejected_and_leaves_nothing_behind(led):
    with pytest.raises(Unbalanced):
        led.post([debit("cash", 100, "USD"), credit("revenue", 99, "USD")],
                 actor="test", reason="bad", request_id="r2")
    assert led.balance("cash") == 0
    assert led._conn().execute("SELECT COUNT(*) c FROM journal_txn").fetchone()["c"] == 0


def test_database_rejects_unbalanced_seal_even_if_app_code_is_bypassed(led):
    """The balance rule must live in the DB, not only in core.post()."""
    con = led._conn()
    con.execute("BEGIN IMMEDIATE")
    con.execute("INSERT INTO journal_txn (created_at, actor, reason, request_id, sealed)"
                " VALUES (datetime('now'),'attacker','bypass','r',0)")
    txn = con.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    con.execute("INSERT INTO journal_entry (txn_id, account_id, direction, amount_minor,"
                " currency) VALUES (?,?,?,?,?)", (txn, "cash", "D", 500, "USD"))
    with pytest.raises(Exception, match="unbalanced"):
        con.execute("UPDATE journal_txn SET sealed = 1 WHERE id = ?", (txn,))
    con.execute("ROLLBACK")


def test_journal_is_append_only(led):
    led.post([debit("cash", 100, "USD"), credit("revenue", 100, "USD")],
             actor="t", reason="sale", request_id="r")
    con = led._conn()
    with pytest.raises(Exception, match="append-only"):
        con.execute("UPDATE journal_entry SET amount_minor = 1 WHERE id = 1")
    with pytest.raises(Exception, match="append-only"):
        con.execute("DELETE FROM journal_entry WHERE id = 1")


def test_floats_are_refused_at_the_type_boundary():
    with pytest.raises(LedgerError):
        debit("cash", 12.50, "USD")


def test_floor_breach_rolls_back_whole_posting(led):
    with pytest.raises(FloorBreach):
        led.post([debit("customer", 100, "USD"), credit("cash", 100, "USD")],
                 actor="t", reason="overdraw", request_id="r")
    assert led.balance("cash") == 0
    assert invariants.check_all(led._conn()) == []


def test_idempotent_retry_returns_original_response(led):
    payload = {"amount": 100}
    entries = [debit("cash", 100, "USD"), credit("revenue", 100, "USD")]
    b1, s1, replayed1 = led.post_idempotent("k1", payload, entries, "t", "sale", "r1")
    b2, s2, replayed2 = led.post_idempotent("k1", payload, entries, "t", "sale", "r2")
    assert (b1, s1, replayed1) == (b2, s2, True) or (b1 == b2 and s1 == s2)
    assert replayed1 is False and replayed2 is True
    assert led.balance("cash") == 100          # exactly one effect


def test_crash_between_journal_write_and_response(led):
    """The nastiest retry case: we committed, then died before answering."""
    payload = {"amount": 250}
    entries = [debit("cash", 250, "USD"), credit("revenue", 250, "USD")]

    class Crash(Exception):
        pass

    def die():
        raise Crash("process died after commit, before the client saw a response")

    with pytest.raises(Crash):
        led.post_idempotent("k2", payload, entries, "t", "sale", "r1",
                            crash_after_commit=die)

    body, status, replayed = led.post_idempotent("k2", payload, entries, "t", "sale", "r2")
    assert replayed is True and status == 201
    assert led.balance("cash") == 250          # still exactly one effect
    assert invariants.check_all(led._conn()) == []


def test_same_key_different_payload_is_a_conflict_not_a_replay(led):
    entries = [debit("cash", 100, "USD"), credit("revenue", 100, "USD")]
    led.post_idempotent("k3", {"amount": 100}, entries, "t", "sale", "r1")
    with pytest.raises(IdempotencyConflict):
        led.post_idempotent("k3", {"amount": 999}, entries, "t", "sale", "r2")


def test_balance_as_of_reconstructs_from_journal_alone(led):
    led.post([debit("cash", 100, "USD"), credit("revenue", 100, "USD")],
             actor="t", reason="s1", request_id="r1")
    con = led._conn()
    cutoff = con.execute("SELECT created_at FROM journal_txn WHERE id = 1").fetchone()[0]
    led.post([debit("cash", 700, "USD"), credit("revenue", 700, "USD")],
             actor="t", reason="s2", request_id="r2")
    assert led.balance("cash") == 800
    assert led.balance_as_of("cash", cutoff) == 100


# ------------------------------------------------------------------ payments
def test_authorization_holds_funds_without_paying_the_merchant(led):
    bootstrap_accounts(led, "m1")
    authorize(led, "pay_1", "m1", 10_000, "USD", "req-1")
    assert led.balance(HOLD_ASSET) == 10_000
    assert led.balance(HOLD_LIAB) == -10_000
    assert led.balance(merchant_payable("m1")) == 0     # nothing moved yet
    assert invariants.check_all(led._conn()) == []


def test_partial_capture_releases_only_the_captured_slice(led):
    bootstrap_accounts(led, "m1")
    authorize(led, "pay_2", "m1", 10_000, "USD", "req-1")
    capture(led, "pay_2", 4_000, fee_minor=120, request_id="req-2")

    assert led.balance(HOLD_ASSET) == 6_000              # remainder still held
    assert led.balance(NETWORK_RECEIVABLE) == 4_000
    assert led.balance(merchant_payable("m1")) == -(4_000 - 120)
    assert led.balance(FEE_REVENUE) == -120
    state = led._conn().execute(
        "SELECT state FROM payment WHERE id='pay_2'").fetchone()["state"]
    assert state == "partially_captured"
    assert invariants.check_all(led._conn()) == []


def test_capture_cannot_exceed_remaining_authorization(led):
    bootstrap_accounts(led, "m1")
    authorize(led, "pay_3", "m1", 5_000, "USD", "req-1")
    capture(led, "pay_3", 5_000, fee_minor=0, request_id="req-2")
    with pytest.raises(PaymentError):
        capture(led, "pay_3", 1, fee_minor=0, request_id="req-3")
