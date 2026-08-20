"""Periodic ledger operations: authorization expiry, FX revaluation, snapshots.

Three jobs that a ledger needs and that nothing in the request path can do,
because they are driven by the passage of time rather than by a customer action.

AUTHORIZATION EXPIRY. An `authorize` with no capture holds funds forever, which
is wrong in both directions: the cardholder's available balance stays reduced,
and our books carry a claim that will never settle. Card networks expire holds
after 7-30 days depending on scheme and MCC. Expiry posts the same entries as a
void -- the hold is released, no money moved -- but it is recorded with a
different reason so the two are distinguishable in the journal. A void is a
decision; an expiry is a timeout, and a book full of expiries is an operational
signal that captures are not happening.

FX REVALUATION. Position accounts accumulate exposure in each currency. Between
the trade date and the reporting date the rate moves, so the reporting-currency
value of that position changes even though no transaction occurred. That change
is unrealised P&L and has to be recognised, or the balance sheet reports a
position at a rate that no longer exists. Revaluation is reversed at the start of
the next period and re-struck, so the same movement is never counted twice.

SNAPSHOTS. Purely a performance checkpoint. Deleting every snapshot changes no
answer, only the time taken to compute it -- which is exactly the property that
makes it safe. A cache that can change an answer is a second source of truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .core import Ledger, credit, debit, utcnow
from .fx import FX_GAIN_LOSS, FX_POSITION, convert

DEFAULT_AUTH_LIFETIME_DAYS = 7
FX_UNREALISED = "fx:unrealised_pnl"


# --------------------------------------------------------------------- expiry
@dataclass
class ExpiryResult:
    expired: list[str]
    released_minor: int
    skipped: list[str]


def expire_authorizations(ledger: Ledger, as_of: str | None = None,
                          lifetime_days: int = DEFAULT_AUTH_LIFETIME_DAYS,
                          request_id: str = "expiry-job") -> ExpiryResult:
    """Release holds older than `lifetime_days` that were never captured."""
    from .payments import HOLD_ASSET, HOLD_LIAB

    as_of = as_of or utcnow()
    cutoff = (datetime.fromisoformat(as_of) - timedelta(days=lifetime_days))
    expired, skipped = [], []
    released = 0

    rows = ledger._conn().execute(
        "SELECT * FROM payment WHERE state IN ('authorized','partially_captured')"
    ).fetchall()

    for row in rows:
        created = datetime.fromisoformat(row["created_at"]).replace(
            tzinfo=timezone.utc)
        if created > cutoff:
            continue
        remaining = row["authorized_minor"] - row["captured_minor"]
        if remaining <= 0:
            skipped.append(row["id"])
            continue

        with ledger.tx() as con:
            ledger.post(
                [debit(HOLD_LIAB, remaining, row["currency"]),
                 credit(HOLD_ASSET, remaining, row["currency"])],
                actor="expiry-job",
                # A distinct reason from 'void'. A void is a decision, an expiry
                # is a timeout, and a book full of expiries means captures are
                # not happening -- which is invisible if both say "void".
                reason="expire:" + row["id"],
                request_id="{}-{}".format(request_id, row["id"]), con=con)
            con.execute(
                "UPDATE payment SET state = 'expired' WHERE id = ?", (row["id"],))
        expired.append(row["id"])
        released += remaining

    return ExpiryResult(expired, released, skipped)


# ---------------------------------------------------------------- revaluation
@dataclass
class RevaluationResult:
    currency: str
    position_minor: int
    booked_rate: str
    current_rate: str
    booked_value_minor: int
    current_value_minor: int
    unrealised_minor: int
    txn_id: int | None


def revalue_fx_position(ledger: Ledger, currency: str, booked_rate: str,
                        current_rate: str, reporting_currency: str = "USD",
                        request_id: str = "fx-reval") -> RevaluationResult:
    """Recognise unrealised P&L on an FX position at the reporting rate.

    Both legs land in the REPORTING currency, so the entry balances there and the
    foreign-currency position itself is untouched -- revaluation changes what the
    position is worth, not how much of it there is. Adjusting the position leg
    would silently create or destroy foreign currency.
    """
    position = ledger.balance(FX_POSITION.format(currency))
    if position == 0:
        return RevaluationResult(currency, 0, booked_rate, current_rate,
                                 0, 0, 0, None)

    booked_value = convert(position, booked_rate)
    current_value = convert(position, current_rate)
    delta = current_value - booked_value
    if delta == 0:
        return RevaluationResult(currency, position, booked_rate, current_rate,
                                 booked_value, current_value, 0, None)

    if delta > 0:
        entries = [debit(FX_UNREALISED, delta, reporting_currency),
                   credit(FX_GAIN_LOSS, delta, reporting_currency)]
    else:
        entries = [debit(FX_GAIN_LOSS, -delta, reporting_currency),
                   credit(FX_UNREALISED, -delta, reporting_currency)]

    txn = ledger.post(entries, actor="fx-reval",
                      reason="revalue:{}:{}".format(currency, current_rate),
                      request_id="{}-{}".format(request_id, currency))
    return RevaluationResult(currency, position, booked_rate, current_rate,
                             booked_value, current_value, delta, txn)


def reverse_revaluation(ledger: Ledger, result: RevaluationResult,
                        reporting_currency: str = "USD",
                        request_id: str = "fx-reval-reverse") -> int | None:
    """Reverse a prior revaluation at the start of the next period.

    Without this, the next revaluation recognises the movement from the ORIGINAL
    booked rate again and the same P&L is counted twice. Reversing and re-striking
    is the standard treatment and it is cheaper to reason about than incremental
    deltas, because each period's entry is independent of the last.
    """
    if result.txn_id is None or result.unrealised_minor == 0:
        return None
    d = result.unrealised_minor
    if d > 0:
        entries = [credit(FX_UNREALISED, d, reporting_currency),
                   debit(FX_GAIN_LOSS, d, reporting_currency)]
    else:
        entries = [credit(FX_GAIN_LOSS, -d, reporting_currency),
                   debit(FX_UNREALISED, -d, reporting_currency)]
    return ledger.post(entries, actor="fx-reval",
                       reason="revalue-reverse:" + result.currency,
                       request_id="{}-{}".format(request_id, result.currency))


# ------------------------------------------------------------------ snapshots
def take_snapshot(ledger: Ledger, as_of: str | None = None) -> int:
    """Checkpoint every account's balance as of a timestamp."""
    as_of = as_of or utcnow()
    con = ledger._conn()
    rows = con.execute(
        "SELECT a.id,"
        "       COALESCE((SELECT SUM(CASE e.direction WHEN 'D' THEN e.amount_minor"
        "                            ELSE -e.amount_minor END)"
        "                   FROM journal_entry e JOIN journal_txn t ON t.id = e.txn_id"
        "                  WHERE e.account_id = a.id AND t.sealed = 1"
        "                    AND t.created_at <= ?), 0) AS bal,"
        "       COALESCE((SELECT MAX(t.id) FROM journal_txn t"
        "                  WHERE t.sealed = 1 AND t.created_at <= ?), 0) AS last_txn"
        "  FROM account a", (as_of, as_of)).fetchall()

    with ledger.tx() as c:
        for r in rows:
            c.execute(
                "INSERT OR REPLACE INTO balance_snapshot"
                " (account_id, as_of, balance_minor, last_txn_id, created_at)"
                " VALUES (?,?,?,?,?)",
                (r["id"], as_of, r["bal"], r["last_txn"], utcnow()))
    return len(rows)


def balance_as_of_snapshotted(ledger: Ledger, account_id: str, ts: str) -> int:
    """As-of balance using the newest snapshot at or before `ts`, plus a replay
    of only the entries after it.

    Falls back to a full scan when no snapshot applies. The fallback must produce
    the identical answer -- a snapshot that changes a result is not a cache, and
    there is a test asserting the two agree.
    """
    con = ledger._conn()
    snap = con.execute(
        "SELECT balance_minor, as_of FROM balance_snapshot"
        " WHERE account_id = ? AND as_of <= ?"
        " ORDER BY as_of DESC LIMIT 1", (account_id, ts)).fetchone()

    if snap is None:
        return ledger.balance_as_of(account_id, ts)

    delta = con.execute(
        "SELECT COALESCE(SUM(CASE e.direction WHEN 'D' THEN e.amount_minor"
        "                    ELSE -e.amount_minor END), 0) AS d"
        "  FROM journal_entry e JOIN journal_txn t ON t.id = e.txn_id"
        " WHERE e.account_id = ? AND t.sealed = 1"
        "   AND t.created_at > ? AND t.created_at <= ?",
        (account_id, snap["as_of"], ts)).fetchone()["d"]
    return snap["balance_minor"] + delta
