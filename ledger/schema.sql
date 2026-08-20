-- SE-1 Fintech Core: immutable double-entry journal.
--
-- Design rules encoded here (not in application code):
--   1. Money is INTEGER minor units. No REAL columns exist anywhere in this file.
--   2. journal_entry is append-only: UPDATE and DELETE raise.
--   3. A transaction cannot be sealed unless SUM(debits) = SUM(credits) *per currency*.
--   4. Nothing may be appended to a sealed transaction.
--   5. Balances are DERIVED. account_balance is a materialized cache maintained
--      incrementally by trigger; the journal is the source of truth and
--      invariants.py re-derives from it to prove the cache has not drifted.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS account (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN ('asset','liability','equity','revenue','expense')),
    currency          TEXT NOT NULL CHECK (length(currency) = 3),
    -- Lowest balance (debit-positive, minor units) this account may reach.
    floor_minor       INTEGER NOT NULL DEFAULT 0,
    overdraft_allowed INTEGER NOT NULL DEFAULT 0 CHECK (overdraft_allowed IN (0,1)),
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS journal_txn (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    actor        TEXT NOT NULL,          -- who caused this posting
    reason       TEXT NOT NULL,          -- business event name
    request_id   TEXT NOT NULL,          -- trace id of the causing request
    sealed       INTEGER NOT NULL DEFAULT 0 CHECK (sealed IN (0,1))
);

CREATE TABLE IF NOT EXISTS journal_entry (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id       INTEGER NOT NULL REFERENCES journal_txn(id),
    account_id   TEXT    NOT NULL REFERENCES account(id),
    direction    TEXT    NOT NULL CHECK (direction IN ('D','C')),
    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
    currency     TEXT    NOT NULL CHECK (length(currency) = 3)
);
CREATE INDEX IF NOT EXISTS ix_entry_txn     ON journal_entry(txn_id);
CREATE INDEX IF NOT EXISTS ix_entry_account ON journal_entry(account_id);

-- Running per-(txn, currency) totals, maintained by trigger. The seal check reads this.
CREATE TABLE IF NOT EXISTS txn_currency_total (
    txn_id       INTEGER NOT NULL REFERENCES journal_txn(id),
    currency     TEXT    NOT NULL,
    debit_minor  INTEGER NOT NULL DEFAULT 0,
    credit_minor INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (txn_id, currency)
);

