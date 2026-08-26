"""How the read cost actually behaves, measured on one connection.

Split out of `pg_snapshot_test.py` because the first version of that
measurement was invalid, and invalid in a way this repo had already documented.

WHAT WENT WRONG. `DerivedLedger.balance()` and `SnapshottedLedger.balance()`
each open a fresh connection, so timing them in a loop times a TCP handshake
plus SCRAM authentication once per read. The result was nonsense in a direction
that should have been impossible -- the "full scan" got FASTER as history grew,
87.98ms at 1,000 entries down to 29.19ms at 4,000 -- because the connection cost
dominated and the early samples were also paying cache warm-up.

`pg_store.py`'s own docstring says this, about the drill:

    A connection PER TRANSACTION is the shape this first had, and it made the
    drill 30x slower than the database: every posting paid a TCP handshake plus
    SCRAM authentication, so the benchmark was measuring connection setup and
    reporting it as ledger throughput.

I reproduced the documented mistake on the read path. So this file measures the
two QUERY SHAPES on a single long-lived connection.

AND IT DOES NOT TRUST THE CLOCK EITHER. This machine runs several other
projects' services, so wall-clock milliseconds carry a lot of noise that has
nothing to do with either query. `EXPLAIN (ANALYZE, BUFFERS)` reports **rows
actually read** and **buffers touched**, which are properties of the plan rather
than of how busy the host was. Those are the columns that carry the argument;
the timings are shown next to them and are the weaker evidence.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ledger.pg_nohotrow import DERIVED_BALANCE_SQL, DerivedLedger
from ledger.pg_snapshot import SnapshottedLedger
from ledger.pg_store import DSN, PgLedger

CASH = "snap:cash"
REV = "snap:revenue"

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


def _entries(amount: int) -> list[dict]:
    return [{"account_id": CASH, "direction": "D",
             "amount_minor": amount, "currency": "USD"},
            {"account_id": REV, "direction": "C",
             "amount_minor": amount, "currency": "USD"}]


def _time(conn, sql: str, n: int) -> dict:
    xs = []
    for _ in range(n):
        t0 = time.perf_counter()
        conn.execute(sql, (CASH,)).fetchone()
        xs.append((time.perf_counter() - t0) * 1000)
    xs.sort()
    return {"median": statistics.median(xs),
            "p95": xs[min(int(0.95 * (len(xs) - 1)), len(xs) - 1)]}


def _plan(conn, sql: str) -> dict:
    """Rows actually read and buffers touched -- the load-independent part."""
    raw = conn.execute(
        "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, (CASH,)).fetchone()[0]
    if isinstance(raw, str):
        raw = json.loads(raw)
    text = json.dumps(raw)
    # BUFFERS, not summed Actual Rows. The first version added Actual Rows over
    # every plan node, which double-counts: a scan feeding an aggregate reports
    # its rows once at the scan and again at the aggregate, so a 200,000-row
    # read printed 400,001. Buffers are the pages actually touched.
    bufs = sum(int(x) for x in re.findall(r'"Shared Hit Blocks": (\d+)', text))
    bufs += sum(int(x) for x in re.findall(r'"Shared Read Blocks": (\d+)', text))
    # The number that exposed the missing index: rows read and then thrown away.
    discarded = sum(int(x) for x in re.findall(r'"Rows Removed by Filter": (\d+)',
                                               text))
    top = re.findall(r'"Actual Rows": ([\d.]+)', text)
    return {"buffers": bufs, "discarded": discarded,
            "rows": int(float(top[0])) if top else 0}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entries", type=int, default=200000)
    ap.add_argument("--steps", type=int, default=5)
    ap.add_argument("--reads", type=int, default=60)
    ap.add_argument("--dsn", default=DSN)
    args = ap.parse_args()

    base = PgLedger(dsn=args.dsn)
    try:
        with base._psycopg.connect(args.dsn, autocommit=True,
                                   connect_timeout=8) as c:
            server = c.execute("SELECT version()").fetchone()[0]
    except Exception as e:                                   # noqa: BLE001
        print("no Postgres at {}: {}".format(args.dsn, e))
        return 2

    derived = DerivedLedger(base)
    derived.install_schema()
    snap = SnapshottedLedger(derived)
    snap.install_schema()
    # install_schema() recreated the journal, so any checkpoint from a previous
    # run now points past the end of it. Clearing is mandatory, not hygiene --
    # see StaleSnapshot.
    snap.clear()
    derived.open_account(CASH, "asset", "USD")
    derived.open_account(REV, "revenue", "USD", overdraft_allowed=True)

    print(server.split(",")[0])
    rows = []
    per = max(args.entries // args.steps, 1)
    written = 0

    # ONE connection for every timing, opened once. This is the whole fix.
    conn = base._psycopg.connect(args.dsn, autocommit=True)

    # Bulk-insert the history directly. The write path is measured by
    # pg_hotrow_test.py; paying its per-posting cost here would turn a
    # 200,000-entry read benchmark into an hour of writes.
    for step in range(args.steps):
        txn = conn.execute(
            "INSERT INTO d_journal_txn (actor, reason, request_id, sealed)"
            " VALUES ('read','load',%s,TRUE) RETURNING id",
            ("rd-{}".format(step),)).fetchone()[0]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO d_journal_entry (txn_id, account_id, direction,"
                " amount_minor, currency) VALUES (%s,%s,%s,%s,'USD')",
                [(txn, CASH, "D", 100 + (i % 7)) for i in range(per)])
        written += per

        # Warm both plans once so neither pays first-touch in the samples.
        conn.execute(DERIVED_BALANCE_SQL, (CASH,)).fetchone()
        scan_t = _time(conn, DERIVED_BALANCE_SQL, args.reads)
        scan_p = _plan(conn, DERIVED_BALANCE_SQL)

        snap.take_snapshot(drain=True)

        # ANALYZE after every bulk load. Not hygiene -- without it the planner
        # picks a plan that reads the whole account and discards it, and the
        # checkpoint saves nothing. `pg_snapshot_stats.py` isolates that; it
        # cannot be measured in this loop because clearing statistics needs
        # superuser and this ledger role is not one.
        conn.execute("ANALYZE d_journal_entry")
        conn.execute(SNAP_BALANCE_SQL, (CASH,)).fetchone()
        snap_t = _time(conn, SNAP_BALANCE_SQL, args.reads)
        snap_p = _plan(conn, SNAP_BALANCE_SQL)

        # A checkpoint is a cache: it may not change the answer.
        a = conn.execute(DERIVED_BALANCE_SQL, (CASH,)).fetchone()[0]
        b = conn.execute(SNAP_BALANCE_SQL, (CASH,)).fetchone()[0]
        assert int(a) == int(b), "snapshot changed the answer: {} vs {}".format(a, b)

        rows.append({"entries": written,
                     "scan_t": scan_t, "scan_p": scan_p,
                     "snap_t": snap_t, "snap_p": snap_p,
                     "replay": snap.replay_depth(CASH)})
        print("  {:>8,} entries | scan {:>7,} buf {:7.2f}ms | snapshot "
              "{:>5,} buf {:6.2f}ms".format(
                  written, scan_p["buffers"], scan_t["median"],
                  snap_p["buffers"], snap_t["median"]))

    conn.close()
    write_report(server, rows, args)
    print("\nwrote docs/PG_SNAPSHOT_READS.md")
    return 0


def write_report(server, rows, args) -> None:
    first, last = rows[0], rows[-1]
    L = []
    add = L.append
    add("# SE-1 — read cost with and without a snapshot")
    add("")
    add("Generated by `pg_snapshot_read.py` against `{}`.".format(
        server.split(",")[0]))
    add("")

    add("## The measurement mistake this file exists to correct")
    add("")
    add("The first version of this benchmark timed `DerivedLedger.balance()` in")
    add("a loop. That method opens a **fresh connection per call**, so the loop")
    add("timed a TCP handshake plus SCRAM authentication once per read, and the")
    add("result was nonsense in a direction that should have been impossible —")
    add("the full scan got *faster* as history grew, 87.98ms at 1,000 entries")
    add("down to 29.19ms at 4,000.")
    add("")
    add("`pg_store.py`'s own docstring already records this mistake on the write")
    add("path: *a connection per transaction ... made the drill 30x slower than")
    add("the database, so the benchmark was measuring connection setup and")
    add("reporting it as ledger throughput.* I reproduced it on the read path.")
    add("")
    add("Everything below runs the two query shapes on **one long-lived")
    add("connection**, and leads with `EXPLAIN (ANALYZE, BUFFERS)` rather than")
    add("the clock: rows read and buffers touched are properties of the plan,")
    add("not of how busy this machine was.")
    add("")

    add("## The second finding, and the diagnosis I got wrong")
    add("")
    add("Draining makes the snapshot **right**. It does not make it **fast**,")
    add("and as ported it was not — the delta query read every entry for the")
    add("account and threw them all away:")
    add("")
    add("```")
    add("Bitmap Heap Scan on d_journal_entry e  (actual rows=0)")
    add("  Filter: (id > s.watermark_id)")
    add("  Rows Removed by Filter: 200000")
    add("  Buffers: shared read=2029")
    add("```")
    add("")
    add("The same buffer count the full scan touches. The checkpoint was")
    add("arithmetically correct and did no less work — the failure mode where a")
    add("cache looks fine **because the answer is right**, so no correctness")
    add("test can see it.")
    add("")
    add("**I diagnosed it wrong.** It reads like a missing index, and I added")
    add("`(account_id, id)` on that basis. Isolating the two causes on 200,000")
    add("entries with replay depth zero says otherwise:")
    add("")
    add("| | buffers | rows discarded | index chosen |")
    add("|---|---|---|---|")
    add("| no composite index, **no ANALYZE** | 8,283 | 200,000 | `ix_d_entry_account` |")
    add("| no composite index, ANALYZEd | 46 | 0 | `d_journal_entry_pkey` |")
    add("| composite index, ANALYZEd | 31 | 0 | `d_journal_entry_pkey` |")
    add("")
    add("The index was never the problem. **A freshly bulk-loaded table has no")
    add("statistics**, so the planner estimated 514 rows for the account, chose")
    add("a bitmap scan over `(account_id)`, and discarded all 200,000. After")
    add("ANALYZE it used the PRIMARY KEY on `id` — which had been there the")
    add("whole time — and the delta became a range scan.")
    add("")
    add("The third row is the one that settles it: with the composite index")
    add("present the planner **still** picks `d_journal_entry_pkey`. It never")
    add("uses the index I added. So the index is gone again, and an unused index")
    add("is not neutral — it is maintained on every insert, a write cost paid")
    add("for a read benefit that does not exist.")
    add("")
    add("Operationally: a checkpoint taken right after a bulk load can be")
    add("correct and useless at once, and the fix is ANALYZE, not schema.")
    add("")

    add("## Result")
    add("")
    add("Every step ANALYZEs after its bulk load, for the reason above. The")
    add("isolation of that effect lives in `pg_snapshot_stats.py`, because")
    add("clearing statistics needs superuser and the ledger role is not one.")
    add("")
    add("| entries | replay depth | full scan | snapshotted |")
    add("|---|---|---|---|")
    for r in rows:
        add("| {:,} | {:,} | {:,} buf / {:.2f}ms | **{:,} buf** / {:.2f}ms |".format(
            r["entries"], r["replay"], r["scan_p"]["buffers"],
            r["scan_t"]["median"], r["snap_p"]["buffers"],
            r["snap_t"]["median"]))
    add("")

    sb_f, sb_l = first["scan_p"]["buffers"], last["scan_p"]["buffers"]
    kb_f, kb_l = first["snap_p"]["buffers"], last["snap_p"]["buffers"]
    add("| | at {:,} entries | at {:,} entries | growth |".format(
        first["entries"], last["entries"]))
    add("|---|---|---|---|")
    add("| full scan | {:,} buf | {:,} buf | {} |".format(
        sb_f, sb_l, "{:.1f}x".format(sb_l / sb_f) if sb_f else "-"))
    add("| snapshotted, ANALYZEd | {:,} buf | {:,} buf | {} |".format(
        kb_f, kb_l, "{:.1f}x".format(kb_l / kb_f) if kb_f else "-"))
    add("")

    if sb_f and sb_l > sb_f * 2 and kb_l < sb_l / 10:
        add("**The full scan's work grows with history; the snapshotted read's")
        add("does not** — {:,} buffers against {:,} at {:,} entries, a".format(
            kb_l, sb_l, last["entries"]))
        add("**{:.0f}x** reduction, in a number the host's load cannot move.".format(
            sb_l / kb_l if kb_l else float("nan")))
    else:
        add("**The expected separation did not appear cleanly in this run**, and")
        add("it is reported as measured rather than re-run until it agreed:")
        add("scan {:,} → {:,} buffers, snapshotted {:,} → {:,}.".format(
            sb_f, sb_l, kb_f, kb_l))
    add("")
    add("The mechanism is the `replay depth` column. A checkpoint holds the")
    add("number of entries a read must touch to roughly a constant regardless of")
    add("how long the account has existed — which is why neither extreme, cached")
    add("balance or pure derivation, was the answer.")
    add("")
    add("**The write path is untouched.** `post()` passes straight through to")
    add("`DerivedLedger`; a checkpoint that changed the write path would hand")
    add("back the contention the derived design was built to remove. And after")
    add("the index was removed there is no new write cost to account for.")
    add("")

    add("## What this does not claim")
    add("")
    add("- **The timings are the weak column.** This machine runs several other")
    add("  projects' services; the milliseconds are a floor with real noise in")
    add("  them. The rows-read and buffers columns are the evidence.")
    add("- **History is bulk-inserted**, not posted through the ledger. The")
    add("  write path is `pg_hotrow_test.py`'s subject; paying its per-posting")
    add("  cost would turn a {:,}-entry read benchmark into an hour of".format(
        args.entries))
    add("  writes. So this measures read shapes over a realistic *volume*, not")
    add("  a realistic write history.")
    add("- **One hot account**, and every snapshot is taken immediately before")
    add("  the read that benefits from it — the best case. A real cadence leaves")
    add("  a replay tail, and that tail is the number that would matter in")
    add("  production.")
    add("- **Snapshot-sweep cost is not measured.** `take_snapshot` sums every")
    add("  account at a watermark; on one account that is cheap and at scale it")
    add("  is a periodic full pass. The read side of the trade is measured here")
    add("  and the checkpoint side is not.")
    add("- **Still not the spec's 100K / 50-worker figure**, which remains")
    add("  unclaimed.")

    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "PG_SNAPSHOT_READS.md").write_text(
        "\n".join(L), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
