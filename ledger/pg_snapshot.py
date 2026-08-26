"""Snapshot-backed balances for the DERIVED Postgres ledger.

`README.md` listed this as the remaining Postgres work, and as a specified job
rather than a hunch: *"the derived design fixes most of the contention and moves
the cost to the read path, where it grows with history. Neither extreme is the
answer; periodic snapshots are, and the SQLite side already has that machinery.
Porting it is the remaining work."*

This is the port. `maintenance.take_snapshot` does it for SQLite; the shape is
the same -- checkpoint the balance, then replay only what came after -- but the
correctness argument is NOT the same, and that difference is the point of this
module.

WHY IT IS NOT A TRANSLATION. SQLite writes are serialized by a database-level
lock, so "everything up to here" is a moment that exists. Postgres runs writers
concurrently, so it is not, and a snapshot taken the obvious way is silently and
permanently wrong.

    d_journal_entry.id comes from an IDENTITY sequence, and a sequence value is
    allocated BEFORE the transaction commits.

So a transaction holding id 100 can still be in flight while a transaction
holding id 105 has already committed. A snapshot that reads MAX(id) sees 105 --
it cannot see 100, which is uncommitted -- and stores watermark 105 with a sum
that excludes entry 100. When 100 finally commits it is below the watermark, so
the delta query (`id > 105`) skips it too.

**The entry is in neither half. The balance is wrong for as long as the snapshot
survives, and every later snapshot inherits it.**

That is the failure this module exists to not have, and it is worth being
precise about why it is nasty: nothing errors, the arithmetic is consistent with
itself, and the only way to see it is to compare against a full scan.

THE FIX -- DRAIN, THEN SUM.

    1. wm    <- MAX(id) visible now. The sequence is monotonic, so any
                transaction that starts after this reads a value ABOVE wm. Ids
                at or below wm therefore belong to a bounded, already-begun set
                of transactions.
    2. xmax0 <- pg_snapshot_xmax(pg_current_snapshot()), the first xid not yet
                assigned. Every transaction that could still write an id <= wm
                has an xid below this.
    3. wait until pg_snapshot_xmin(pg_current_snapshot()) >= xmax0. xmin is the
                oldest still-running xid, so this holds exactly when every
                transaction in flight at step 2 has ended.
    4. NOW sum the entries with id <= wm. Each one has committed or aborted, so
                the sum is complete and can never change again.

Step 3 is the whole difference, and the flag exists so the failure can be
demonstrated rather than described: `take_snapshot(drain=False)` is the obvious
implementation, and `pg_snapshot_test.py` runs it against concurrent writers and
shows the balance going wrong.

THE SECOND HAZARD -- STATISTICS, NOT AN INDEX. Draining makes the checkpoint
correct. It does not make it fast, and the port initially was not: the delta
query `account_id = ? AND id > watermark` read every entry for the account and
threw them all away.

    Bitmap Heap Scan on d_journal_entry e  (actual rows=0)
      Filter: (id > s.watermark_id)
      Rows Removed by Filter: 200000
      Buffers: shared read=2029

That is the same number of buffers the full scan touches. The checkpoint was
arithmetically correct and did no less work -- the failure mode where a cache
looks fine because the ANSWER is right, so no correctness test can see it.

I DIAGNOSED THAT WRONG. It reads like a missing index, and I added
`(account_id, id)` on that basis. Isolating the two causes on 200,000 entries
with the replay depth at zero says otherwise:

    A. no composite index, NO ANALYZE   8,283 buf | 200,000 discarded | ix_d_entry_account
    B. no composite index, ANALYZEd        46 buf |       0 discarded | d_journal_entry_pkey
    C. composite index,    ANALYZEd        31 buf |       0 discarded | d_journal_entry_pkey

The index was never the problem. **A freshly bulk-loaded table has no
statistics**, so the planner estimated 514 rows for the account, chose a bitmap
scan over `(account_id)`, and discarded all 200,000. Once ANALYZE ran it used
the PRIMARY KEY on `id` -- which was there all along -- and the delta became a
range scan.

Case C proves the point against me: with the composite index present the planner
still picks `d_journal_entry_pkey`. It never uses the index I added, so the
index is now gone, and this comment is what replaced it. An unused index is not
free -- it is maintained on every insert, which is a write cost paid for a read
benefit that does not exist.

WHAT THAT MEANS OPERATIONALLY: a checkpoint taken right after a bulk load can be
correct and useless at the same time, and the fix is ANALYZE, not schema. Worth
stating because "it must need an index" is the reflex, and it was mine.

WHAT A SNAPSHOT IS ALLOWED TO BE. Purely a performance checkpoint, exactly as in
`maintenance.py`: deleting every row of `d_balance_snapshot` must change no
answer, only the time taken to reach it. A cache that can change an answer is a
second source of truth, and `balance()` is checked against a full scan in the
tests rather than trusted.
"""
from __future__ import annotations

