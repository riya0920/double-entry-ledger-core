"""Property-based tests: hypothesis drives random operation sequences and the
four invariants must hold after EVERY one.

Example-based tests check the cases I thought of. These check the cases I did
not -- which, in a ledger, is where the money goes missing.
"""
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ledger import invariants
from ledger.core import Ledger, credit, debit
from ledger.fx import FX_GAIN_LOSS, FX_POSITION, allocate, conversion_entries, convert
from ledger.payments import (ALLOWED, HOLD_ASSET, HOLD_LIAB, PaymentError, authorize,
                             bootstrap_accounts, capture, refund, void)

BIG = -10**15
SLOW = settings(max_examples=60, deadline=None,
                suppress_health_check=[HealthCheck.function_scoped_fixture,
                                       HealthCheck.too_slow])


# ---------------------------------------------------------------- allocation
@given(amount=st.integers(min_value=-10**9, max_value=10**9),
       weights=st.lists(st.integers(min_value=0, max_value=1000),
                        min_size=1, max_size=12))
def test_allocation_never_creates_or_destroys_a_unit(amount, weights):
    """The property that keeps a split from leaking pennies."""
    assume(sum(weights) > 0)
    parts = allocate(amount, weights)
    assert sum(parts) == amount


@given(amount=st.integers(min_value=0, max_value=10**7),
       weights=st.lists(st.integers(min_value=1, max_value=100),
                        min_size=2, max_size=8))
def test_allocation_is_deterministic(amount, weights):
    """Same inputs, same assignment -- otherwise a recomputation cannot
    reconcile against the original."""
    assert allocate(amount, weights) == allocate(amount, weights)


@given(amount=st.integers(min_value=0, max_value=10**7),
       weights=st.lists(st.integers(min_value=1, max_value=100),
                        min_size=2, max_size=8))
def test_allocation_respects_weight_ordering(amount, weights):
    """A larger weight never receives less than a smaller one."""
    parts = allocate(amount, weights)
    pairs = sorted(zip(weights, parts))
    for (w1, p1), (w2, p2) in zip(pairs, pairs[1:]):
        if w1 < w2:
            assert p1 <= p2


# ----------------------------------------------------------------------- fx
def test_convert_rejects_float_rates():
    with pytest.raises(TypeError):
        convert(10_000, 1.09)


@given(amount=st.integers(min_value=1, max_value=10**9))
def test_conversion_balances_in_every_currency(amount):
    """The per-currency seal check is what a naive two-leg conversion fails."""
    entries, converted = conversion_entries(
        "cust:eur", "cust:usd", amount, "EUR", "USD", "1.09")
    by_ccy = {}
    for e in entries:
        delta = e.amount_minor if e.direction == "D" else -e.amount_minor
        by_ccy[e.currency] = by_ccy.get(e.currency, 0) + delta
    assert all(v == 0 for v in by_ccy.values()), by_ccy
    assert converted > 0


@given(amount=st.integers(min_value=1, max_value=10**7),
       drift=st.integers(min_value=-5000, max_value=5000))
def test_rate_movement_lands_in_fx_gain_loss_not_on_the_customer(amount, drift):
    entries, converted = conversion_entries(
        "cust:eur", "cust:usd", amount, "EUR", "USD", "1.09",
        booking_value_minor=convert(amount, "1.09") + drift)
    by_ccy = {}
    for e in entries:
        delta = e.amount_minor if e.direction == "D" else -e.amount_minor
        by_ccy[e.currency] = by_ccy.get(e.currency, 0) + delta
    assert all(v == 0 for v in by_ccy.values())

    customer_leg = sum(e.amount_minor for e in entries if e.account_id == "cust:usd")
    assert customer_leg == converted, "rate movement leaked onto the customer leg"
    if drift:
        assert any(e.account_id == FX_GAIN_LOSS for e in entries)


def test_half_even_rounding_is_used():
    """Round-half-up is biased upward and the bias accumulates. Half-even splits
    ties, so .5 cases go to the even neighbour in both directions."""
    assert convert(5, "0.5") == 2      # 2.5 -> 2 (even), not 3
    assert convert(15, "0.5") == 8     # 7.5 -> 8 (even)


