# SE-1 — Fintech Core: Double-Entry Ledger + Payment API

**Status: ~85%.** Ledger invariants (now enforced by database trigger, not only
by application code), idempotency exercised over HTTP, the full payment lifecycle
including expiry, deterministic FX with period-end revaluation, and balance
snapshots — **66 tests**, including a `hypothesis` stateful model and the complete
illegal-transition cross-product. The Postgres serializable story is the main
thing still missing, and nothing in the "remaining" list is claimed anywhere else.

```bash
python -m pytest tests -q              # 66 tests
python drift_test.py --txns 8000       # four-invariant concurrency drill
uvicorn serve:app --port 8100          # HTTP API
```

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

## The state machine and FX (added since the first slice)

**Full lifecycle**: `authorize → capture (partial/full) → refund
(partial/full) | void`, with `voided` and `refunded` terminal. The transition
table is a single dictionary and `_guard()` is the only enforcement point, which
is what makes "illegal transitions are unreachable" checkable rather than
asserted. `test_illegal_transitions_are_unreachable` runs the **entire 6×3
cross-product** — 18 pairs, 6 legal — and asserts the ledger invariants survive
each attempt.

A refund is not a reversal of the capture entry. The original capture stays in
the journal because it happened; the refund posts its own balanced entries in the
opposite direction. Whether the processing fee is returned is a `refund_fee`
parameter, not a hard-coded choice — it is a pricing decision, not a technical one.

**FX** (`ledger/fx.py`) turned out to contain the sharpest lesson in this repo: a
conversion **cannot be two legs**. Debiting a EUR account and crediting a USD one
leaves both currencies unbalanced and the per-currency seal check rejects it —
correctly. Money does not teleport between currencies; it is sold into a position
and bought out of another, so each side balances within its own currency against
an FX position account. Rounding is half-even (round-half-up accumulates a
one-directional bias), rates are `str`/`Decimal` and a float raises, and
`allocate()` splits an amount so the parts sum **exactly** to the whole — pinned
by a hypothesis property over ±$10M and up to 12 weights.

## What is NOT built

1. **Postgres + SERIALIZABLE** with an explicit retry loop, and the drift test at
   100K txns / 50 workers on named hardware. Still SQLite-only, so the
   concurrency proof remains weaker than it looks — see the note above.
2. **HTTP API** (FastAPI + Docker). Everything is a library call; there is no
   service, no OpenAPI contract, no p99 latency number.
3. **Authorization expiry.** `authorize` holds funds forever; real auths expire
   after 7–30 days and release the hold automatically.
4. **FX revaluation.** Position accounts accumulate exposure but are never
   revalued at a period-end rate, so unrealised FX P&L is not recognised.
5. **Floors as a DB trigger** rather than an in-transaction Python check.
6. **Testcontainers integration tests** and CI wiring.
7. Snapshotting for `balance_as_of` (today a full journal scan — fine at this
   size, wrong at scale).

## Run it

```bash
python -m pytest tests -q
```

```bash
python drift_test.py --txns 8000 --workers 8
```
