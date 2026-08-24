"""Accounting periods, backdating, and the close that makes a report final.

Every posting in this ledger is stamped `datetime('now')`. That is fine until
somebody asks the question a controller asks every month: **"is January
final?"** -- and the honest answer is no, because nothing prevents a posting
dated into January from landing tomorrow.

WHAT A CLOSE ACTUALLY IS. Not a flag on a report. It is a rule that changes what
the ledger will ACCEPT: after the close, a posting whose effective date falls in
that period is refused, and the correction has to go somewhere else. Without
that rule, "closed" is a note in a spreadsheet and every published figure is
provisional forever.

THE THREE THINGS A REAL CLOSE HAS TO DECIDE, and this module makes each one
explicit rather than implying it:

  WHAT IS THE EFFECTIVE DATE?   `journal_txn.created_at` is when we wrote the
                                row. A backdated correction is January money
                                recorded in February, so effective date has to
                                be a separate column that a caller can set.
                                Conflating them means you can never backdate,
                                which sounds safe and is why people post to the
                                wrong period instead.

  WHAT HAPPENS TO A LATE ITEM?  Two defensible answers and they are NOT
                                interchangeable. RESTATE reopens January and
                                changes a published number. ADJUST FORWARD
                                leaves January alone and books the correction
                                in February. Which one is right depends on
                                materiality and on who has already relied on the
                                figure -- it is a controllership decision, and
                                this module refuses to make it silently.

  WHO CAN REOPEN?               A close that anyone can undo is not a close. So
                                reopening takes a named approver and is recorded.

WHAT THIS IS NOT. There is no materiality threshold, no journal-approval
workflow, and no sub-ledger reconciliation. This is the mechanism a policy would
sit on top of, and DATA-2's README makes the same distinction about
restate-versus-adjust-forward for the same reason.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounting_period (
    period       TEXT PRIMARY KEY,          -- 'YYYY-MM'
    status       TEXT NOT NULL CHECK (status IN ('open','closed')),
    closed_at    TEXT,
    closed_by    TEXT,
    reopened_at  TEXT,
    reopened_by  TEXT,
    reopen_reason TEXT
);

-- Effective date is SEPARATE from created_at. created_at is when we wrote the
-- row; effective_on is when the money moved. A backdated correction has
-- created_at > effective_on, and that difference is the whole subject of this
-- module.
CREATE TABLE IF NOT EXISTS txn_effective_date (
    txn_id       INTEGER PRIMARY KEY REFERENCES journal_txn(id),
    effective_on TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_effective ON txn_effective_date(effective_on);
"""


class PeriodClosed(Exception):
    """Raised instead of silently posting into a closed month."""


@dataclass
class CloseResult:
    period: str
    status: str
    postings: int
    closed_by: str | None = None


def period_of(day: str | date) -> str:
    d = day if isinstance(day, str) else day.isoformat()
    return d[:7]


def install(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def status(con: sqlite3.Connection, period: str) -> str:
    row = con.execute("SELECT status FROM accounting_period WHERE period = ?",
                      (period,)).fetchone()
    # An unknown period is OPEN. The alternative -- default closed -- means a
    # brand new ledger refuses its own first posting.
    return row[0] if row else "open"


def is_closed(con: sqlite3.Connection, period: str) -> bool:
    return status(con, period) == "closed"


def guard(con: sqlite3.Connection, effective_on: str) -> None:
    """Refuse a posting into a closed period. Call before writing, not after."""
    p = period_of(effective_on)
    if is_closed(con, p):
        raise PeriodClosed(
            "period {} is closed; post the correction to an open period or "
            "reopen {} with a named approver".format(p, p))


def record_effective_date(con: sqlite3.Connection, txn_id: int,
                          effective_on: str) -> None:
    guard(con, effective_on)
    con.execute(
        "INSERT OR REPLACE INTO txn_effective_date (txn_id, effective_on)"
        " VALUES (?,?)", (txn_id, effective_on))


def close_period(con: sqlite3.Connection, period: str, closed_by: str,
                 now: str | None = None) -> CloseResult:
    """Close a month. Requires a name, because a close is an assertion."""
    if not closed_by or not closed_by.strip():
        raise ValueError("closing a period requires a named approver")
    n = con.execute(
        "SELECT COUNT(*) FROM txn_effective_date WHERE effective_on LIKE ?",
        (period + "%",)).fetchone()[0]
    con.execute(
        "INSERT INTO accounting_period (period, status, closed_at, closed_by)"
        " VALUES (?,'closed',COALESCE(?, datetime('now')),?)"
        " ON CONFLICT(period) DO UPDATE SET status='closed',"
        " closed_at=COALESCE(?, datetime('now')), closed_by=?",
        (period, now, closed_by, now, closed_by))
    return CloseResult(period, "closed", int(n), closed_by)


def reopen_period(con: sqlite3.Connection, period: str, approver: str,
                  reason: str, now: str | None = None) -> CloseResult:
    """Reopen a closed month. A close anyone can undo is not a close.

    Both the approver and the REASON are required. "Reopened" with no reason is
    the audit trail equivalent of no audit trail: it records that something
    happened and nothing about why.
    """
    if not approver or not approver.strip():
        raise ValueError("reopening a period requires a named approver")
    if not reason or not reason.strip():
        raise ValueError("reopening a period requires a recorded reason")
    if not is_closed(con, period):
        raise ValueError("period {} is not closed".format(period))
    con.execute(
        "UPDATE accounting_period SET status='open',"
        " reopened_at=COALESCE(?, datetime('now')), reopened_by=?,"
        " reopen_reason=? WHERE period=?",
        (now, approver, reason, period))
    return CloseResult(period, "open", 0, approver)


# ------------------------------------------------------- the two policies
def adjust_forward(effective_on: str, con: sqlite3.Connection,
                   today: str) -> str:
    """Move a late item into the earliest OPEN period on or after today.

    This is the conservative answer: January stays as published, and the
    correction is visible in February with its original effective date recorded
    alongside. What it costs is that February's figures now contain something
    that economically belongs to January -- which is why the original date is
    kept rather than overwritten.
    """
    target = period_of(today)
    if is_closed(con, target):
        raise PeriodClosed(
            "cannot adjust forward: {} is also closed".format(target))
    return today


def restate(effective_on: str, con: sqlite3.Connection, approver: str,
            reason: str) -> str:
    """Reopen the original period and post there.

    This is the answer that makes the published January number change. It is
    correct when the error is material enough that leaving it would mislead --
    and it requires the reopen approval, which is the point.
    """
    p = period_of(effective_on)
    if is_closed(con, p):
        reopen_period(con, p, approver, reason)
    return effective_on


def report(con: sqlite3.Connection) -> list[dict]:
    rows = con.execute(
        "SELECT period, status, closed_at, closed_by, reopened_by,"
        " reopen_reason FROM accounting_period ORDER BY period").fetchall()
    out = []
    for r in rows:
        n = con.execute(
            "SELECT COUNT(*) FROM txn_effective_date WHERE effective_on LIKE ?",
            (r[0] + "%",)).fetchone()[0]
        out.append({"period": r[0], "status": r[1], "postings": int(n),
                    "closed_by": r[3], "reopened_by": r[4],
                    "reopen_reason": r[5]})
    return out
