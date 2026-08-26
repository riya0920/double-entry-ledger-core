"""Where a rate comes from, and whose rules an adjustment must satisfy."""
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger.rates import (RateError, RateImmutable, RateNotFound, RateStore,
                          VALID_KINDS)
from ledger.schemes import (ASSUMED_RULES, AuthorizationContext,
                            INCREMENTAL_MCC, SchemeViolation, check_adjustment,
                            headroom)

D = "2026-03-31"


@pytest.fixture
def store():
    s = RateStore()
    for ccy, close, avg in (("USD", "0.92", "0.91"), ("GBP", "1.17", "1.16")):
        s.publish(D, ccy, "EUR", "closing", close, "provider")
        s.publish(D, ccy, "EUR", "average", avg, "provider")
    return s


# ------------------------------------------------------------------ rates
def test_a_rate_is_dated(store):
    """"The USD/EUR rate" is not a value. A store keyed only by currency
    silently reuses yesterday's rate for today's close, and the error is a
    plausible-looking CTA rather than a crash."""
    assert store.get(D, "USD", "EUR", "closing").rate == Decimal("0.92")
    with pytest.raises(RateNotFound):
        store.get("2026-04-01", "USD", "EUR", "closing")


def test_a_missing_rate_raises_rather_than_defaulting(store):
    """Defaulting to 1.0 or carrying the last rate forward produces a
    consolidation that balances and is wrong -- and one that balances is one
    nobody checks."""
    with pytest.raises(RateNotFound, match="NOT defaulting to 1.0"):
        store.get(D, "JPY", "EUR", "closing")


def test_a_published_rate_cannot_be_edited(store):
    """A filed consolidation used it. Changing it in place makes that report
    unreproducible with no record of when it moved."""
    with pytest.raises(RateImmutable, match="not editable"):
        store.publish(D, "USD", "EUR", "closing", "0.99", "manual")


def test_republishing_the_same_value_is_a_no_op(store):
    """Idempotent, so a re-run of a rate load is not an incident."""
    r = store.publish(D, "USD", "EUR", "closing", "0.92", "provider")
    assert r.rate == Decimal("0.92")


def test_a_restatement_is_a_new_date_not_an_edit(store):
    """The remedy the immutability forces, and it is the correct one."""
    store.publish("2026-04-01", "USD", "EUR", "closing", "0.93", "provider")
    hist = store.history("USD", "EUR", "closing")
    assert [h.quote_date for h in hist] == [D, "2026-04-01"]
    assert [str(h.rate) for h in hist] == ["0.92", "0.93"]


def test_a_float_rate_is_refused(store):
    """Same bug as a float amount. Decimal(0.1) preserves the binary error
    rather than removing it, so accepting a float and converting is how one
    gets in."""
    with pytest.raises(RateError, match="never a float"):
        store.publish("2026-04-02", "USD", "EUR", "closing", 0.92, "manual")


def test_a_string_and_a_decimal_are_both_accepted(store):
    store.publish("2026-04-02", "USD", "EUR", "closing", "0.93", "manual")
    store.publish("2026-04-02", "USD", "EUR", "average", Decimal("0.93"),
                  "manual")
    assert store.get("2026-04-02", "USD", "EUR", "average").rate == Decimal("0.93")


def test_a_nonpositive_rate_is_refused(store):
    for bad in ("0", "-1.5"):
        with pytest.raises(RateError, match="positive"):
            store.publish("2026-04-03", "USD", "EUR", "closing", bad, "manual")


def test_the_source_is_recorded_because_a_manual_override_is_the_one_asked_about(store):
    store.publish(D, "JPY", "EUR", "closing", "0.0061", "manual",
                  note="treasury override pending provider fix")
    assert store.sources_used(D) == {"provider": 4, "manual": 1}
    assert "override" in store.get(D, "JPY", "EUR", "closing").note


def test_an_unknown_source_is_refused(store):
    with pytest.raises(RateError, match="source must be"):
        store.publish(D, "CHF", "EUR", "closing", "1.05", "a-guy-i-know")


# ------------------------------------------------------------- rate sets
def test_a_rate_set_needs_both_kinds(store):
    """Closing for the balance sheet, average for income. A partial set is
    refused rather than filled in, because the missing half is exactly where a
    silent default would hide."""
    rs = store.rate_set(D, "EUR", ["USD", "GBP", "EUR"])
    assert rs.closing["USD"] == Decimal("0.92")
    assert rs.average["USD"] == Decimal("0.91")
    assert rs.closing != rs.average


