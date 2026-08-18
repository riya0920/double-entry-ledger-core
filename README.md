# SE-1 — Fintech Core: Double-Entry Ledger + Payment API

**Status: ~20% slice.** The ledger invariants and the idempotency semantics are
built and proven. The API surface, the full state machine, and the Postgres
serializable concurrency story are not. Both lists are below, and nothing in the
"remaining" list is claimed anywhere else in this repo.

## The one design decision everything else follows from

There is no `accounts.balance` column that anyone writes to. The journal is the
source of truth; `account_balance` is a materialized cache maintained by trigger,
and `invariants.py:i3` re-derives every balance from the journal to prove the
cache has not drifted. Corrections are reversing entries — `journal_entry` raises
on `UPDATE` and on `DELETE`.

Money is `INTEGER` minor units everywhere. `grep -i "REAL\|FLOAT" ledger/schema.sql`
returns nothing, and `Entry.__post_init__` refuses a float at the type boundary
(`test_floats_are_refused_at_the_type_boundary`).

## What is built

| Piece | Where | Proven by |
|---|---|---|
| Immutable journal, append-only enforced in DB | `ledger/schema.sql` | `test_journal_is_append_only` |
| Σdebits = Σcredits **per currency**, enforced by trigger at seal | `schema.sql:txn_seal_must_balance` | `test_database_rejects_unbalanced_seal_even_if_app_code_is_bypassed` |
| Derived balances + incremental cache | `schema.sql:entry_after_insert` | invariant I3 |
| Account floors, checked inside the write txn | `core.py:_check_floors` | `test_floor_breach_rolls_back_whole_posting` |
| Idempotency with key↔payload binding | `core.py:post_idempotent` | `test_same_key_different_payload_is_a_conflict_not_a_replay` |
| Crash between journal-write and response | same | `test_crash_between_journal_write_and_response` |
| Auth-hold accounting (held ≠ moved) | `ledger/payments.py` | `test_authorization_holds_funds_without_paying_the_merchant` |
| Partial capture with fee split | `payments.py:capture` | `test_partial_capture_releases_only_the_captured_slice` |
| As-of balance reconstruction from journal alone | `core.py:balance_as_of` | `test_balance_as_of_reconstructs_from_journal_alone` |
| Four-invariant drift test | `drift_test.py` | run it |

### The idempotency point that most implementations miss

The journal write **and the serialized response body** commit in the same
database transaction. That is why a crash after commit but before the client ever
sees a response still replays byte-identical on retry — there is no window where
the effect exists and the recorded answer does not. Concurrent duplicates are
arbitrated by the primary key on `idempotency_key`, not by a read-then-write
check (which is TOCTOU and would admit two effects).

Same key + **different** payload returns a conflict and deliberately does *not*
return the first caller's result — returning it would tell caller two that its
different request succeeded.

## Measured results (methodology attached — this is a floor, not a benchmark)

Run: `python drift_test.py --txns 8000 --workers 8`
Hardware: Windows 11, single laptop, CPython 3.14, SQLite 3.50.4 in WAL mode.

```
requests ok          : 7829
idempotent replays   : 383   (same key+payload -> original result)
409 conflicts        : 171   (same key, different payload)
sealed transactions  : 7446
idempotency keys     : 7446  (equal to sealed txns => invariant I4)
elapsed              : 5.63s (1323 txn/s, single-writer store)
ALL INVARIANTS HOLD (I1 balance, I2 floors, I3 derived, I4 idempotency)
```

**What this number is not.** SQLite admits one writer at a time, so 1,323 txn/s
is a correctness-harness throughput, not a claim about the design's parallel
capacity, and "0 violations" here is weaker evidence than it would be on
Postgres because true write-write interleaving never occurs. The interesting
failure mode — serialization anomaly → `40001` → retry loop — cannot happen on
this store. Porting to Postgres `SERIALIZABLE` and re-running at 100K/50 workers
is the first item of remaining work, and the 100K figure in the spec is not
claimed until that runs.

## What is NOT built (the other 80%)

1. **Postgres + SERIALIZABLE** with an explicit retry loop, and the drift test at
   100K txns / 50 workers on named hardware. Currently SQLite-only.
2. **HTTP API** (FastAPI + Docker). Everything is a library call today; there is
   no service, no OpenAPI contract, no p99 latency number.
3. **State machine completion**: refund (full/partial), void, expiry, and
   `hypothesis` property tests proving illegal transitions are unreachable. Only
   `authorize` and `capture` exist; `ALLOWED` in `payments.py` has two rows.
4. **Multi-currency FX**: per-currency accounts and the balance check are in, but
   the FX gain/loss account, round-half-even with deterministic remainder
   assignment, and the "books balance to the cent in every currency" test are not.
5. **Floors as a DB trigger** rather than an in-transaction Python check.
6. **Testcontainers integration tests** and CI wiring.
7. Snapshotting for `balance_as_of` (today it is a full journal scan — fine at
   this size, wrong at scale).

## Run it

```bash
python -m pytest tests -q
```

```bash
python drift_test.py --txns 8000 --workers 8
```
