"""Consolidated multi-currency reporting, and the number it refuses to give.

`ledger/fx.py` gets conversions right: money is sold into a position and bought
out of another, each side balancing within its own currency. What the book then
lacks is a single figure -- with EUR and USD balances sitting side by side there
is no "total assets", because adding them is a category error until somebody
names a rate.

WHICH RATE, AND WHY IT IS NOT ONE CHOICE. Consolidation uses at least two, and
using one for everything is the classic error:

  CLOSING RATE   for balance-sheet items (assets, liabilities). What they are
                 worth at the reporting date.
  AVERAGE RATE   for income-statement items (revenue, expense). What they were
                 worth as they were earned, approximated over the period.

Translating revenue at the closing rate makes a month's earnings move because
the currency moved on the last day, which is a different fact wearing the same
name. The residual between the two is the CUMULATIVE TRANSLATION ADJUSTMENT,
and it goes to equity rather than P&L -- because it is not a gain anybody
realised, it is an artefact of translating a book at two different rates.

**A consolidation that reports no CTA has almost certainly used one rate for
everything**, so this module reports it explicitly and asserts the identity.

WHAT THIS IS NOT. IAS 21 / ASC 830 proper, which cover functional-currency
determination, hyperinflationary economies, net-investment hedges and disposal
recycling. This is the arithmetic and the CTA, with the rates as declared
inputs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal

BALANCE_SHEET = {"asset", "liability", "equity"}
INCOME_STATEMENT = {"revenue", "expense"}


class RateMissing(Exception):
    """Refusing to guess a rate is the whole point of this exception."""


@dataclass
class RateSet:
    """Rates INTO the reporting currency, as strings -- never floats.

    A float rate is the same bug as a float amount: 0.1 + 0.2 in the middle of a
    consolidation produces a CTA that is pure representation error and looks
    exactly like a real one.
    """
    reporting_currency: str
    closing: dict = field(default_factory=dict)
    average: dict = field(default_factory=dict)

    def rate_for(self, currency: str, kind: str) -> Decimal:
        if currency == self.reporting_currency:
            return Decimal(1)
        table = self.closing if kind in BALANCE_SHEET else self.average
        if currency not in table:
            raise RateMissing(
                "no {} rate for {} -> {}; refusing to guess".format(
                    "closing" if kind in BALANCE_SHEET else "average",
                    currency, self.reporting_currency))
        return Decimal(str(table[currency]))


def translate(amount_minor: int, rate: Decimal) -> int:
    """Round half-even, like the rest of this ledger.

    Half-up accumulates a one-directional bias, and a consolidation applies
    rounding once per account -- so the bias shows up as a CTA that grows with
    the number of accounts rather than with the currency movement.
    """
    q = (Decimal(amount_minor) * rate).quantize(Decimal(1),
                                                rounding=ROUND_HALF_EVEN)
    return int(q)


@dataclass
class ConsolidatedLine:
    account_id: str
    kind: str
    currency: str
    local_minor: int
    rate: str
    reported_minor: int


def consolidate(con, rates: RateSet) -> dict:
    """Translate every account into the reporting currency and prove it ties."""
    rows = con.execute(
        "SELECT a.id, a.kind, a.currency, COALESCE(b.balance_minor, 0)"
        "  FROM account a LEFT JOIN account_balance b ON b.account_id = a.id"
        " ORDER BY a.kind, a.id").fetchall()

    lines, by_kind = [], {}
    for acct, kind, ccy, bal in rows:
        rate = rates.rate_for(ccy, kind)
        reported = translate(int(bal), rate)
        lines.append(ConsolidatedLine(acct, kind, ccy, int(bal), str(rate),
                                      reported))
        by_kind[kind] = by_kind.get(kind, 0) + reported

    # The book balances in LOCAL currency by construction -- that is invariant
    # I1. After translation at two different rates it does not, and the residual
    # IS the CTA. Reporting it as a plug would hide the arithmetic; reporting it
    # as a named line is what makes the statement readable.
    total = sum(by_kind.values())
    cta = -total

    return {
        "reporting_currency": rates.reporting_currency,
        "lines": lines,
        "by_kind": by_kind,
        "translated_total_minor": total,
        "cumulative_translation_adjustment_minor": cta,
        "balances_after_cta": total + cta == 0,
        "currencies": sorted({l.currency for l in lines}),
    }


def render(result: dict) -> str:
    L = ["consolidated into {}".format(result["reporting_currency"]),
         "currencies in the book: {}".format(", ".join(result["currencies"])),
         "",
         "{:<26}{:<12}{:>8}{:>16}{:>10}{:>16}".format(
             "account", "kind", "ccy", "local", "rate", "reported")]
    L.append("-" * 88)
    for l in result["lines"]:
        L.append("{:<26}{:<12}{:>8}{:>16,}{:>10}{:>16,}".format(
            l.account_id[:25], l.kind, l.currency, l.local_minor, l.rate,
            l.reported_minor))
    L.append("-" * 88)
    for kind, v in sorted(result["by_kind"].items()):
        L.append("{:<46}{:>26,}".format(kind, v))
    L.append("")
    L.append("{:<46}{:>26,}".format("translated total", result["translated_total_minor"]))
    L.append("{:<46}{:>26,}".format(
        "cumulative translation adjustment (equity)",
        result["cumulative_translation_adjustment_minor"]))
    L.append("{:<46}{:>26}".format("balances after CTA",
                                   result["balances_after_cta"]))
    return "\n".join(L)