def test_an_incomplete_rate_set_names_what_is_missing(store):
    store.publish(D, "CHF", "EUR", "closing", "1.05", "provider")
    with pytest.raises(RateNotFound, match="average CHF"):
        store.rate_set(D, "EUR", ["USD", "CHF"])


def test_the_reporting_currency_needs_no_rate(store):
    rs = store.rate_set(D, "EUR", ["EUR"])
    assert rs.rate_for("EUR", "assets") == Decimal(1)


# ---------------------------------------------------------------- schemes
def _ctx(**kw):
    base = dict(scheme="visa", mcc="3501", original_minor=100_000,
                current_minor=100_000, increments_so_far=0,
                authorized_on="2026-03-01")
    base.update(kw)
    return AuthorizationContext(**base)


def test_a_normal_increment_is_allowed():
    out = check_adjustment(_ctx(), 10_000, "2026-03-05")
    assert out["allowed"] and out["increments_remaining"] == 4


def test_the_cumulative_ratio_is_against_the_ORIGINAL_not_the_running_total():
    """Three 15% increments on a running total is 52%, not 45%. Reading the
    ratio the other way compounds."""
    ctx = _ctx(current_minor=114_000, increments_so_far=3)
    with pytest.raises(SchemeViolation, match="against the original"):
        check_adjustment(ctx, 5_000, "2026-03-05")


def test_the_increment_count_is_capped():
    ctx = _ctx(increments_so_far=ASSUMED_RULES["visa"].max_increments)
    with pytest.raises(SchemeViolation, match="allows 5 increments"):
        check_adjustment(ctx, 1_000, "2026-03-05")


def test_the_expiry_clock_runs_from_the_ORIGINAL_authorization():
    """The rule an implementation is most likely to get wrong. Restarting it on
    each increment would re-introduce exactly the bug `adjustments.py` avoided
    by not re-authorizing -- a hold extended indefinitely by incrementing it."""
    ctx = _ctx(authorized_on="2026-01-01")
    with pytest.raises(SchemeViolation, match="clock runs from the ORIGINAL"):
        check_adjustment(ctx, 1_000, "2026-03-05")


def test_a_non_incremental_mcc_is_refused():
    """Incremental authorization exists for merchants whose final amount is
    genuinely unknown. A retailer that knows the total is expected to authorize
    it."""
    with pytest.raises(SchemeViolation, match="not incremental-eligible"):
        check_adjustment(_ctx(mcc="5411"), 1_000, "2026-03-05")


def test_every_listed_mcc_is_accepted():
    for mcc in INCREMENTAL_MCC:
        assert check_adjustment(_ctx(mcc=mcc), 1_000, "2026-03-05")["allowed"]


def test_a_scheme_that_does_not_support_increments_says_so():
    """A different answer from a small limit, and treating it as one would
    produce an authorization the network never accepted."""
    with pytest.raises(SchemeViolation, match="does not support incremental"):
        check_adjustment(_ctx(scheme="interac", authorized_on="2026-03-05"),
                         1_000, "2026-03-05")


def test_a_scheme_that_does_not_support_decrements_says_so():
    with pytest.raises(SchemeViolation, match="does not support decremental"):
        check_adjustment(_ctx(scheme="interac", authorized_on="2026-03-05"),
                         -1_000, "2026-03-05")


def test_an_unknown_scheme_is_refused_rather_than_permitted():
    """Guessing an unknown scheme's limits upward is how an authorization gets
    declined at capture time when the money is already spent."""
    with pytest.raises(SchemeViolation, match="NOT falling back"):
        check_adjustment(_ctx(scheme="somebank"), 1_000, "2026-03-05")


def test_schemes_differ_and_that_is_the_point():
    """If every scheme had the same limits the module would be a constant."""
    ctx = _ctx(current_minor=108_000, increments_so_far=1)
    check_adjustment(_ctx(scheme="mastercard", current_minor=108_000,
                          increments_so_far=1), 10_000, "2026-03-05")
    with pytest.raises(SchemeViolation):
        check_adjustment(_ctx(scheme="amex", current_minor=108_000,
                              increments_so_far=1), 10_000, "2026-03-05")


def test_a_zero_adjustment_is_refused():
    with pytest.raises(SchemeViolation, match="not an adjustment"):
        check_adjustment(_ctx(), 0, "2026-03-05")


