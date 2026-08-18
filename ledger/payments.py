"""Payment API layer: the auth-hold accounting.

The point of this file is one idea: **held funds are not moved funds.** An
authorization creates an obligation, not a transfer, so it posts into a holds
account pair and leaves the merchant payable untouched. Capture is what moves
money, and it releases exactly the captured slice of the hold.

Scope of this 20% slice: authorize + capture (full and partial).
refund / void / the full property-tested state machine are the remaining 80%.

Chart of accounts used here (acquirer's books):
  holds:auth_receivable     asset      claim on the issuer for authorized funds
  holds:pending_settlement  liability  the matching obligation while held
  network:receivable        asset      funds owed to us by the network post-capture
  merchant:<id>:payable     liability  what we owe the merchant
  revenue:fees              revenue    our processing fee
"""
from __future__ import annotations

from .core import Ledger, credit, debit

HOLD_ASSET = "holds:auth_receivable"
HOLD_LIAB = "holds:pending_settlement"
NETWORK_RECEIVABLE = "network:receivable"
FEE_REVENUE = "revenue:fees"

PAYMENT_DDL = """
CREATE TABLE IF NOT EXISTS payment (
    id                TEXT PRIMARY KEY,
    merchant_id       TEXT NOT NULL,
    currency          TEXT NOT NULL,
    authorized_minor  INTEGER NOT NULL,
    captured_minor    INTEGER NOT NULL DEFAULT 0,
    state             TEXT NOT NULL CHECK (state IN
                        ('authorized','partially_captured','captured','voided','refunded')),
    created_at        TEXT NOT NULL
);
"""

# Transitions this slice implements. Everything absent is unreachable by
# construction (the guard in _require_state), not by convention.
ALLOWED = {
    ("authorized", "capture"): ("partially_captured", "captured"),
    ("partially_captured", "capture"): ("partially_captured", "captured"),
}


class PaymentError(Exception):
    pass


def bootstrap_accounts(ledger: Ledger, merchant_id: str, currency: str = "USD") -> None:
    ledger._conn().executescript(PAYMENT_DDL)
    unlimited = -10**15
    for acct, kind in [(HOLD_ASSET, "asset"), (HOLD_LIAB, "liability"),
                       (NETWORK_RECEIVABLE, "asset"), (FEE_REVENUE, "revenue"),
                       ("merchant:{}:payable".format(merchant_id), "liability")]:
        try:
            ledger.open_account(acct, kind, currency,
                                floor_minor=unlimited, overdraft_allowed=True)
        except Exception:
            pass  # already open


def merchant_payable(merchant_id: str) -> str:
    return "merchant:{}:payable".format(merchant_id)


def authorize(ledger: Ledger, payment_id: str, merchant_id: str,
              amount_minor: int, currency: str, request_id: str) -> int:
    """Record a hold. No merchant balance changes -- that is the whole point."""
    if amount_minor <= 0:
        raise PaymentError("authorization must be positive")
    with ledger.tx() as con:
        con.execute(
            "INSERT INTO payment (id, merchant_id, currency, authorized_minor,"
            " captured_minor, state, created_at)"
            " VALUES (?,?,?,?,0,'authorized', datetime('now'))",
            (payment_id, merchant_id, currency, amount_minor))
        txn = ledger.post(
            [debit(HOLD_ASSET, amount_minor, currency),
             credit(HOLD_LIAB, amount_minor, currency)],
            actor="payments-api", reason="authorize:" + payment_id,
            request_id=request_id, con=con)
    return txn


def capture(ledger: Ledger, payment_id: str, amount_minor: int,
            fee_minor: int, request_id: str) -> int:
    """Release the captured slice of the hold and move the money.

    Partial capture is the normal case, not an edge case: the uncaptured
    remainder stays held until void/expiry (remaining 80%).
    """
    with ledger.tx() as con:
        row = con.execute("SELECT * FROM payment WHERE id = ?", (payment_id,)).fetchone()
        if row is None:
            raise PaymentError("unknown payment " + payment_id)
        if (row["state"], "capture") not in ALLOWED:
            raise PaymentError(
                "illegal transition: capture from state {!r}".format(row["state"]))
        remaining = row["authorized_minor"] - row["captured_minor"]
        if not 0 < amount_minor <= remaining:
            raise PaymentError(
                "capture {} exceeds remaining authorization {}".format(amount_minor, remaining))
        if not 0 <= fee_minor <= amount_minor:
            raise PaymentError("fee must be within [0, capture amount]")

        ccy = row["currency"]
        payable = merchant_payable(row["merchant_id"])
        entries = [
            # release the hold for the captured slice
            debit(HOLD_LIAB, amount_minor, ccy),
            credit(HOLD_ASSET, amount_minor, ccy),
            # and move the money for real
            debit(NETWORK_RECEIVABLE, amount_minor, ccy),
            credit(payable, amount_minor - fee_minor, ccy),
        ]
        if fee_minor:
            entries.append(credit(FEE_REVENUE, fee_minor, ccy))
        txn = ledger.post(entries, actor="payments-api",
                          reason="capture:" + payment_id, request_id=request_id, con=con)

        captured = row["captured_minor"] + amount_minor
        state = "captured" if captured == row["authorized_minor"] else "partially_captured"
        con.execute("UPDATE payment SET captured_minor = ?, state = ? WHERE id = ?",
                    (captured, state, payment_id))
    return txn
