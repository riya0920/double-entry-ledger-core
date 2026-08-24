-- PostgreSQL port of the ledger, for the concurrency proof SQLite cannot give.
--
-- WHY THIS FILE EXISTS. The SQLite schema is correct and its invariants hold,
-- but SQLite admits exactly one writer at a time. So "0 violations under 8
-- workers" is weaker evidence than it looks: true write-write interleaving never
-- occurred, and the interesting failure mode -- two transactions reading the
-- same balance, both deciding a floor is satisfied, both committing -- is
-- structurally impossible there. It is not that the test passed; it is that the
-- test could not fail.
--
-- Under SERIALIZABLE, Postgres does allow the interleaving and then detects it:
-- serialization anomaly, SQLSTATE 40001, one transaction aborts, the caller
-- retries. That retry loop is the thing the spec asks for and the thing SQLite
-- cannot motivate.
--
-- WHAT IS DELIBERATELY THE SAME. Money is BIGINT minor units. Balances are
-- derived from the journal and cached by trigger. Corrections are reversing
-- entries -- UPDATE and DELETE on journal_entry raise. The floor check happens
-- at the SEAL boundary, not per entry, for the same reason as in SQLite: a floor
-- is a property of a completed transaction, not of one leg of it.
--
-- WHAT DIFFERS, AND WHY. Postgres has no AUTOINCREMENT (GENERATED AS IDENTITY),
-- no `length()` on the SQLite CHECK dialect quirk (char_length), and triggers
-- are functions plus bindings rather than inline bodies. The `RAISE ABORT`
-- messages carry the same text so the two ports fail identically to a caller.

DROP TABLE IF EXISTS balance_snapshot, idempotency_key, account_balance,
    txn_currency_total, journal_entry, journal_txn, account CASCADE;

-- Functions survive DROP TABLE ... CASCADE: cascade removes the trigger
-- BINDINGS, not the functions they call. Re-running the installer then fails on
-- "already exists with same argument types", which is a confusing way to say
-- "this script is not idempotent".
DROP FUNCTION IF EXISTS entry_is_append_only() CASCADE;
DROP FUNCTION IF EXISTS txn_no_unseal_fn() CASCADE;
DROP FUNCTION IF EXISTS entry_guards() CASCADE;
DROP FUNCTION IF EXISTS entry_after_insert_fn() CASCADE;
DROP FUNCTION IF EXISTS txn_seal_checks() CASCADE;