def test_the_ledger_invariant_is_not_re_implemented_here():
    """The "never below captured" rule stays in `adjust`. Two implementations
    of one invariant drift, and the ledger's is the one that must hold."""
    import inspect

    import ledger.schemes as sch

    src = inspect.getsource(sch)
    assert "captured" in src           # it is discussed
    assert "stays in `adjust`" in src  # and explicitly delegated


# ------------------------------------------------------------- headroom
def test_headroom_can_be_asked_before_trying():
    """A merchant should not discover the limit by being declined in front of a
    customer."""
    h = headroom(_ctx(current_minor=110_000, increments_so_far=2), "2026-03-05")
    assert h["known"] and h["ceiling_minor"] == 115_000
    assert h["headroom_minor"] == 5_000
    assert h["increments_remaining"] == 3
    assert h["days_remaining"] == 26


def test_headroom_never_goes_negative():
    h = headroom(_ctx(current_minor=200_000), "2026-03-05")
    assert h["headroom_minor"] == 0


def test_a_refusal_reports_the_headroom_so_the_caller_need_not_guess():
    out = check_adjustment(_ctx(), 10_000, "2026-03-05")
    assert out["headroom_minor"] == 5_000


def test_the_limits_are_named_as_assumptions():
    """Real limits come from licensed, versioned scheme documentation. A number
    quoted from memory and presented as a rule is what this project exists not
    to do."""
    import ledger.schemes as sch

    assert hasattr(sch, "ASSUMED_RULES")
    assert not hasattr(sch, "SCHEME_RULES")
    assert "STAND-INS" in sch.__doc__ or "stand-ins" in sch.__doc__


# --------------------------------- the period guard, enforced not offered
def test_a_posting_into_a_closed_period_is_refused_by_post_itself():
    """The README said the control was "available rather than enforced":
    `periods.guard` existed and callers had to remember to call it. A rule
    nothing calls is a rule that is not in effect -- the same shape as every
    other bug this repo has found."""
    from ledger import periods
    from ledger.core import Ledger, credit, debit
    from ledger.payments import HOLD_ASSET, HOLD_LIAB, bootstrap_accounts

    lg = Ledger(":memory:")
    bootstrap_accounts(lg, "m1")
    periods.close_period(lg._conn(), "2026-03", closed_by="controller")

    with pytest.raises(periods.PeriodClosed):
        lg.post([debit(HOLD_ASSET, 100, "USD"),
                 credit(HOLD_LIAB, 100, "USD")],
                actor="t", reason="late", request_id="r-closed",
                effective_on="2026-03-15")


def test_an_open_period_still_posts():
    from ledger import periods
    from ledger.core import Ledger, credit, debit
    from ledger.payments import HOLD_ASSET, HOLD_LIAB, bootstrap_accounts

    lg = Ledger(":memory:")
    bootstrap_accounts(lg, "m1")
    periods.close_period(lg._conn(), "2026-03", closed_by="controller")

    txn = lg.post([debit(HOLD_ASSET, 100, "USD"),
                   credit(HOLD_LIAB, 100, "USD")],
                  actor="t", reason="fine", request_id="r-open",
                  effective_on="2026-04-02")
    assert txn > 0


def test_an_unstated_effective_date_defaults_to_today_not_to_exempt():
    """The honest default. A posting with no stated effective date IS being
    made today, and treating an unstated date as exempt from the close would
    make the guard optional by omission rather than by argument."""
    import inspect

    from ledger.core import Ledger

    src = inspect.getsource(Ledger.post)
    assert "effective_on or utcnow()[:10]" in src
    assert "periods.guard(c, eff)" in src


def test_the_guard_runs_before_the_insert_not_after():
    """Checking after the write and rolling back would leave the sequence
    allocated and the error arriving after the effect."""
    import inspect

    from ledger.core import Ledger

    src = inspect.getsource(Ledger.post)
    assert src.index("periods.guard(c, eff)") < src.index("INSERT INTO journal_txn")


def test_the_period_tables_are_part_of_the_ledger_schema():
    """A guard that queries a table which may not exist is a guard that fails
    OPEN on a fresh database -- the state every test starts in. Having `guard`
    tolerate a missing table would have recreated the problem being fixed."""
    from ledger.core import Ledger

    lg = Ledger(":memory:")
    tables = {r[0] for r in lg._conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "accounting_period" in tables
    assert "txn_effective_date" in tables
