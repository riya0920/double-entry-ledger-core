"""The four invariants. Everything else in this repo exists to keep these true.

I1  global sum(debits) == sum(credits), per currency
I2  no account below its floor unless overdraft_allowed
I3  materialized account_balance == recomputation from the journal
I4  every idempotency key maps to exactly one transaction

check_all() returns a list of violations; empty list is the pass condition.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Violation:
    invariant: str
    detail: str


def i1_global_balance(con) -> list[Violation]:
    rows = con.execute(
        "SELECT e.currency AS ccy,"
        "       SUM(CASE e.direction WHEN 'D' THEN e.amount_minor ELSE 0 END) AS dr,"
        "       SUM(CASE e.direction WHEN 'C' THEN e.amount_minor ELSE 0 END) AS cr"
        "  FROM journal_entry e JOIN journal_txn t ON t.id = e.txn_id"
        " WHERE t.sealed = 1 GROUP BY e.currency").fetchall()
    out = []
    for r in rows:
        if r["dr"] != r["cr"]:
            out.append(Violation(
                "I1", "{}: debits {} != credits {} (delta {})".format(
                    r["ccy"], r["dr"], r["cr"], r["dr"] - r["cr"])))
    return out


def i2_floors(con) -> list[Violation]:
    rows = con.execute(
        "SELECT a.id, b.balance_minor AS bal, a.floor_minor AS flr"
        "  FROM account a JOIN account_balance b ON b.account_id = a.id"
        " WHERE a.overdraft_allowed = 0 AND b.balance_minor < a.floor_minor").fetchall()
    return [Violation("I2", "{} balance {} < floor {}".format(r["id"], r["bal"], r["flr"]))
            for r in rows]


def i3_derived_balances(con) -> list[Violation]:
    rows = con.execute(
        "SELECT a.id,"
        "       COALESCE(b.balance_minor, 0) AS cached,"
        "       COALESCE((SELECT SUM(CASE e.direction WHEN 'D' THEN e.amount_minor"
        "                            ELSE -e.amount_minor END)"
        "                   FROM journal_entry e JOIN journal_txn t ON t.id = e.txn_id"
        "                  WHERE e.account_id = a.id AND t.sealed = 1), 0) AS derived"
        "  FROM account a LEFT JOIN account_balance b ON b.account_id = a.id").fetchall()
    return [Violation("I3", "{}: cached {} != journal {}".format(
        r["id"], r["cached"], r["derived"]))
        for r in rows if r["cached"] != r["derived"]]


def i4_idempotency(con) -> list[Violation]:
    out = []
    dupes = con.execute(
        "SELECT txn_id, COUNT(*) AS n FROM idempotency_key"
        " WHERE txn_id IS NOT NULL GROUP BY txn_id HAVING n > 1").fetchall()
    for r in dupes:
        out.append(Violation("I4", "txn {} claimed by {} keys".format(r["txn_id"], r["n"])))
    orphan = con.execute(
        "SELECT k.key FROM idempotency_key k"
        "  LEFT JOIN journal_txn t ON t.id = k.txn_id"
        " WHERE k.txn_id IS NOT NULL AND (t.id IS NULL OR t.sealed = 0)").fetchall()
    for r in orphan:
        out.append(Violation("I4", "key {} points at a missing/unsealed txn".format(r["key"])))
    return out


def check_all(con) -> list[Violation]:
    return (i1_global_balance(con) + i2_floors(con)
            + i3_derived_balances(con) + i4_idempotency(con))


def report(con) -> str:
    v = check_all(con)
    if not v:
        return "ALL INVARIANTS HOLD (I1 balance, I2 floors, I3 derived, I4 idempotency)"
    lines = ["{} VIOLATION(S)".format(len(v))]
    lines += ["  [{}] {}".format(x.invariant, x.detail) for x in v]
    return "\n".join(lines)
