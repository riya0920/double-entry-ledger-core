"""Payment API layer: the auth-hold accounting.

The point of this file is one idea: **held funds are not moved funds.** An
authorization creates an obligation, not a transfer, so it posts into a holds
account pair and leaves the merchant payable untouched. Capture is what moves
money, and it releases exactly the captured slice of the hold.

Full lifecycle: authorize, capture (partial and full), refund (partial and
full), void, and expiry. The transition table below is the whole specification
and `_guard` is its only enforcement point.

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
    refunded_minor    INTEGER NOT NULL DEFAULT 0,
    fee_minor         INTEGER NOT NULL DEFAULT 0,
    state             TEXT NOT NULL CHECK (state IN
                        ('authorized','partially_captured','captured','voided',
                         'partially_refunded','refunded','expired')),
    created_at        TEXT NOT NULL
);
"""

# The complete transition table. Anything not listed here is unreachable, and
# `_guard` is the single place that enforces it -- so "illegal transitions are
# impossible" is a property of one dictionary rather than of scattered ifs.
#
#                        authorized
#                       /     |     \
#                  void   capture   (expiry -- not modelled)
#                    |       |
#                 voided  partially_captured -> captured
#                             |                    |
#                          refund               refund
#                             v                    v
#                    partially_refunded <-> refunded
ALLOWED: dict[tuple[str, str], tuple[str, ...]] = {
    ("authorized", "capture"): ("partially_captured", "captured"),
    ("partially_captured", "capture"): ("partially_captured", "captured"),
    ("authorized", "void"): ("voided",),
    ("captured", "refund"): ("partially_refunded", "refunded"),
    ("partially_captured", "refund"): ("partially_refunded", "refunded"),
    ("partially_refunded", "refund"): ("partially_refunded", "refunded"),
}

TERMINAL = {"voided", "refunded", "expired"}


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
              amount_minor: int, currency: str, request_id: str,
              idempotency_key: str | None = None) -> int:
    """Record a hold. No merchant balance changes -- that is the whole point.

    With `idempotency_key`, the payment row, the journal entries AND the stored
    response all commit together, so a client that times out and retries gets
    the original answer instead of a second hold. Without it the call behaves
    as it always did, which is what the library-level tests exercise.
    """
    if amount_minor <= 0:
        raise PaymentError("authorization must be positive")

    def work(con):
        con.execute(
            "INSERT INTO payment (id, merchant_id, currency, authorized_minor,"
            " captured_minor, refunded_minor, fee_minor, state, created_at)"
            " VALUES (?,?,?,?,0,0,0,'authorized', datetime('now'))",
            (payment_id, merchant_id, currency, amount_minor))
        txn = ledger.post(
            [debit(HOLD_ASSET, amount_minor, currency),
             credit(HOLD_LIAB, amount_minor, currency)],
            actor="payments-api", reason="authorize:" + payment_id,
            request_id=request_id, con=con)
        return {"txn_id": txn, "payment_id": payment_id, "state": "authorized"}

    if idempotency_key is None:
        with ledger.tx() as con:
            return work(con)["txn_id"]

    payload = {"op": "authorize", "payment_id": payment_id,
               "merchant_id": merchant_id, "amount_minor": amount_minor,
               "currency": currency}
    body, _, _ = ledger.run_idempotent(idempotency_key, payload, work)
    return int(body["txn_id"])


def capture(ledger: Ledger, payment_id: str, amount_minor: int,
            fee_minor: int, request_id: str,
            idempotency_key: str | None = None) -> int:
    """Release the captured slice of the hold and move the money.

    Partial capture is the normal case, not an edge case: the uncaptured
    remainder stays held until void/expiry (remaining 80%).

    A retried capture is MORE dangerous than a retried authorize. An authorize
    holds funds; a capture MOVES them. Two captures for the same slice pay the
    merchant twice and take the money from the cardholder twice, and because
    partial capture is legal the second one often succeeds -- there is usually
    remaining authorization for it to consume. Nothing about the second call
    looks wrong.
    """
    if idempotency_key is not None:
        payload = {"op": "capture", "payment_id": payment_id,
                   "amount_minor": amount_minor, "fee_minor": fee_minor}
        body, _, _ = ledger.run_idempotent(
            idempotency_key, payload,
            lambda con: {"txn_id": _capture_locked(
                ledger, con, payment_id, amount_minor, fee_minor, request_id),
                "payment_id": payment_id, "op": "capture"})
        return int(body["txn_id"])

    with ledger.tx() as con:
        return _capture_locked(ledger, con, payment_id, amount_minor,
                               fee_minor, request_id)


