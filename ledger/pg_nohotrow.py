"""The hot-row hypothesis, tested rather than asserted.

`pg_drift_test.py` measured an 88.7% serialization-failure rate on a single hot
account and diagnosed the cause: every posting updates ONE row of
`account_balance`, so under SERIALIZABLE that row is a serialization point and
two postings to the same account always conflict.

The README then said the fix is "not keeping a hot cached balance at all, and
aggregating the journal on read instead". **That was a hypothesis with no
experiment behind it**, and this module is the experiment.

THE TWO DESIGNS

  CACHED    a trigger maintains `account_balance`; reading a balance is one
            indexed row. Every write touches that row.
  DERIVED   no cache at all; a balance is `SUM(debits) - SUM(credits)` over the
            journal. Writes only ever APPEND, and appends to different rows do
            not conflict.

WHAT THE TRADE IS. Derived moves the cost from write to read, and the read cost
grows with history: at a million entries per account, every balance query scans
a million rows. Real systems resolve that with periodic snapshots -- balance at
a checkpoint plus the entries since -- which this repo already has machinery for
in `balance_snapshot`. So "derived" here is the write-path experiment, not a
complete design, and the read-cost column in the report is the part that says
why.

WHAT WOULD FALSIFY THE HYPOTHESIS. If the derived design shows a comparable
serialization-failure rate, the cache is not the bottleneck and the diagnosis
was wrong. That is a real possibility -- the entries themselves share a
transaction row and an index -- and it is why this is measured.
"""
from __future__ import annotations

DERIVED_SCHEMA = """
DROP TABLE IF EXISTS d_journal_entry, d_journal_txn, d_account CASCADE;
DROP FUNCTION IF EXISTS d_entry_append_only() CASCADE;

CREATE TABLE d_account (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL,
    currency          TEXT NOT NULL CHECK (char_length(currency) = 3),
    floor_minor       BIGINT NOT NULL DEFAULT 0,
    overdraft_allowed BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE d_journal_txn (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor      TEXT NOT NULL,
    reason     TEXT NOT NULL,
    request_id TEXT NOT NULL,
    sealed     BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE d_journal_entry (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    txn_id       BIGINT NOT NULL REFERENCES d_journal_txn(id),
    account_id   TEXT   NOT NULL REFERENCES d_account(id),
    direction    TEXT   NOT NULL CHECK (direction IN ('D','C')),
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    currency     TEXT   NOT NULL
);
CREATE INDEX ix_d_entry_account ON d_journal_entry(account_id);

-- Still append-only. Dropping the cache does not mean dropping the invariant
-- that corrections are reversing entries.
CREATE FUNCTION d_entry_append_only() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'journal_entry is append-only: use a reversing entry';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER d_entry_no_update BEFORE UPDATE ON d_journal_entry
    FOR EACH ROW EXECUTE FUNCTION d_entry_append_only();
CREATE TRIGGER d_entry_no_delete BEFORE DELETE ON d_journal_entry
    FOR EACH ROW EXECUTE FUNCTION d_entry_append_only();
"""

DERIVED_BALANCE_SQL = """
SELECT COALESCE(SUM(CASE WHEN direction = 'D' THEN amount_minor
                         ELSE -amount_minor END), 0)
  FROM d_journal_entry WHERE account_id = %s
"""


class DerivedLedger:
    """Same postings, no materialized balance. Writes only append."""

    def __init__(self, base):
        self.base = base                       # a PgLedger, for its retry loop
        self.stats = base.stats

    def install_schema(self) -> None:
        with self.base._psycopg.connect(self.base.dsn, autocommit=True) as conn:
            conn.execute(DERIVED_SCHEMA)

    def open_account(self, account_id, kind, currency, floor_minor=0,
                     overdraft_allowed=False):
        with self.base._psycopg.connect(self.base.dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO d_account (id, kind, currency, floor_minor,"
                " overdraft_allowed) VALUES (%s,%s,%s,%s,%s)"
                " ON CONFLICT (id) DO NOTHING",
                (account_id, kind, currency, floor_minor, overdraft_allowed))

    def post(self, entries, actor, reason, request_id) -> int:
        def work(conn):
            txn = conn.execute(
                "INSERT INTO d_journal_txn (actor, reason, request_id)"
                " VALUES (%s,%s,%s) RETURNING id",
                (actor, reason, request_id)).fetchone()[0]
            for e in entries:
                conn.execute(
                    "INSERT INTO d_journal_entry (txn_id, account_id, direction,"
                    " amount_minor, currency) VALUES (%s,%s,%s,%s,%s)",
                    (txn, e["account_id"], e["direction"], e["amount_minor"],
                     e["currency"]))
            conn.execute("UPDATE d_journal_txn SET sealed = TRUE WHERE id = %s",
                         (txn,))
            return txn

        return self.base.run_serializable(work)

    def balance(self, account_id: str) -> int:
        with self.base._psycopg.connect(self.base.dsn, autocommit=True) as conn:
            return int(conn.execute(DERIVED_BALANCE_SQL, (account_id,)).fetchone()[0])

    def entry_count(self, account_id: str) -> int:
        with self.base._psycopg.connect(self.base.dsn, autocommit=True) as conn:
            return int(conn.execute(
                "SELECT count(*) FROM d_journal_entry WHERE account_id = %s",
                (account_id,)).fetchone()[0])
