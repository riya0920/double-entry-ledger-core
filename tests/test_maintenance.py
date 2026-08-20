"""Expiry, FX revaluation, snapshots, and the floor trigger."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger import invariants
from ledger.core import Ledger, credit, debit, utcnow
from ledger.fx import FX_GAIN_LOSS, FX_POSITION, conversion_entries
from ledger.maintenance import (FX_UNREALISED, balance_as_of_snapshotted,
                                expire_authorizations, reverse_revaluation,
                                revalue_fx_position, take_snapshot)
from ledger.payments import (HOLD_ASSET, authorize, bootstrap_accounts, capture)

BIG = -10**15


@pytest.fixture
def led(tmp_path):
    lg = Ledger(tmp_path / "m.db")
    bootstrap_accounts(lg, "m1")
    for acct, kind, ccy in [
            (FX_POSITION.format("EUR"), "asset", "EUR"),
            (FX_POSITION.format("USD"), "asset", "USD"),
            (FX_GAIN_LOSS, "revenue", "USD"),
            (FX_UNREALISED, "asset", "USD"),
            ("cust:eur", "liability", "EUR"),
            ("cust:usd", "liability", "USD")]:
        lg.open_account(acct, kind, ccy, floor_minor=BIG, overdraft_allowed=True)
    return lg


def _age(led, payment_id, days=30):
    led._conn().execute(
        "UPDATE payment SET created_at = ? WHERE id = ?",
        ((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),
         payment_id))


# ------------------------------------------------------------------- expiry
def test_stale_authorization_is_expired_and_the_hold_released(led):
    authorize(led, "old", "m1", 10_000, "USD", "r1")
    _age(led, "old")
    assert led.balance(HOLD_ASSET) == 10_000

    res = expire_authorizations(led)
    assert res.expired == ["old"]
    assert res.released_minor == 10_000
    assert led.balance(HOLD_ASSET) == 0
    assert not invariants.check_all(led._conn())


def test_fresh_authorization_is_left_alone(led):
    authorize(led, "new", "m1", 5_000, "USD", "r1")
    assert expire_authorizations(led).expired == []
    assert led.balance(HOLD_ASSET) == 5_000


def test_expiry_only_releases_the_uncaptured_remainder(led):
    authorize(led, "part", "m1", 10_000, "USD", "r1")
    capture(led, "part", 4_000, 0, "r2")
    _age(led, "part")

    res = expire_authorizations(led)
    assert res.released_minor == 6_000
    assert led.balance(HOLD_ASSET) == 0
    assert not invariants.check_all(led._conn())


def test_expiry_is_distinguishable_from_a_void_in_the_journal(led):
    """A void is a decision; an expiry is a timeout. A book full of expiries
    means captures are not happening, and that is invisible if both say void."""
    authorize(led, "old", "m1", 1_000, "USD", "r1")
    _age(led, "old")
    expire_authorizations(led)

    reasons = [r[0] for r in led._conn().execute(
        "SELECT reason FROM journal_txn").fetchall()]
    assert any(r.startswith("expire:") for r in reasons)
    assert not any(r.startswith("void:") for r in reasons)


def test_expiry_is_idempotent_across_repeated_runs(led):
    authorize(led, "old", "m1", 3_000, "USD", "r1")
    _age(led, "old")
    first = expire_authorizations(led)
    second = expire_authorizations(led)
    assert first.expired == ["old"] and second.expired == []
    assert led.balance(HOLD_ASSET) == 0


# -------------------------------------------------------------- revaluation
def _build_eur_position(led, amount=100_000):
    entries, _ = conversion_entries("cust:eur", "cust:usd", amount,
                                    "EUR", "USD", "1.10")
    led.post(entries, "t", "fx-trade", "r-fx")


def test_revaluation_recognises_unrealised_pnl(led):
    _build_eur_position(led)
    res = revalue_fx_position(led, "EUR", booked_rate="1.10", current_rate="1.15")
    assert res.unrealised_minor > 0
    assert led.balance(FX_UNREALISED) == res.unrealised_minor
    assert not invariants.check_all(led._conn())


def test_revaluation_does_not_touch_the_position_itself(led):
    """Revaluation changes what a position is worth, not how much of it there
    is. Adjusting the position leg would create foreign currency out of a rate."""
    _build_eur_position(led)
    before = led.balance(FX_POSITION.format("EUR"))
    revalue_fx_position(led, "EUR", "1.10", "1.15")
    assert led.balance(FX_POSITION.format("EUR")) == before


def test_a_falling_rate_produces_a_loss(led):
    _build_eur_position(led)
    res = revalue_fx_position(led, "EUR", "1.10", "1.05")
    assert res.unrealised_minor < 0
    assert led.balance(FX_UNREALISED) < 0


def test_reversing_a_revaluation_returns_to_zero(led):
    """Without the reversal, the next period recognises the same movement from
    the original booked rate again and the P&L is counted twice."""
    _build_eur_position(led)
    res = revalue_fx_position(led, "EUR", "1.10", "1.15")
    reverse_revaluation(led, res)
    assert led.balance(FX_UNREALISED) == 0
    assert led.balance(FX_GAIN_LOSS) == 0
    assert not invariants.check_all(led._conn())


def test_no_position_means_no_entry(led):
    res = revalue_fx_position(led, "EUR", "1.10", "1.15")
    assert res.txn_id is None and res.unrealised_minor == 0


# ---------------------------------------------------------------- snapshots
def test_snapshot_result_matches_a_full_scan(led):
    """A snapshot is a cache. If it can change an answer it is a second source
    of truth, and the journal stops being the ledger."""
    for i in range(6):
        led.post([debit("network:receivable", 1_000, "USD"),
                  credit("merchant:m1:payable", 1_000, "USD")],
                 "t", "s{}".format(i), "r{}".format(i))

    mid = led._conn().execute(
        "SELECT created_at FROM journal_txn ORDER BY id LIMIT 1 OFFSET 2"
    ).fetchone()[0]
    take_snapshot(led, mid)

    for i in range(6, 10):
        led.post([debit("network:receivable", 500, "USD"),
                  credit("merchant:m1:payable", 500, "USD")],
                 "t", "s{}".format(i), "r{}".format(i))

    now = utcnow()
    for acct in ("network:receivable", "merchant:m1:payable"):
        assert (balance_as_of_snapshotted(led, acct, now)
                == led.balance_as_of(acct, now)), acct
        assert (balance_as_of_snapshotted(led, acct, mid)
                == led.balance_as_of(acct, mid)), acct


def test_deleting_snapshots_changes_no_answer(led):
    led.post([debit("network:receivable", 2_000, "USD"),
              credit("merchant:m1:payable", 2_000, "USD")], "t", "s", "r")
    take_snapshot(led)
    now = utcnow()
    before = balance_as_of_snapshotted(led, "network:receivable", now)
    led._conn().execute("DELETE FROM balance_snapshot")
    assert balance_as_of_snapshotted(led, "network:receivable", now) == before


# ------------------------------------------------------------ floor trigger
def test_floor_is_enforced_by_the_database_not_only_by_python(led):
    """The Python check protects callers who go through core.post(). The trigger
    protects the DATA from anyone who does not -- including a DBA at a prompt."""
    led.open_account("strict", "asset", "USD", floor_minor=0,
                     overdraft_allowed=False)
    con = led._conn()
    con.execute("BEGIN IMMEDIATE")
    con.execute("INSERT INTO journal_txn (created_at, actor, reason, request_id,"
                " sealed) VALUES (datetime('now'),'dba','manual','r',0)")
    txn = con.execute("SELECT last_insert_rowid() AS i").fetchone()["i"]
    # A balanced pair that takes `strict` below its floor. Entries insert fine;
    # the transaction is not part of the ledger until it seals, and that is
    # where the floor is checked.
    con.execute("INSERT INTO journal_entry (txn_id, account_id, direction,"
                " amount_minor, currency) VALUES (?,?,?,?,?)",
                (txn, "strict", "C", 100, "USD"))
    con.execute("INSERT INTO journal_entry (txn_id, account_id, direction,"
                " amount_minor, currency) VALUES (?,?,?,?,?)",
                (txn, "network:receivable", "D", 100, "USD"))
    with pytest.raises(Exception, match="floor breach"):
        con.execute("UPDATE journal_txn SET sealed = 1 WHERE id = ?", (txn,))
    con.execute("ROLLBACK")
    assert led.balance("strict") == 0


def test_a_transaction_that_dips_and_recovers_within_itself_is_allowed(led):
    """A floor is a property of a completed transaction, not of one leg. A
    per-entry check would reject this depending on leg ordering."""
    led.open_account("dip", "asset", "USD", floor_minor=0, overdraft_allowed=False)
    led.post([credit("dip", 100, "USD"),
              debit("dip", 100, "USD")], "t", "dip-and-recover", "r-dip")
    assert led.balance("dip") == 0


def test_overdraft_allowed_accounts_are_unaffected_by_the_trigger(led):
    led.open_account("loose", "asset", "USD", floor_minor=0,
                     overdraft_allowed=True)
    led.post([credit("loose", 5_000, "USD"),
              debit("network:receivable", 5_000, "USD")], "t", "od", "r-od")
    assert led.balance("loose") == -5_000
