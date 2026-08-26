"""Was it a missing index, or missing statistics?

    python3 pg_snapshot_stats.py

The snapshotted balance read was correct and saved nothing. The delta query
`account_id = ? AND id > watermark` read every entry for the account and
discarded all of them:

    Bitmap Heap Scan on d_journal_entry e  (actual rows=0)
      Filter: (id > s.watermark_id)
      Rows Removed by Filter: 200000
      Buffers: shared read=2029

The same buffer count the full scan touches. **I read that as a missing index**
-- the derived schema indexes `(account_id)` alone, so nothing lets the delta
start at the watermark -- and added `(account_id, id)`.

This file is the experiment that should have come first. It builds the same
200,000-entry account with the replay depth at ZERO, so a working plan should
touch almost nothing, and separates the two candidate causes:

    A. no composite index, never ANALYZEd
    B. no composite index, after ANALYZE
    C. composite index, after ANALYZE

If the index is the cause, A and B look alike and C is the fast one. If
statistics are the cause, A stands alone.

It is run from a fresh table rather than by clearing `pg_statistic`, which needs
superuser -- the ledger role is deliberately not one.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ledger.pg_nohotrow import DERIVED_SCHEMA
from ledger.pg_snapshot import SNAPSHOT_SCHEMA
from ledger.pg_store import DSN, PgLedger

CASH = "snap:cash"

SNAP_BALANCE_SQL = """
SELECT s.balance_minor
     + COALESCE((SELECT SUM(CASE WHEN e.direction = 'D' THEN e.amount_minor
                                 ELSE -e.amount_minor END)
                   FROM d_journal_entry e
                  WHERE e.account_id = s.account_id AND e.id > s.watermark_id), 0)
  FROM d_balance_snapshot s
 WHERE s.account_id = %s
 ORDER BY s.watermark_id DESC
 LIMIT 1
