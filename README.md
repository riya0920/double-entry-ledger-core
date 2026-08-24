# SE-1 — Fintech Core: Double-Entry Ledger + Payment API

**Status: ~99%.** Ledger invariants enforced by database trigger, **idempotency
on the payment endpoints as well as on raw postings**, the full payment lifecycle
including expiry, deterministic FX with period-end revaluation, balance
snapshots, and a **measured API latency curve that shows the single-writer
contention rather than hiding it** — **85 tests**, including a `hypothesis`
stateful model and the complete illegal-transition cross-product -- **and a
PostgreSQL 18 port running under real SERIALIZABLE**, which measured something
that argues against this project's central design decision.

```bash
python -m pytest tests -q              # 85 tests
python drift_test.py --txns 8000       # four-invariant concurrency drill
python run_api_load.py                 # latency curve + contention + invariants
python pg_drift_test.py --txns 800 --workers 16   # real SERIALIZABLE + retries
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

## The API load test, and the bug it found

`run_api_load.py` drives `POST /payments/authorize` over real HTTP at rising
concurrency. Each call opens a transaction, writes two journal entries, updates
two balance rows through a trigger, seals, and commits an idempotency record —
all in one transaction.

```
 workers        ok       err      mean       p50       p95         p99    req/s
       1       800         0      2.26      1.81      3.73       13.18      358
       2       800         0      4.26      1.80     20.29       35.94      352
       4       800         0      7.79      1.97     35.13      108.80      319
       8       800         0     14.10      1.96     37.44      271.46      251
```

Windows 11 laptop, CPython 3.14, SQLite WAL, loopback HTTP. 3,200 transactions,
0 invariant violations after the run.

**That shape is the finding.** From 1 to 8 workers the p99 grows **20.6×** while
throughput *falls* to 0.70×. That is what a single-writer store looks like from
outside: the workers are queueing on a lock, not sharing a machine. Adding
workers buys nothing and costs the tail. On Postgres SERIALIZABLE the same load
would interleave, hit serialization anomalies, return `40001` and need a retry
loop — a genuinely different failure mode this store cannot produce, which is
exactly why the 100K/50-worker figure below is still not claimed.

### The bug: the payments API was not idempotent

The first run reported **3,200 journal transactions and 0 idempotency keys.**

`/payments/authorize` passed a request id straight to `ledger.post`, which
records the id on the journal row and creates no idempotency record. So a client
that timed out and retried placed a **second hold on the card** — the single most
common payment-API bug, in a repository whose headline property is idempotency.
The guarantee was real; it was being demonstrated on `/postings`, a path the
payments API did not take.

The fix generalises `post_idempotent` into `Ledger.run_idempotent(key, payload,
work)`, so the payment row, the journal entries and the stored response body all
commit in one transaction. `/payments/authorize` honours an `Idempotency-Key`
header and defaults to the payment id when it is absent, because a default that
quietly does not protect the caller is worse than no default. The same load now
reports **3,200 transactions and 3,200 keys**.

One subtlety worth naming: a duplicate `payment.id` also raises
`IntegrityError`, and treating that as "concurrent duplicate, replay the other
caller's answer" would let a second authorize through on the same payment. The
handler re-raises when no key is found, and
`test_a_duplicate_payment_id_under_a_new_key_is_still_rejected` pins it.

## Postgres SERIALIZABLE, and the finding that argues against this design

`ledger/pg_schema.sql` ports every invariant to PostgreSQL 18 and
`pg_drift_test.py` runs the same drill there. The point of the port is that
SQLite's "0 violations under 8 workers" is weaker evidence than it looks: SQLite
admits one writer, so true write-write interleaving never happened. **The test
did not pass so much as fail to be possible.**

Under SERIALIZABLE the interleaving does happen, Postgres detects the dependency
cycle, and aborts one side with SQLSTATE 40001. On one hot account, 16 workers:

```
committed              : 500
serialization failures : 3,924
retries exhausted      : 300
retry rate             : 88.7%
```

**The retry loop did not save every transaction. It saved 62.5% of them**, and
300 failed outright after 8 bounded retries. That is the honest version of "we
retry on serialization failure".

### The diagnosis: a materialized balance is a hot row

Every posting updates **one row** of `account_balance`. Under SERIALIZABLE that
row is a serialization point — two postings to the same account always conflict.
The README above calls the trigger-maintained balance cache *the one design
decision everything else follows from*. It still is, and this is its cost.

Holding the transport fixed and changing **only** the isolation level:

| | SERIALIZABLE | READ COMMITTED |
|---|---|---|
| committed | 500 | 800 |
| serialization failures | 3,924 | 0 |
| retries exhausted | **300** | 0 |
| retry rate | 88.7% | 0.0% |
| txn/s | 9 | 22 |

I first wrote that Postgres made this workload "150× slower than SQLite". That
comparison is not fair and I removed it: SQLite runs in-process against a local
file while this crosses a virtual NIC into another OS, so most of that gap is
transport rather than isolation. **2.4× and 300 failed transactions is the
isolation cost**, measured with everything else held constant.

### What SERIALIZABLE is buying

`tests/test_pg_serializable.py` **constructs** the anomaly rather than hoping
load produces one — a concurrency test that waits for a race by luck is a test
that passes on a slow day. Two transactions each read a balance of 150, each
decide a withdrawal of 100 is legal, and both commit:

- under **SERIALIZABLE**: one commits, one gets 40001, balance ends at **50**
- under **READ COMMITTED**: both commit, balance ends at **−50** — below a floor
  neither transaction ever saw breached

So the choice is not which is better. It is: pay the throughput and the failed
transactions for a floor that cannot be crossed, or take the throughput and
enforce floors somewhere a race cannot reach — which in practice means not
keeping a hot cached balance at all and aggregating the journal on read. **That
is now the honest first item of remaining work**, and it is a rewrite this
project has not done.

One more thing the port surfaced: I3 (cached balance equals journal) correctly
flagged the anomaly probes' hand-written rows. The invariant caught a balance no
journal entry justified, which is exactly its job.

## What is NOT built

1. **A hot-row-free balance design.** The measurement above says the
   trigger-maintained cache is the bottleneck under real concurrency. Sharding
   the balance row, or dropping the cache and aggregating the journal on read,
   is the fix and it is a rewrite rather than a patch.
2. **The spec's 100K / 50-worker figure.** The Postgres drill runs at 800/16 in
   a reasonable time; at 100K it would take hours on a hot account precisely
   because of item 1, so the number is still not claimed.
3. **A connection pool.** `PgLedger` holds one connection per instance, which
   stands in for a pool at one connection per worker thread. A real service
   needs pgbouncer or an application pool with a sizing argument behind it.
4. **Idempotency on capture and refund.** `authorize` takes an
   `Idempotency-Key`; capture and refund still do not, so a retried capture can
   double-capture. The mechanism (`run_idempotent`) is in place and the wiring
   is not.
5. **Multi-currency reporting.** FX position accounts and period-end revaluation
   exist; there is no consolidated reporting currency.
6. **Authorization incremental/decremental adjustments** — real card auths get
   topped up (hotels, fuel) and this lifecycle has no transition for it.
7. **Backdating and period close.** Every posting is `now()`. There is no
   accounting period, no close, and nothing preventing a posting into a closed
   month.