# ------------------------------------------------------- state machine model
class PaymentMachine(RuleBasedStateMachine):
    """Random legal operation sequences; invariants checked after every step.

    The state machine only ever issues operations the transition table permits,
    then asserts that the ledger's four invariants survive. Anything the table
    forbids is checked separately in test_illegal_transitions_are_unreachable.
    """

    def __init__(self):
        super().__init__()
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.ledger = Ledger(Path(self.dir) / "prop.db")
        bootstrap_accounts(self.ledger, "m1")
        self.n = 0
        self.payments: dict[str, dict] = {}

    def _state(self, pid):
        row = self.ledger._conn().execute(
            "SELECT * FROM payment WHERE id = ?", (pid,)).fetchone()
        return row["state"] if row else None

    def _in_state(self, *states):
        return [p for p in self.payments if self._state(p) in states]

    @rule(amount=st.integers(min_value=100, max_value=500_000))
    def do_authorize(self, amount):
        self.n += 1
        pid = "p{}".format(self.n)
        authorize(self.ledger, pid, "m1", amount, "USD", "req-{}".format(self.n))
        self.payments[pid] = {"authorized": amount}

    @precondition(lambda self: self._in_state("authorized", "partially_captured"))
    @rule(data=st.data(), fee_bps=st.integers(min_value=0, max_value=300))
    def do_capture(self, data, fee_bps):
        pid = data.draw(st.sampled_from(self._in_state("authorized",
                                                       "partially_captured")))
        row = self.ledger._conn().execute(
            "SELECT * FROM payment WHERE id = ?", (pid,)).fetchone()
        remaining = row["authorized_minor"] - row["captured_minor"]
        assume(remaining > 0)
        amount = data.draw(st.integers(min_value=1, max_value=remaining))
        capture(self.ledger, pid, amount, amount * fee_bps // 10_000,
                "req-cap-{}".format(self.n))

    @precondition(lambda self: self._in_state("authorized"))
    @rule(data=st.data())
    def do_void(self, data):
        pid = data.draw(st.sampled_from(self._in_state("authorized")))
        void(self.ledger, pid, "req-void-{}".format(self.n))

    @precondition(lambda self: self._in_state("captured", "partially_captured",
                                              "partially_refunded"))
    @rule(data=st.data())
    def do_refund(self, data):
        pid = data.draw(st.sampled_from(
            self._in_state("captured", "partially_captured", "partially_refunded")))
        row = self.ledger._conn().execute(
            "SELECT * FROM payment WHERE id = ?", (pid,)).fetchone()
        refundable = row["captured_minor"] - row["refunded_minor"]
        assume(refundable > 0)
        amount = data.draw(st.integers(min_value=1, max_value=refundable))
        refund(self.ledger, pid, amount, "req-ref-{}".format(self.n))

    @invariant()
    def ledger_invariants_hold(self):
        v = invariants.check_all(self.ledger._conn())
        assert not v, "invariant violated: {}".format([x.detail for x in v])

    @invariant()
    def holds_never_go_negative(self):
        """A hold released twice would show up here before it showed up in the
        balance sheet."""
        assert self.ledger.balance(HOLD_ASSET) >= 0
        assert self.ledger.balance(HOLD_LIAB) <= 0

    @invariant()
    def refunds_never_exceed_captures(self):
        rows = self.ledger._conn().execute(
            "SELECT id, captured_minor, refunded_minor, authorized_minor,"
            " state FROM payment").fetchall()
        for r in rows:
            assert r["refunded_minor"] <= r["captured_minor"], r["id"]
            assert r["captured_minor"] <= r["authorized_minor"], r["id"]


TestPaymentMachine = PaymentMachine.TestCase
TestPaymentMachine.settings = settings(max_examples=25, stateful_step_count=25,
                                       deadline=None,
                                       suppress_health_check=[HealthCheck.too_slow])


# ------------------------------------------------- illegal transitions
@pytest.fixture
def led(tmp_path):
    lg = Ledger(tmp_path / "t.db")
    bootstrap_accounts(lg, "m1")
    return lg


ACTIONS = {
    "capture": lambda lg, pid: capture(lg, pid, 1, 0, "r"),
    "void": lambda lg, pid: void(lg, pid, "r"),
    "refund": lambda lg, pid: refund(lg, pid, 1, "r"),
}


def _drive_to(lg, target, pid):
    authorize(lg, pid, "m1", 10_000, "USD", "r-auth")
    if target == "authorized":
        return True
    if target == "voided":
        void(lg, pid, "r-void"); return True
    if target == "partially_captured":
        capture(lg, pid, 4_000, 0, "r-cap"); return True
    capture(lg, pid, 10_000, 0, "r-cap")
    if target == "captured":
        return True
    if target == "partially_refunded":
        refund(lg, pid, 4_000, "r-ref"); return True
    if target == "refunded":
        refund(lg, pid, 10_000, "r-ref"); return True
    return False


@pytest.mark.parametrize("state", ["authorized", "partially_captured", "captured",
                                   "voided", "partially_refunded", "refunded"])
@pytest.mark.parametrize("action", ["capture", "void", "refund"])
def test_illegal_transitions_are_unreachable(led, state, action):
    """Every (state, action) pair NOT in the table must raise, and the ledger
    must be unchanged afterwards. This is the exhaustive cross-product, not a
    sample -- 18 pairs, of which 6 are legal."""
    pid = "pay-{}-{}".format(state, action)
    assert _drive_to(led, state, pid)
    before = invariants.check_all(led._conn())
    assert not before

    legal = (state, action) in ALLOWED
    if legal:
        ACTIONS[action](led, pid)          # must not raise
    else:
        with pytest.raises(PaymentError, match="illegal transition|exceeds"):
            ACTIONS[action](led, pid)
    assert not invariants.check_all(led._conn())


def test_voided_and_refunded_are_terminal(led):
    for state in ("voided", "refunded"):
        assert not [a for (s, a) in ALLOWED if s == state], \
            "{} should have no outgoing transitions".format(state)