"""


def measure(conn, label: str) -> dict:
    raw = conn.execute(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + SNAP_BALANCE_SQL,
        (CASH,)).fetchone()[0]
    text = raw if isinstance(raw, str) else json.dumps(raw)
    bufs = sum(int(x) for x in re.findall(r'"Shared Hit Blocks": (\d+)', text))
    bufs += sum(int(x) for x in re.findall(r'"Shared Read Blocks": (\d+)', text))
    disc = sum(int(x) for x in re.findall(r'"Rows Removed by Filter": (\d+)',
                                          text))
    idx = sorted({i for i in re.findall(r'"Index Name": "([^"]+)"', text)
                  if i.startswith(("ix_d_entry", "d_journal_entry"))})
    out = {"label": label, "buffers": bufs, "discarded": disc,
           "index": ", ".join(idx) or "none"}
    print("  {:<36} {:>7,} buf | {:>8,} discarded | {}".format(
        label, bufs, disc, out["index"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entries", type=int, default=200000)
    ap.add_argument("--dsn", default=DSN)
    args = ap.parse_args()

    base = PgLedger(dsn=args.dsn)
    try:
        conn = base._psycopg.connect(args.dsn, autocommit=True,
                                     connect_timeout=8)
    except Exception as e:                                   # noqa: BLE001
        print("no Postgres at {}: {}".format(args.dsn, e))
        return 2

    server = conn.execute("SELECT version()").fetchone()[0]
    print(server.split(",")[0])

    # A genuinely fresh table: DERIVED_SCHEMA drops and recreates it, so it has
    # never been analyzed. That is the state a bulk load leaves behind.
    conn.execute(DERIVED_SCHEMA)
    conn.execute("DROP TABLE IF EXISTS d_balance_snapshot")
    conn.execute(SNAPSHOT_SCHEMA)
    conn.execute("DROP INDEX IF EXISTS ix_d_entry_account_id")
    conn.execute("INSERT INTO d_account VALUES (%s,'asset','USD',0,false)",
                 (CASH,))
    txn = conn.execute(
        "INSERT INTO d_journal_txn (actor, reason, request_id, sealed)"
        " VALUES ('stats','stats','stats',TRUE) RETURNING id").fetchone()[0]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO d_journal_entry (txn_id, account_id, direction,"
            " amount_minor, currency) VALUES (%s,%s,'D',%s,'USD')",
            [(txn, CASH, 100) for _ in range(args.entries)])

    wm = conn.execute("SELECT MAX(id) FROM d_journal_entry").fetchone()[0]
    bal = conn.execute(
        "SELECT COALESCE(SUM(amount_minor), 0) FROM d_journal_entry"
        " WHERE id <= %s", (wm,)).fetchone()[0]
    conn.execute(
        "INSERT INTO d_balance_snapshot (account_id, watermark_id,"
        " balance_minor) VALUES (%s,%s,%s)", (CASH, wm, bal))
    print("{:,} entries, snapshot at watermark {} -> replay depth 0\n".format(
        args.entries, wm))

    a = measure(conn, "A. no composite index, NO ANALYZE")
    conn.execute("ANALYZE d_journal_entry")
    b = measure(conn, "B. no composite index, ANALYZEd")
    conn.execute("CREATE INDEX ix_d_entry_account_id"
                 " ON d_journal_entry(account_id, id)")
    conn.execute("ANALYZE d_journal_entry")
    c = measure(conn, "C. composite index, ANALYZEd")

    # Leave the schema as the module ships it.
    conn.execute("DROP INDEX IF EXISTS ix_d_entry_account_id")
    conn.close()

    write_report(server, a, b, c, args)
    print("\nwrote docs/PG_SNAPSHOT_STATS.md")
    return 0


def write_report(server, a, b, c, args) -> None:
    L = []
    add = L.append
    add("# SE-1 — a missing index, or missing statistics?")
    add("")
    add("Generated by `pg_snapshot_stats.py` against `{}`.".format(
        server.split(",")[0]))
    add("")
    add("## The symptom")
    add("")
    add("The snapshotted balance read was **correct and saved nothing**. With")
    add("the replay depth at zero it still read every entry for the account and")
    add("discarded all of them:")
    add("")
    add("```")
    add("Bitmap Heap Scan on d_journal_entry e  (actual rows=0)")
    add("  Filter: (id > s.watermark_id)")
    add("  Rows Removed by Filter: 200000")
    add("  Buffers: shared read=2029")
    add("```")
    add("")
    add("That is the same buffer count the full scan touches — the failure mode")
    add("where a cache looks fine **because the answer is right**, so no")
    add("correctness test can see it.")
    add("")
    add("## The diagnosis I made, and the one the experiment supports")
    add("")
    add("I read it as a missing index — the derived schema indexes")
    add("`(account_id)` alone, so nothing lets the delta start at the watermark")
    add("— and added `(account_id, id)`. This is the experiment that should have")
    add("come first: {:,} entries, replay depth zero, two candidate causes".format(
        args.entries))
    add("separated.")
    add("")
    add("| | buffers | rows discarded | index chosen |")
    add("|---|---|---|---|")
    for r in (a, b, c):
        add("| {} | {:,} | {:,} | `{}` |".format(
            r["label"][3:], r["buffers"], r["discarded"], r["index"]))
    add("")

    stats_cause = a["buffers"] > b["buffers"] * 5
    index_helps = b["buffers"] > c["buffers"] * 5
    if stats_cause and not index_helps:
        add("**Statistics, not indexes.** A freshly bulk-loaded table has none,")
        add("so the planner mis-estimated the account's row count, chose a")
        add("bitmap scan over `(account_id)`, and discarded every row. After")
        add("ANALYZE it used the **primary key on `id`** — which had been there")
        add("the whole time — and the delta became a range scan.")
        add("")
        add("Row C settles it: with the composite index present the planner")
        add("**still** picks `{}`. It never uses the index I added.".format(
            c["index"]))
        add("")
        add("So the index was removed again. An unused index is not neutral —")
        add("it is maintained on every insert, a write cost paid for a read")
        add("benefit that does not exist.")
    elif index_helps:
        add("**The index does carry the difference** — B is materially worse")
        add("than C — so the original diagnosis stands and the index belongs in")
        add("the schema after all. Recorded as measured.")
    else:
        add("**Neither cause separates cleanly in this run.** Reported as")
        add("measured rather than re-run until it agreed.")
    add("")
    add("## What it means operationally")
    add("")
    add("A checkpoint taken right after a bulk load can be **correct and")
    add("useless at the same time**, and the fix is ANALYZE, not schema. Worth")
    add("stating plainly because *it must need an index* is the reflex, and it")
    add("was mine.")
    add("")
    add("## What this does not claim")
    add("")
    add("- **One table, one account, one shape of query.** This says nothing")
    add("  about whether other queries in this repo want other indexes.")
    add("- **Autovacuum would eventually have analyzed the table.** The window")
    add("  measured here is the one between a bulk load and that happening,")
    add("  which is exactly when a first checkpoint gets taken.")
    add("- **Buffer counts, not timings.** Deliberately: this machine is busy")
    add("  enough that the clock would be the weaker evidence.")

    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "PG_SNAPSHOT_STATS.md").write_text(
        "\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