-- Materialized, incrementally maintained. Debit-positive convention:
--   balance = SUM(debits) - SUM(credits)
CREATE TABLE IF NOT EXISTS account_balance (
    account_id    TEXT PRIMARY KEY REFERENCES account(id),
    balance_minor INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS idempotency_key (
    key           TEXT PRIMARY KEY,
    payload_hash  TEXT NOT NULL,
    -- Response is persisted in the SAME database transaction as the journal write,
    -- so a crash anywhere after commit still replays the original result.
    response_json TEXT NOT NULL,
    status_code   INTEGER NOT NULL,
    txn_id        INTEGER REFERENCES journal_txn(id),
    created_at    TEXT NOT NULL
);

-- ---------------------------------------------------------------- append-only
CREATE TRIGGER IF NOT EXISTS entry_no_update
BEFORE UPDATE ON journal_entry
BEGIN
    SELECT RAISE(ABORT, 'journal_entry is append-only: correct with a reversing entry');
END;

CREATE TRIGGER IF NOT EXISTS entry_no_delete
BEFORE DELETE ON journal_entry
BEGIN
    SELECT RAISE(ABORT, 'journal_entry is append-only: correct with a reversing entry');
END;

CREATE TRIGGER IF NOT EXISTS txn_no_unseal
BEFORE UPDATE OF sealed ON journal_txn
WHEN OLD.sealed = 1
BEGIN
    SELECT RAISE(ABORT, 'sealed transactions are immutable');
END;

-- ------------------------------------------------- entries only on open txns
CREATE TRIGGER IF NOT EXISTS entry_requires_open_txn
BEFORE INSERT ON journal_entry
WHEN (SELECT sealed FROM journal_txn WHERE id = NEW.txn_id) = 1
BEGIN
    SELECT RAISE(ABORT, 'cannot append to a sealed transaction');
END;

-- Entry currency must match the account it posts to.
CREATE TRIGGER IF NOT EXISTS entry_currency_matches_account
BEFORE INSERT ON journal_entry
WHEN (SELECT currency FROM account WHERE id = NEW.account_id) <> NEW.currency
BEGIN
    SELECT RAISE(ABORT, 'entry currency does not match account currency');
END;

-- ------------------------------------------- maintain totals + cached balance
CREATE TRIGGER IF NOT EXISTS entry_after_insert
AFTER INSERT ON journal_entry
BEGIN
    INSERT INTO txn_currency_total (txn_id, currency, debit_minor, credit_minor)
    VALUES (NEW.txn_id, NEW.currency,
            CASE NEW.direction WHEN 'D' THEN NEW.amount_minor ELSE 0 END,
            CASE NEW.direction WHEN 'C' THEN NEW.amount_minor ELSE 0 END)
    ON CONFLICT(txn_id, currency) DO UPDATE SET
        debit_minor  = debit_minor  + CASE NEW.direction WHEN 'D' THEN NEW.amount_minor ELSE 0 END,
        credit_minor = credit_minor + CASE NEW.direction WHEN 'C' THEN NEW.amount_minor ELSE 0 END;

    INSERT INTO account_balance (account_id, balance_minor)
    VALUES (NEW.account_id,
            CASE NEW.direction WHEN 'D' THEN NEW.amount_minor ELSE -NEW.amount_minor END)
    ON CONFLICT(account_id) DO UPDATE SET
        balance_minor = balance_minor
            + CASE NEW.direction WHEN 'D' THEN NEW.amount_minor ELSE -NEW.amount_minor END;
END;

-- ------------------------------------------------------------- the seal check
-- This is the constraint that makes the ledger a ledger. Sealing fails unless
-- every currency touched by the transaction balances exactly.
CREATE TRIGGER IF NOT EXISTS txn_seal_must_balance
BEFORE UPDATE OF sealed ON journal_txn
WHEN NEW.sealed = 1 AND OLD.sealed = 0
  AND EXISTS (SELECT 1 FROM txn_currency_total
              WHERE txn_id = NEW.id AND debit_minor <> credit_minor)
BEGIN
    SELECT RAISE(ABORT, 'unbalanced transaction: sum(debits) <> sum(credits) for some currency');
END;

CREATE TRIGGER IF NOT EXISTS txn_seal_needs_entries
BEFORE UPDATE OF sealed ON journal_txn
WHEN NEW.sealed = 1 AND OLD.sealed = 0
  AND NOT EXISTS (SELECT 1 FROM txn_currency_total WHERE txn_id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'cannot seal a transaction with no entries');
END;

-- ---------------------------------------------------------- floor as a trigger
-- Invariant I2 was enforced by a Python check inside the write transaction.
-- That is correct as long as every writer goes through core.post(), which is a
-- guarantee about code rather than about data. In the database it holds for any
-- writer, including a DBA at a prompt at 2am.
--
-- The check runs at SEAL, not per entry, for two reasons:
--
--   CORRECTNESS. A floor is a property of a completed transaction, not of an
--   individual leg. A transaction that debits an account below its floor and
--   credits it back within the same balanced set never leaves the account
--   overdrawn, and a per-entry check would reject it depending on the order the
--   legs happen to be inserted in.
--
--   DETERMINISM. SQLite does not define the firing order of multiple AFTER
--   INSERT triggers on one table. A per-entry floor check that reads
--   account_balance therefore races entry_after_insert, which is the trigger
--   that maintains it -- and a check that silently passes because it ran first
--   is worse than no check. An earlier version of this file had exactly that
--   bug: the trigger existed, the test asserted it fired, and it did not.
CREATE TRIGGER IF NOT EXISTS txn_seal_respects_floors
BEFORE UPDATE OF sealed ON journal_txn
WHEN NEW.sealed = 1 AND OLD.sealed = 0
 AND EXISTS (
     SELECT 1
       FROM account a
       JOIN account_balance b ON b.account_id = a.id
      WHERE a.overdraft_allowed = 0
        AND b.balance_minor < a.floor_minor
        AND a.id IN (SELECT account_id FROM journal_entry WHERE txn_id = NEW.id)
 )
BEGIN
    SELECT RAISE(ABORT, 'floor breach: account balance below its floor at seal');
END;

-- ------------------------------------------------------------------ snapshots
-- balance_as_of() scans the whole journal, which is fine at this size and wrong
-- at scale. A snapshot is a checkpoint: the balance at a point in time, so an
-- as-of query reads the latest snapshot at or before the target and replays only
-- the entries after it.
--
-- Snapshots are DERIVED and disposable. Deleting every row here changes no
-- answer, only the time taken to compute it -- which is the property that makes
-- a cache safe. If a snapshot could change an answer it would be a second source
-- of truth, and the journal would no longer be the ledger.
CREATE TABLE IF NOT EXISTS balance_snapshot (
    account_id    TEXT NOT NULL REFERENCES account(id),
    as_of         TEXT NOT NULL,
    balance_minor INTEGER NOT NULL,
    last_txn_id   INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (account_id, as_of)
);
CREATE INDEX IF NOT EXISTS ix_snapshot_account ON balance_snapshot(account_id, as_of);
