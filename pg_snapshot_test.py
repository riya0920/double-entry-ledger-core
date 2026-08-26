"""Do Postgres snapshots hold the read cost down, and are they correct?

    python3 pg_snapshot_test.py --entries 20000

Two questions, and the second one matters more.

This file is CORRECTNESS ONLY. Read cost lives in `pg_snapshot_read.py`,
because the obvious way to time it -- calling `balance()` in a loop -- opens a
fresh connection per read and measures TCP and SCRAM instead. An earlier version
of this file did exactly that and reported the full scan getting FASTER as
history grew; that section is gone rather than left in with a caveat.

THE QUESTION HERE is whether the obvious implementation is even right, and it
is not. `d_journal_entry.id` comes from an IDENTITY sequence, and a
sequence value is allocated BEFORE commit -- so a transaction holding a LOW id
can still be in flight while one holding a HIGHER id has committed. A snapshot
that trusts MAX(id) stores a watermark above an entry it could not see, and the
delta query then skips that entry for being below the watermark. It lands in
neither half.

This forces exactly that interleaving with two connections, so the result
is a demonstration rather than a race that might not fire, then runs the same
sequence with the drain enabled.

Requires a reachable Postgres. Nothing here is simulated.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ledger.pg_nohotrow import DERIVED_SCHEMA, DerivedLedger
from ledger.pg_snapshot import SnapshottedLedger
from ledger.pg_store import DSN, PgLedger

CASH = "snap:cash"
REV = "snap:revenue"


def _entries(amount: int) -> list[dict]:
    return [{"account_id": CASH, "direction": "D",
             "amount_minor": amount, "currency": "USD"},
            {"account_id": REV, "direction": "C",
             "amount_minor": amount, "currency": "USD"}]


# --------------------------------------------------------------- section 2
def sequence_gap(base: PgLedger, derived: DerivedLedger,
                 snap: SnapshottedLedger, drain: bool) -> dict:
    """Force the interleaving that breaks an undrained snapshot.

        A: BEGIN, insert entry            -- allocates a LOW id, stays open
        B: insert entry, COMMIT           -- allocates a HIGHER id, visible
        snapshot                          -- sees B's id as MAX, cannot see A
        A: COMMIT                         -- A's entry is now below the watermark

    With drain=False the snapshot is taken between B's commit and A's, so A's
    entry is in neither the checkpoint nor the delta. With drain=True the
    snapshot blocks until A commits, which is the entire fix.
    """
    psycopg = base._psycopg
    with psycopg.connect(base.dsn, autocommit=True) as c:
        c.execute("DELETE FROM d_balance_snapshot")

    before_scan = derived.balance(CASH)

    conn_a = psycopg.connect(base.dsn, autocommit=False)
    txn_a = conn_a.execute(
        "INSERT INTO d_journal_txn (actor, reason, request_id)"
        " VALUES ('gap','gap','gap-a') RETURNING id").fetchone()[0]
    id_a = conn_a.execute(
        "INSERT INTO d_journal_entry (txn_id, account_id, direction,"
        " amount_minor, currency) VALUES (%s,%s,'D',%s,'USD') RETURNING id",
        (txn_a, CASH, 7777)).fetchone()[0]
    conn_a.execute("UPDATE d_journal_txn SET sealed = TRUE WHERE id = %s",
                   (txn_a,))
    # A is now holding id_a, uncommitted.

    derived.post(_entries(11), "gap", "gap-b", "gap-b-1")   # commits, higher id

    # With drain=True the snapshot waits for A, so A must be committed by
    # someone else or the call would block until the timeout.
    def commit_a_later():
        time.sleep(0.6)
        conn_a.commit()
        conn_a.close()

    committer = None
    if drain:
        committer = threading.Thread(target=commit_a_later)
        committer.start()

    rep = snap.take_snapshot(drain=drain)

    if not drain:
        conn_a.commit()
        conn_a.close()
    else:
        committer.join()

    cached = snap.balance(CASH)
    truth = derived.balance(CASH)
    return {"drain": drain, "id_a": id_a, "watermark": rep["watermark"],
            "gap": id_a <= rep["watermark"], "cached": cached, "truth": truth,
            "delta": cached - truth, "before": before_scan,
            "drain_wait_s": rep["drain_wait_s"]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entries", type=int, default=200)
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
    # Mandatory after install_schema() rebuilds the journal -- see StaleSnapshot.
    snap.clear()
    derived.open_account(CASH, "asset", "USD")
    derived.open_account(REV, "revenue", "USD", overdraft_allowed=True)

    print(server.split(",")[0])

    # A little history, so the hazard is demonstrated against a populated
    # journal rather than an empty one.
    for i in range(args.entries):
        derived.post(_entries(100 + (i % 7)), "snap", "seed", "sd-{}".format(i))
    print("seeded {:,} postings".format(args.entries))
    print("the sequence-gap hazard")

    broken = sequence_gap(base, derived, snap, drain=False)
    print("   drain=False -> cached {} vs truth {} (delta {})".format(
        broken["cached"], broken["truth"], broken["delta"]))
    fixed = sequence_gap(base, derived, snap, drain=True)
    print("   drain=True  -> cached {} vs truth {} (delta {})".format(
        fixed["cached"], fixed["truth"], fixed["delta"]))

    write_report(server, broken, fixed, args)
    print("\nwrote docs/PG_SNAPSHOTS.md")
    return 0


def write_report(server, broken, fixed, args) -> None:
    L = []
    add = L.append
    add("# SE-1 — snapshot-backed balances on Postgres")
    add("")
    add("Generated by `pg_snapshot_test.py` against `{}`.".format(
        server.split(",")[0]))
    add("")
    add("`README.md` listed this as the remaining Postgres work: *the derived")
    add("design fixes most of the contention and moves the cost to the read")
    add("path, where it grows with history ... periodic snapshots are [the")
    add("answer], and the SQLite side already has that machinery. Porting it is")
    add("the remaining work.*")
    add("")

    add("## The finding: it is not a port")
    add("")
    add("The SQLite version checkpoints *everything up to here*, which is a")
    add("moment that exists because SQLite serializes writers with a")
    add("database-level lock. Postgres runs writers concurrently, so it is not,")
    add("and the obvious translation is **silently and permanently wrong**.")
    add("")
    add("`d_journal_entry.id` comes from an IDENTITY sequence, and **a sequence")
    add("value is allocated before the transaction commits.** So a transaction")
    add("holding a low id can still be in flight while one holding a higher id")
    add("has already committed. A snapshot that trusts `MAX(id)`:")
    add("")
    add("- stores a watermark **above** an entry it could not see, so the")
    add("  checkpoint sum excludes it, and")
    add("- the delta query (`id > watermark`) then skips that same entry for")
    add("  being **below** the watermark.")
    add("")
    add("The entry lands in neither half. Measured, with the interleaving")
    add("forced by two connections so it is a demonstration rather than a race:")
    add("")
    add("| | entry id | watermark | id <= wm | snapshot says | full scan says | error |")
    add("|---|---|---|---|---|---|---|")
    for r, name in ((broken, "`drain=False`"), (fixed, "`drain=True`")):
        add("| {} | {} | {} | {} | {:,} | {:,} | **{:+,}** |".format(
            name, r["id_a"], r["watermark"], "yes" if r["gap"] else "no",
            r["cached"], r["truth"], r["delta"]))
    add("")
    if broken["delta"]:
        add("**Nothing errors.** The arithmetic is self-consistent, the query")
        add("plan is fine, and the only way to see the {:+,} is to compare".format(
            broken["delta"]))
        add("against a full scan — which is the one thing a cache exists to")
        add("avoid doing. Every later snapshot inherits it, because each one is")
        add("built on the journal the previous one already mis-summed.")
    else:
        add("**The forced interleaving did not reproduce the error here**, so")
        add("this run does not establish the hazard. That is reported rather")
        add("than quietly dropped: the mechanism is real, but a demonstration")
        add("that did not fire is not evidence.")
    add("")

    add("## The fix: drain, then sum")
    add("")
    add("```")
    add("1. wm    <- MAX(id) visible now. The sequence is monotonic, so any")
    add("            transaction starting after this reads a value ABOVE wm.")
    add("2. xmax0 <- pg_snapshot_xmax(pg_current_snapshot()), the first xid not")
    add("            yet assigned. Every transaction that could still write an")
    add("            id <= wm has an xid below this.")
    add("3. wait until pg_snapshot_xmin(pg_current_snapshot()) >= xmax0 --")
    add("            i.e. every transaction in flight at step 2 has ended.")
    add("4. NOW sum entries with id <= wm. Each has committed or aborted, so")
    add("            the sum can never change again.")
    add("```")
    add("")
    add("Step 3 is the entire difference. `take_snapshot(drain=False)` is kept")
    add("so the failure can be reproduced, and it is never the default; a drain")
    add("that times out **raises** rather than falling back to the undrained")
    add("watermark, because a snapshot that quietly degrades under load is worse")
    add("than no snapshot — load is exactly when it would be taken.")
    add("")
    add("Drain wait in the clean run: **{:.3f}s**.".format(
        fixed["drain_wait_s"]))
    add("")

    add("## Read cost is measured elsewhere")
    add("")
    add("`pg_snapshot_read.py` and `docs/PG_SNAPSHOT_READS.md` carry it, on one")
    add("long-lived connection. This file used to time `balance()` in a loop,")
    add("which opens a fresh connection per read and therefore measured a TCP")
    add("handshake plus SCRAM authentication -- reporting the full scan getting")
    add("*faster* as history grew. That section was removed rather than kept")
    add("with a caveat attached.")
    add("")
    add("## What this does not claim")
    add("")
    add("- **One forced interleaving.** It is a demonstration of a mechanism,")
    add("  not a measurement of how often it fires in production -- which")
    add("  depends on transaction duration and posting rate, and is not")
    add("  estimated here.")
    add("- **The drain has a cost that is not characterised.** It waits for")
    add("  every transaction in flight to end, so a long-running writer delays")
    add("  every checkpoint behind it. `DrainTimeout` bounds that wait and turns")
    add("  it into an error; what it does NOT do is say how often a busy system")
    add("  would hit the bound.")
    add("- **Still not the spec's 100K / 50-worker figure**, which remains")
    add("  unclaimed.")

    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "PG_SNAPSHOTS.md").write_text(
        chr(10).join(L), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