CREATE TABLE account (
    id                TEXT PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN ('asset','liability','equity','revenue','expense')),
    currency          TEXT NOT NULL CHECK (char_length(currency) = 3),
    floor_minor       BIGINT NOT NULL DEFAULT 0,
    overdraft_allowed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE journal_txn (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor        TEXT NOT NULL,
    reason       TEXT NOT NULL,
    request_id   TEXT NOT NULL,
    sealed       BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE journal_entry (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    txn_id       BIGINT NOT NULL REFERENCES journal_txn(id),
    account_id   TEXT   NOT NULL REFERENCES account(id),
    direction    TEXT   NOT NULL CHECK (direction IN ('D','C')),
    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
    currency     TEXT   NOT NULL CHECK (char_length(currency) = 3)
);
CREATE INDEX ix_entry_txn     ON journal_entry(txn_id);
CREATE INDEX ix_entry_account ON journal_entry(account_id);

CREATE TABLE txn_currency_total (
    txn_id       BIGINT NOT NULL REFERENCES journal_txn(id),
    currency     TEXT   NOT NULL,
    debit_minor  BIGINT NOT NULL DEFAULT 0,
    credit_minor BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (txn_id, currency)
);

CREATE TABLE account_balance (
    account_id    TEXT PRIMARY KEY REFERENCES account(id),
    balance_minor BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE idempotency_key (
    key           TEXT PRIMARY KEY,
    payload_hash  TEXT NOT NULL,
    response_json TEXT NOT NULL,
    status_code   INTEGER NOT NULL,
    txn_id        BIGINT REFERENCES journal_txn(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE balance_snapshot (
    account_id    TEXT NOT NULL REFERENCES account(id),
    as_of         TIMESTAMPTZ NOT NULL,
    balance_minor BIGINT NOT NULL,
    PRIMARY KEY (account_id, as_of)
);

-- ------------------------------------------------------------ immutability
CREATE FUNCTION entry_is_append_only() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'journal_entry is append-only: use a reversing entry';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER entry_no_update BEFORE UPDATE ON journal_entry
    FOR EACH ROW EXECUTE FUNCTION entry_is_append_only();
CREATE TRIGGER entry_no_delete BEFORE DELETE ON journal_entry
    FOR EACH ROW EXECUTE FUNCTION entry_is_append_only();

CREATE FUNCTION txn_no_unseal_fn() RETURNS TRIGGER AS $$
BEGIN
    IF OLD.sealed AND NOT NEW.sealed THEN
        RAISE EXCEPTION 'a sealed transaction cannot be reopened';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER txn_no_unseal BEFORE UPDATE ON journal_txn
    FOR EACH ROW EXECUTE FUNCTION txn_no_unseal_fn();

-- ----------------------------------------------------------- entry guards
CREATE FUNCTION entry_guards() RETURNS TRIGGER AS $$
DECLARE
    is_sealed BOOLEAN;
    acct_ccy  TEXT;
BEGIN
    SELECT sealed INTO is_sealed FROM journal_txn WHERE id = NEW.txn_id;
    IF is_sealed IS NULL THEN
        RAISE EXCEPTION 'entry references a transaction that does not exist';
    END IF;
    IF is_sealed THEN
        RAISE EXCEPTION 'cannot add an entry to a sealed transaction';
    END IF;

    SELECT currency INTO acct_ccy FROM account WHERE id = NEW.account_id;
    IF acct_ccy IS DISTINCT FROM NEW.currency THEN
        RAISE EXCEPTION 'entry currency % does not match account currency %',
            NEW.currency, acct_ccy;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER entry_requires_open_txn BEFORE INSERT ON journal_entry
    FOR EACH ROW EXECUTE FUNCTION entry_guards();

-- ------------------------------------------------- derived totals + balance
-- The cache is maintained here rather than by the application, so a writer that
-- bypasses the library still cannot drift it. invariants.i3 re-derives every
-- balance from the journal to prove the cache has not.
CREATE FUNCTION entry_after_insert_fn() RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO txn_currency_total (txn_id, currency, debit_minor, credit_minor)
    VALUES (NEW.txn_id, NEW.currency,
            CASE WHEN NEW.direction = 'D' THEN NEW.amount_minor ELSE 0 END,
            CASE WHEN NEW.direction = 'C' THEN NEW.amount_minor ELSE 0 END)
    ON CONFLICT (txn_id, currency) DO UPDATE SET
        debit_minor  = txn_currency_total.debit_minor
                       + CASE WHEN NEW.direction = 'D' THEN NEW.amount_minor ELSE 0 END,
        credit_minor = txn_currency_total.credit_minor
                       + CASE WHEN NEW.direction = 'C' THEN NEW.amount_minor ELSE 0 END;

    INSERT INTO account_balance (account_id, balance_minor)
    VALUES (NEW.account_id,
            CASE WHEN NEW.direction = 'D' THEN NEW.amount_minor ELSE -NEW.amount_minor END)
    ON CONFLICT (account_id) DO UPDATE SET
        balance_minor = account_balance.balance_minor
                        + CASE WHEN NEW.direction = 'D' THEN NEW.amount_minor
                               ELSE -NEW.amount_minor END;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER entry_after_insert AFTER INSERT ON journal_entry
    FOR EACH ROW EXECUTE FUNCTION entry_after_insert_fn();

-- --------------------------------------------------------------- the seal
-- Everything that must be true of a COMPLETED transaction is checked here, at
-- the moment it is sealed. Per-entry checking cannot express any of it: a single
-- leg is never balanced, and a floor is a property of the finished posting.
CREATE FUNCTION txn_seal_checks() RETURNS TRIGGER AS $$
DECLARE
    n_entries  INTEGER;
    bad_ccy    TEXT;
    breach     TEXT;
BEGIN
    IF NOT (NEW.sealed AND NOT OLD.sealed) THEN
        RETURN NEW;
    END IF;

    SELECT count(*) INTO n_entries FROM journal_entry WHERE txn_id = NEW.id;
    IF n_entries = 0 THEN
        RAISE EXCEPTION 'cannot seal a transaction with no entries';
    END IF;

    SELECT currency INTO bad_ccy
      FROM txn_currency_total
     WHERE txn_id = NEW.id AND debit_minor <> credit_minor
     LIMIT 1;
    IF bad_ccy IS NOT NULL THEN
        RAISE EXCEPTION 'unbalanced transaction in currency %', bad_ccy;
    END IF;

    SELECT a.id INTO breach
      FROM account a
      JOIN account_balance b ON b.account_id = a.id
     WHERE a.overdraft_allowed = FALSE
       AND b.balance_minor < a.floor_minor
       AND a.id IN (SELECT account_id FROM journal_entry WHERE txn_id = NEW.id)
     LIMIT 1;
    IF breach IS NOT NULL THEN
        RAISE EXCEPTION 'floor breach: account balance below its floor at seal';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER txn_seal_guard BEFORE UPDATE OF sealed ON journal_txn
    FOR EACH ROW EXECUTE FUNCTION txn_seal_checks();
