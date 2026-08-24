"""Period close, consolidated reporting, and authorization adjustments."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger import invariants, periods
from ledger.adjustments import adjust, authorized_amount
from ledger.core import Ledger, credit, debit
from ledger.payments import bootstrap_accounts, capture, authorize
from ledger.reporting import RateMissing, RateSet, consolidate, translate

BIG = -10**15


@pytest.fixture
def led(tmp_path):
    lg = Ledger(tmp_path / "p.db")
    bootstrap_accounts(lg, "m1")
    periods.install(lg._conn())
    return lg


# ------------------------------------------------------------- periods
def test_an_unknown_period_is_open(led):
    """Default-closed means a brand new ledger refuses its own first posting."""
    assert periods.status(led._conn(), "2026-01") == "open"


def test_closing_requires_a_named_approver(led):
    for bad in ("", "   ", None):
        with pytest.raises(ValueError, match="approver"):
            periods.close_period(led._conn(), "2026-01", bad)


def test_a_posting_into_a_closed_period_is_refused(led):
    con = led._conn()
    periods.close_period(con, "2026-01", "controller")
    with pytest.raises(periods.PeriodClosed, match="closed"):
        periods.guard(con, "2026-01-15")


def test_an_open_period_still_accepts_backdated_postings(led):
    """Backdating is legal until the period closes. Forbidding it outright is
    why people post to the wrong period instead."""
    con = led._conn()
    txn = led.post([debit("network:receivable", 1_000, "USD"),
                    credit("merchant:m1:payable", 1_000, "USD")],
                   "t", "backdated", "r-bd")
    periods.record_effective_date(con, txn, "2026-01-15")
    periods.close_period(con, "2026-01", "controller")
    row = [r for r in periods.report(con) if r["period"] == "2026-01"][0]
    assert row["postings"] == 1


def test_reopening_requires_both_an_approver_and_a_reason(led):
    con = led._conn()
    periods.close_period(con, "2026-01", "controller")
    with pytest.raises(ValueError, match="approver"):
        periods.reopen_period(con, "2026-01", "", "because")
    with pytest.raises(ValueError, match="reason"):
        periods.reopen_period(con, "2026-01", "cfo", "")


def test_reopening_records_who_and_why(led):
    con = led._conn()
    periods.close_period(con, "2026-01", "controller")
    periods.reopen_period(con, "2026-01", "cfo", "material fee error")
    row = [r for r in periods.report(con) if r["period"] == "2026-01"][0]
    assert row["status"] == "open"
    assert row["reopened_by"] == "cfo"
    assert "material" in row["reopen_reason"]


def test_adjust_forward_refuses_when_the_target_is_also_closed(led):
    con = led._conn()
    periods.close_period(con, "2026-01", "controller")
    periods.close_period(con, "2026-02", "controller")
    with pytest.raises(periods.PeriodClosed):
        periods.adjust_forward("2026-01-15", con, "2026-02-03")


def test_restate_reopens_the_original_period(led):
    """The two policies are not interchangeable: this one changes a published
    number, which is why it needs the approval."""
    con = led._conn()
    periods.close_period(con, "2026-01", "controller")
    out = periods.restate("2026-01-15", con, "cfo", "material restatement")
    assert out == "2026-01-15"
    assert not periods.is_closed(con, "2026-01")


def test_adjust_forward_leaves_the_closed_period_alone(led):
    con = led._conn()
    periods.close_period(con, "2026-01", "controller")
    out = periods.adjust_forward("2026-01-15", con, "2026-02-03")
    assert out == "2026-02-03"
    assert periods.is_closed(con, "2026-01"), "January was reopened by a forward adjustment"


# ----------------------------------------------------------- reporting
def test_a_missing_rate_raises_rather_than_defaulting_to_one(led):
    rates = RateSet("USD", closing={}, average={})
    led.open_account("eur:cash", "asset", "EUR", floor_minor=BIG,
                     overdraft_allowed=True)
    with pytest.raises(RateMissing, match="refusing to guess"):
        consolidate(led._conn(), rates)


def test_the_reporting_currency_translates_at_one(led):
    rates = RateSet("USD")
    assert rates.rate_for("USD", "asset") == 1


def test_balance_sheet_uses_closing_and_income_uses_average(led):
    """Translating revenue at the closing rate makes a month's earnings move
    because the currency moved on the last day."""
    rates = RateSet("USD", closing={"EUR": "1.10"}, average={"EUR": "1.05"})
    from decimal import Decimal

    assert rates.rate_for("EUR", "asset") == Decimal("1.10")
    assert rates.rate_for("EUR", "revenue") == Decimal("1.05")


def test_translation_rounds_half_even(led):
    """Half-up accumulates a one-directional bias, and consolidation rounds once
    per account -- so the bias grows with the number of accounts."""
    from decimal import Decimal

    assert translate(5, Decimal("0.5")) == 2      # 2.5 -> 2, not 3
    assert translate(15, Decimal("0.5")) == 8     # 7.5 -> 8


def test_the_cta_makes_the_consolidation_balance(led):
    led.open_account("eur:cash", "asset", "EUR", floor_minor=BIG,
                     overdraft_allowed=True)
    led.open_account("eur:rev", "revenue", "EUR", floor_minor=BIG,
                     overdraft_allowed=True)
    led.post([debit("eur:cash", 100_000, "EUR"),
              credit("eur:rev", 100_000, "EUR")], "t", "sale", "r1")

    rates = RateSet("USD", closing={"EUR": "1.10"}, average={"EUR": "1.05"})
    res = consolidate(led._conn(), rates)
    assert res["balances_after_cta"]
    # Two different rates on the two sides means the residual is real.
    assert res["cumulative_translation_adjustment_minor"] != 0


def test_a_single_currency_book_has_no_cta(led):
    """The control on the control: if one rate is used for everything the CTA is
    zero, so a non-zero CTA is evidence the two-rate rule actually applied."""
    led.post([debit("network:receivable", 5_000, "USD"),
              credit("merchant:m1:payable", 5_000, "USD")], "t", "s", "r")
    res = consolidate(led._conn(), RateSet("USD"))
    assert res["cumulative_translation_adjustment_minor"] == 0


# --------------------------------------------------------- adjustments
def test_an_increment_holds_more_without_paying_the_merchant(led):
    from ledger.payments import HOLD_ASSET, merchant_payable

    authorize(led, "hotel", "m1", 10_000, "USD", "r1")
    before_payable = led.balance(merchant_payable("m1"))
    adj = adjust(led, "hotel", 5_000, "r2")

    assert adj.kind == "incremental"
    assert adj.authorized_after == 15_000
    assert led.balance(HOLD_ASSET) == 15_000
    assert led.balance(merchant_payable("m1")) == before_payable
    assert not invariants.check_all(led._conn())


def test_a_decrement_releases_part_of_the_hold(led):
    from ledger.payments import HOLD_ASSET

    authorize(led, "fuel", "m1", 10_000, "USD", "r1")
    adj = adjust(led, "fuel", -6_000, "r2")
    assert adj.kind == "decremental"
    assert adj.authorized_after == 4_000
    assert led.balance(HOLD_ASSET) == 4_000


def test_a_decrement_cannot_go_below_what_was_already_captured(led):
    """Partial capture makes this reachable, and the book would then say we hold
    less than we have already paid out."""
    authorize(led, "p", "m1", 10_000, "USD", "r1")
    capture(led, "p", 8_000, 0, "r2")
    with pytest.raises(Exception, match="already captured|below"):
        adjust(led, "p", -6_000, "r3")


def test_an_adjustment_to_zero_is_refused_in_favour_of_a_void(led):
    authorize(led, "p", "m1", 10_000, "USD", "r1")
    with pytest.raises(Exception, match="void it instead"):
        adjust(led, "p", -10_000, "r2")


def test_a_zero_adjustment_is_refused(led):
    authorize(led, "p", "m1", 10_000, "USD", "r1")
    with pytest.raises(Exception, match="not an adjustment"):
        adjust(led, "p", 0, "r2")


def test_a_retried_adjustment_replays(led):
    authorize(led, "p", "m1", 10_000, "USD", "r1")
    a = adjust(led, "p", 2_000, "r2", idempotency_key="adj-1")
    b = adjust(led, "p", 2_000, "r2", idempotency_key="adj-1")
    assert a.txn_id == b.txn_id
    assert authorized_amount(led, "p") == 12_000


def test_adjustments_keep_the_book_balanced(led):
    authorize(led, "p", "m1", 10_000, "USD", "r1")
    for d in (3_000, -1_000, 500, -2_000):
        adjust(led, "p", d, "r-{}".format(d))
    assert authorized_amount(led, "p") == 10_500
    assert not invariants.check_all(led._conn())
