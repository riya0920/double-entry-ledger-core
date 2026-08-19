"""Multi-currency conversion with deterministic rounding.

Pennies are where ledgers go to die. Two rules make the difference:

1. **Round half to even** (banker's rounding). Round-half-up is biased upward:
   over millions of conversions the bias accumulates into a real, one-directional
   discrepancy that shows up as an unexplained P&L line. Half-even splits the
   ties and the bias cancels. Python's `decimal` does this natively with
   ROUND_HALF_EVEN, so the rounding mode is declared once and never re-derived.

2. **The remainder is assigned, not dropped.** Converting one amount is easy.
   Splitting a converted amount across N accounts is where books break: the
   parts, individually rounded, do not sum to the whole. The difference (always
   under N minor units) must land somewhere deterministic. Here it goes to the
   largest allocation, and the rule is stated rather than emergent -- what
   matters is that the same inputs always produce the same assignment, so a
   recomputation reconciles.

The FX gain/loss account exists because a conversion is not value-neutral in the
books: the debit leg is in one currency, the credit leg in another, and the
residual at the booking rate has to be recognised somewhere. Hiding it inside a
rounding difference is how an FX desk loses track of its own exposure.
"""
from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, localcontext

FX_GAIN_LOSS = "fx:gain_loss"


def convert(amount_minor: int, rate: str | Decimal, *, precision: int = 28) -> int:
    """Convert integer minor units at `rate`, rounding half to even.

    `rate` is a string or Decimal on purpose. Passing a float here would
    reintroduce binary floating point at the one point in the system where the
    result must be exactly reproducible, and 0.1 + 0.2 != 0.3 is not an
    acceptable property for a conversion rate.
    """
    if isinstance(rate, float):
        raise TypeError("pass the FX rate as str or Decimal, never float")
    with localcontext() as ctx:
        ctx.prec = precision
        converted = Decimal(amount_minor) * Decimal(rate)
        return int(converted.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def allocate(amount_minor: int, weights: list[int]) -> list[int]:
    """Split `amount_minor` across `weights` so the parts sum EXACTLY to the whole.

    Largest-remainder method: floor every share, then hand the leftover units to
    the entries with the largest fractional parts. Deterministic, order-stable,
    and it never invents or loses a unit -- `sum(allocate(x, w)) == x` for every
    input, which is the property the test suite checks with hypothesis.
    """
    if not weights or any(w < 0 for w in weights):
        raise ValueError("weights must be non-empty and non-negative")
    total_w = sum(weights)
    if total_w == 0:
        raise ValueError("weights must not sum to zero")

    sign = -1 if amount_minor < 0 else 1
    amount = abs(amount_minor)

    exact = [amount * w for w in weights]
    base = [e // total_w for e in exact]
    remainder = amount - sum(base)

    # Rank by fractional part, tie-broken by index so the result is stable.
    order = sorted(range(len(weights)),
                   key=lambda i: (-(exact[i] % total_w), i))
    for k in range(remainder):
        base[order[k % len(order)]] += 1
    return [sign * b for b in base]


FX_POSITION = "fx:position:{}"


def conversion_entries(from_account: str, to_account: str, amount_minor: int,
                       from_ccy: str, to_ccy: str, rate: str | Decimal,
                       booking_value_minor: int | None = None):
    """Journal legs for an FX conversion.

    The thing that makes this non-obvious: **a conversion cannot be two legs.**
    Debiting a EUR account and crediting a USD account leaves BOTH currencies
    unbalanced, and the ledger's per-currency seal check rejects it -- correctly.
    Money does not teleport between currencies; it is sold into one position and
    bought out of another.

    So each side balances within its own currency against an FX position account:

        C from_account          (from_ccy)   -- currency sold
        D fx:position:FROM_CCY  (from_ccy)   -- ... into the position
        D to_account            (to_ccy)     -- currency bought
        C fx:position:TO_CCY    (to_ccy)     -- ... out of the position

    The position accounts are where FX exposure lives, which is exactly where a
    treasury desk wants to see it rather than smeared across customer accounts.

    `booking_value_minor` is the converted amount the business expected (say, at
    yesterday's booking rate). Any difference against today's rate is recognised
    in FX gain/loss, offset against the position account so `to_ccy` still
    balances. Absorbing that difference into the customer's leg instead would
    quietly move a rate movement onto the customer.
    """
    from .core import credit, debit

    converted = convert(amount_minor, rate)
    pos_from = FX_POSITION.format(from_ccy)
    pos_to = FX_POSITION.format(to_ccy)

    entries = [
        credit(from_account, amount_minor, from_ccy),
        debit(pos_from, amount_minor, from_ccy),
        debit(to_account, converted, to_ccy),
        credit(pos_to, converted, to_ccy),
    ]

    if booking_value_minor is not None and booking_value_minor != converted:
        diff = converted - booking_value_minor
        if diff > 0:
            # Received more than booked: a gain. Credit revenue, debit position
            # so to_ccy still nets to zero.
            entries += [credit(FX_GAIN_LOSS, diff, to_ccy),
                        debit(pos_to, diff, to_ccy)]
        else:
            entries += [debit(FX_GAIN_LOSS, -diff, to_ccy),
                        credit(pos_to, -diff, to_ccy)]
    return entries, converted