import time

SNAPSHOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS d_balance_snapshot (
    account_id    TEXT   NOT NULL,
    watermark_id  BIGINT NOT NULL,
    balance_minor BIGINT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, watermark_id)
);
CREATE INDEX IF NOT EXISTS ix_d_snap_acct
    ON d_balance_snapshot(account_id, watermark_id DESC);

"""

# The sum is over entries at or below the watermark. `direction` is D/C and a
# balance is debits minus credits, matching DERIVED_BALANCE_SQL exactly -- if
# these two ever disagree the snapshot stops being a cache.
_SUM_UPTO = """
SELECT account_id,
       COALESCE(SUM(CASE WHEN direction = 'D' THEN amount_minor
                         ELSE -amount_minor END), 0) AS bal
  FROM d_journal_entry
 WHERE id <= %s
 GROUP BY account_id
"""

_DELTA_AFTER = """
SELECT COALESCE(SUM(CASE WHEN direction = 'D' THEN amount_minor
                         ELSE -amount_minor END), 0)
  FROM d_journal_entry
 WHERE account_id = %s AND id > %s
"""

DRAIN_TIMEOUT_S = 30.0


class DrainTimeout(RuntimeError):
    """Writers did not settle within the timeout.

    Raised rather than falling back to the undrained watermark: a snapshot that
    quietly degrades to the broken variant under load is worse than no snapshot,
    because load is exactly when it would be taken.
    """


class StaleSnapshot(RuntimeError):
    """A checkpoint whose watermark is beyond the journal's last entry.

    Found by running the benchmark twice. `DERIVED_SCHEMA` drops and recreates
    the journal tables; `d_balance_snapshot` is not among them, so checkpoints
    from the previous run survived a journal rebuild -- and their watermarks and
    balances described a history that no longer existed. The IDENTITY sequence
    restarted, so the stale watermarks sat far ABOVE every live entry, the delta
    query matched nothing, and `balance()` returned the old checkpoint total.

    The answer was wrong by 5x and nothing complained. It was caught only
    because the benchmark asserts the checkpoint against a full scan on every
    step, which is the reason that assertion is there.

    A watermark is a journal id, so it is only meaningful against the journal
    that issued it. `balance()` therefore refuses a checkpoint that points past
    the end of the journal instead of quietly trusting it.
    """


class SnapshottedLedger:
    """DerivedLedger plus balance checkpoints. Writes are untouched."""

    def __init__(self, derived):
        self.derived = derived
        self.base = derived.base
        self.stats = derived.stats

    def _connect(self):
        return self.base._psycopg.connect(self.base.dsn, autocommit=True)

    def install_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(SNAPSHOT_SCHEMA)

    def clear(self) -> int:
        """Drop every checkpoint. Safe by construction -- a checkpoint is a
        cache, so this changes no answer, only the time taken to reach it.

        Required after a journal rebuild, because watermarks are journal ids.
        """
        with self._connect() as conn:
            return conn.execute("DELETE FROM d_balance_snapshot").rowcount

    # ------------------------------------------------------------- snapshots
    def take_snapshot(self, drain: bool = True,
                      timeout_s: float = DRAIN_TIMEOUT_S) -> dict:
        """Checkpoint every account at a watermark. Returns a small report.

        `drain=False` is the obvious-and-wrong implementation, kept so the
        failure can be reproduced. It is never the default.
        """
        with self._connect() as conn:
            wm = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM d_journal_entry").fetchone()[0]
            xmax0 = conn.execute(
                "SELECT pg_snapshot_xmax(pg_current_snapshot())::text::bigint"
            ).fetchone()[0]

            waited = 0.0
            if drain:
                t0 = time.perf_counter()
                while True:
                    xmin = conn.execute(
                        "SELECT pg_snapshot_xmin(pg_current_snapshot())"
                        "::text::bigint").fetchone()[0]
                    if xmin >= xmax0:
                        break
                    waited = time.perf_counter() - t0
                    if waited > timeout_s:
                        raise DrainTimeout(
                            "writers still in flight after {:.1f}s "
                            "(xmin={} < xmax0={})".format(waited, xmin, xmax0))
                    time.sleep(0.01)
                waited = time.perf_counter() - t0

            rows = conn.execute(_SUM_UPTO, (wm,)).fetchall()
            for account_id, bal in rows:
                conn.execute(
                    "INSERT INTO d_balance_snapshot"
                    " (account_id, watermark_id, balance_minor)"
                    " VALUES (%s,%s,%s)"
                    " ON CONFLICT (account_id, watermark_id)"
                    " DO UPDATE SET balance_minor = EXCLUDED.balance_minor",
                    (account_id, wm, bal))

        return {"watermark": wm, "accounts": len(rows),
                "drained": drain, "drain_wait_s": round(waited, 4)}

    # --------------------------------------------------------------- reading
    def balance(self, account_id: str) -> int:
        """Newest snapshot plus the entries after it, or a full scan if none.

        The fallback must return the identical number. That is asserted in the
        tests rather than assumed, because a snapshot that changes an answer is
        not a cache.
        """
        with self._connect() as conn:
            snap = conn.execute(
                "SELECT watermark_id, balance_minor FROM d_balance_snapshot"
                " WHERE account_id = %s ORDER BY watermark_id DESC LIMIT 1",
                (account_id,)).fetchone()
            if snap is None:
                return self.derived.balance(account_id)
            wm, base = snap

            # A watermark is a journal id. One past the end of the journal means
            # the journal was rebuilt underneath it, and the checkpoint now
            # describes a history that does not exist.
            head = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM d_journal_entry").fetchone()[0]
            if wm > head:
                raise StaleSnapshot(
                    "checkpoint for {} has watermark {} but the journal ends at"
                    " {} -- the journal was rebuilt; clear d_balance_snapshot"
                    .format(account_id, wm, head))
            delta = conn.execute(_DELTA_AFTER, (account_id, wm)).fetchone()[0]
            return int(base) + int(delta)

    def replay_depth(self, account_id: str) -> int:
        """How many entries a read has to replay -- the number the checkpoint
        exists to hold down, and the one that grows without it."""
        with self._connect() as conn:
            snap = conn.execute(
                "SELECT watermark_id FROM d_balance_snapshot"
                " WHERE account_id = %s ORDER BY watermark_id DESC LIMIT 1",
                (account_id,)).fetchone()
            wm = snap[0] if snap else 0
            return int(conn.execute(
                "SELECT count(*) FROM d_journal_entry"
                " WHERE account_id = %s AND id > %s",
                (account_id, wm)).fetchone()[0])

    # Writes go straight through. A checkpoint that changed the write path
    # would give back the contention the derived design was built to remove.
    def post(self, *a, **kw):
        return self.derived.post(*a, **kw)

    def open_account(self, *a, **kw):
        return self.derived.open_account(*a, **kw)
