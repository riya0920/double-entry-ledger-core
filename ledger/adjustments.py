"""Incremental and decremental authorization adjustments.

The lifecycle in `payments.py` goes authorize -> capture -> refund | void, and
real card authorizations do something it has no transition for: **they change
size while they are open.**

  A hotel authorizes an estimated stay, then increments it when the guest orders
  room service and extends a night. A fuel pump authorizes a nominal amount at
  the start and adjusts to the real one when the nozzle stops. A restaurant
  authorizes the bill and increments for the tip.

Without an adjustment transition the only ways to model that are both wrong:

  VOID AND RE-AUTHORIZE  loses the original authorization date, which is what
                         the card scheme's expiry clock runs from -- so a
                         re-auth silently restarts the clock and the merchant
                         thinks it has 30 days when it has 3.
  CAPTURE THE DIFFERENCE captures money before the stay ends, which is what the
                         hold exists to avoid.

WHAT AN ADJUSTMENT IS IN THE LEDGER. Exactly what an authorize is -- a movement
between the hold asset and the hold liability -- signed. An increment holds
more; a decrement releases some. Neither pays the merchant, because an
adjustment is still not a capture.

THE ONE RULE THAT MATTERS. A decrement can never take the authorization below
what has already been captured. Partial capture makes that reachable: capture
80, then decrement to 50, and the book now says you hold less than you have
already paid out. The guard is in `adjust`, and a test drives exactly that
sequence.
"""
from __future__ import annotations

from dataclasses import dataclass

from .core import Ledger, credit, debit
from .payments import HOLD_ASSET, HOLD_LIAB, PaymentError, _guard


@dataclass
class Adjustment:
    payment_id: str
    txn_id: int
    delta_minor: int
    authorized_before: int
    authorized_after: int
    kind: str                       # incremental | decremental


def adjust(ledger: Ledger, payment_id: str, delta_minor: int,
           request_id: str, idempotency_key: str | None = None) -> Adjustment:
    """Resize an open authorization. Positive increments, negative decrements."""
    if delta_minor == 0:
        raise PaymentError("an adjustment of zero is not an adjustment")

    def work(con):
        row = con.execute("SELECT * FROM payment WHERE id = ?",
                          (payment_id,)).fetchone()
        if row is None:
            raise PaymentError("unknown payment " + payment_id)

        # An adjustment is only legal while the authorization is open. Reusing
        # the capture guard rather than writing a second rule keeps "illegal
        # transitions are unreachable" true of this transition too.
        _guard(row["state"], "capture")

        before = int(row["authorized_minor"])
        captured = int(row["captured_minor"])
        after = before + delta_minor

        if after <= 0:
            raise PaymentError(
                "adjustment would take the authorization to {}; void it "
                "instead".format(after))
        if after < captured:
            raise PaymentError(
                "cannot decrement to {} below the {} already captured -- the "
                "money has left".format(after, captured))

        ccy = row["currency"]
        kind = "incremental" if delta_minor > 0 else "decremental"
        size = abs(delta_minor)

        # Increment: hold more (debit the hold asset, credit the liability).
        # Decrement: the mirror image. Same two accounts either way, because an
        # adjustment is an authorize of a different size and not a new kind of
        # event.
        if delta_minor > 0:
            entries = [debit(HOLD_ASSET, size, ccy), credit(HOLD_LIAB, size, ccy)]
        else:
            entries = [debit(HOLD_LIAB, size, ccy), credit(HOLD_ASSET, size, ccy)]

        txn = ledger.post(entries, actor="payments-api",
                          reason="{}:{}".format(kind, payment_id),
                          request_id=request_id, con=con)

        con.execute("UPDATE payment SET authorized_minor = ? WHERE id = ?",
                    (after, payment_id))
        return {"txn_id": txn, "payment_id": payment_id, "kind": kind,
                "authorized_after": after}

    if idempotency_key is not None:
        payload = {"op": "adjust", "payment_id": payment_id,
                   "delta_minor": delta_minor}
        body, _, _ = ledger.run_idempotent(idempotency_key, payload, work)
        txn, after = int(body["txn_id"]), int(body["authorized_after"])
        kind = body["kind"]
        return Adjustment(payment_id, txn, delta_minor, after - delta_minor,
                          after, kind)

    with ledger.tx() as con:
        body = work(con)
    after = int(body["authorized_after"])
    return Adjustment(payment_id, int(body["txn_id"]), delta_minor,
                      after - delta_minor, after, body["kind"])


def authorized_amount(ledger: Ledger, payment_id: str) -> int:
    row = ledger._conn().execute(
        "SELECT authorized_minor FROM payment WHERE id = ?",
        (payment_id,)).fetchone()
    if row is None:
        raise PaymentError("unknown payment " + payment_id)
    return int(row["authorized_minor"])