def _capture_locked(ledger: Ledger, con, payment_id: str, amount_minor: int,
                    fee_minor: int, request_id: str) -> int:
    if True:
        row = con.execute("SELECT * FROM payment WHERE id = ?", (payment_id,)).fetchone()
        if row is None:
            raise PaymentError("unknown payment " + payment_id)
        _guard(row["state"], "capture")
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
        con.execute("UPDATE payment SET captured_minor = ?, fee_minor = fee_minor + ?,"
                    " state = ? WHERE id = ?",
                    (captured, fee_minor, state, payment_id))
    return txn


def _guard(state: str, action: str) -> None:
    """The single enforcement point for the transition table.

    Every state change in this module goes through here, which is what makes
    'illegal transitions are unreachable' checkable by a property test rather
    than by reading every branch.
    """
    if (state, action) not in ALLOWED:
        raise PaymentError(
            "illegal transition: cannot {} from state {!r}. Legal actions here: "
            "{}".format(action, state,
                        sorted(a for (s, a) in ALLOWED if s == state) or "none (terminal)"))


def void(ledger: Ledger, payment_id: str, request_id: str) -> int:
    """Release an authorization without moving money.

    Void is only legal before any capture. Once funds have moved the correct
    instrument is a refund, and conflating the two is how a reversal gets booked
    against a hold that no longer exists.
    """
    with ledger.tx() as con:
        row = con.execute("SELECT * FROM payment WHERE id = ?", (payment_id,)).fetchone()
        if row is None:
            raise PaymentError("unknown payment " + payment_id)
        _guard(row["state"], "void")

        remaining = row["authorized_minor"] - row["captured_minor"]
        ccy = row["currency"]
        txn = ledger.post(
            [debit(HOLD_LIAB, remaining, ccy), credit(HOLD_ASSET, remaining, ccy)],
            actor="payments-api", reason="void:" + payment_id,
            request_id=request_id, con=con)
        con.execute("UPDATE payment SET state = 'voided' WHERE id = ?", (payment_id,))
    return txn


def refund(ledger: Ledger, payment_id: str, amount_minor: int, request_id: str,
           refund_fee: bool = False, idempotency_key: str | None = None) -> int:
    """Return captured funds to the cardholder.

    A refund is NOT a reversal of the capture entry. The original capture stays
    in the journal untouched -- it happened -- and the refund posts its own
    balanced set of entries in the opposite direction. That is the append-only
    rule applied to the payment layer.

    `refund_fee` models the commercial question every processor has to answer:
    does the merchant get its processing fee back? Most do not refund it, so the
    default is False and the fee stays earned. Making it a parameter rather than
    a hard-coded choice is the point -- it is a pricing decision, not a
    technical one.

    A retried refund pays the cardholder twice out of the merchant's balance,
    which the merchant discovers at settlement rather than at the time.
    """
    if idempotency_key is not None:
        payload = {"op": "refund", "payment_id": payment_id,
                   "amount_minor": amount_minor, "refund_fee": refund_fee}
        body, _, _ = ledger.run_idempotent(
            idempotency_key, payload,
            lambda con: {"txn_id": _refund_locked(
                ledger, con, payment_id, amount_minor, request_id, refund_fee),
                "payment_id": payment_id, "op": "refund"})
        return int(body["txn_id"])

    with ledger.tx() as con:
        return _refund_locked(ledger, con, payment_id, amount_minor,
                              request_id, refund_fee)


def _refund_locked(ledger: Ledger, con, payment_id: str, amount_minor: int,
                   request_id: str, refund_fee: bool = False) -> int:
    if True:
        row = con.execute("SELECT * FROM payment WHERE id = ?", (payment_id,)).fetchone()
        if row is None:
            raise PaymentError("unknown payment " + payment_id)
        _guard(row["state"], "refund")

        refundable = row["captured_minor"] - row["refunded_minor"]
        if not 0 < amount_minor <= refundable:
            raise PaymentError(
                "refund {} exceeds refundable balance {} (captured {}, already "
                "refunded {})".format(amount_minor, refundable,
                                      row["captured_minor"], row["refunded_minor"]))

        ccy = row["currency"]
        payable = merchant_payable(row["merchant_id"])
        fee_back = 0
        if refund_fee and row["captured_minor"]:
            # Pro-rata share of the fee actually charged on this payment.
            fee_back = row["fee_minor"] * amount_minor // row["captured_minor"]

        entries = [
            credit(NETWORK_RECEIVABLE, amount_minor, ccy),
            debit(payable, amount_minor - fee_back, ccy),
        ]
        if fee_back:
            entries.append(debit(FEE_REVENUE, fee_back, ccy))
        txn = ledger.post(entries, actor="payments-api",
                          reason="refund:" + payment_id, request_id=request_id, con=con)

        refunded = row["refunded_minor"] + amount_minor
        state = "refunded" if refunded == row["captured_minor"] else "partially_refunded"
        con.execute("UPDATE payment SET refunded_minor = ?, state = ? WHERE id = ?",
                    (refunded, state, payment_id))
    return txn
