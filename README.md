# SE-1 — Fintech Core: Double-Entry Ledger + Payment API

**Status: ~99%.** Ledger invariants enforced by database trigger, **idempotency
on the payment endpoints as well as on raw postings**, the full payment lifecycle
including expiry, deterministic FX with period-end revaluation, balance
snapshots, and a **measured API latency curve that shows the single-writer
contention rather than hiding it** — **109 tests**, including a `hypothesis`
stateful model and the complete illegal-transition cross-product -- **and a
PostgreSQL 18 port running under real SERIALIZABLE**, which measured something
that argues against this project's central design decision.

```bash
python -m pytest tests -q              # 109 tests
python drift_test.py --txns 8000       # four-invariant concurrency drill
python run_api_load.py                 # latency curve + contention + invariants
python pg_drift_test.py --txns 800 --workers 16   # real SERIALIZABLE + retries
python pg_hotrow_test.py --txns 480 --workers 16  # cached vs derived balance
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

## The hot-row hypothesis, tested

The section above diagnosed the contention as the trigger-maintained
`account_balance` row and asserted the fix — aggregate the journal on read
instead. **That was reasoning with no experiment behind it**, so
`pg_hotrow_test.py` runs both designs against the same hot account, same
isolation, same retry loop.

| | cached balance row | derived on read |
|---|---|---|
| committed | 355 | **477** |
| serialization failures | 1,793 | 489 |
| **retries exhausted** | **125** | **3** |
| retry rate | 83.5% | 50.6% |
| txn/s | 21 | **127** |

**Supported, with a qualification the first draft did not have.** Removing the
cache cut the retry rate roughly in half and took transactions lost outright from
125 to 3. But 50.6% is not zero: the postings still share a transaction table, a
sequence and an index, and SERIALIZABLE finds dependencies there too. **The
balance row was the dominant cause, not the only one** — a weaker claim than the
one I published, and the one the measurement supports.

The magnitude moves run to run (cached 77–84%, derived 33–51%) because the box is
loaded, so the test asserts the *direction* and not a threshold. Asserting a
threshold would be asserting the load.

### What the derived design costs

Measured on a warm connection, 25 repeats:

| | cached | derived |
|---|---|---|
| balance read | 1.41 ms | 1.90 ms |
| rows scanned | 1 | 477 |

The first attempt at this measurement opened a fresh connection per read and
reported the *cached* read as slower — impossible when one scans a single row and
the other scans hundreds. It was timing SCRAM handshakes, not queries.

The cost is real and it grows with history: at a million entries the balance
query scans a million rows, on every authorization. **The production answer is
neither design alone — it is periodic snapshots**, balance at a checkpoint plus
the entries since. This repo already has `balance_snapshot` and
`balance_as_of_snapshotted` for exactly that shape on the SQLite side. Porting it
is the real work, and this experiment is what says it is worth doing.

## Period close, consolidation, and authorization adjustments

Three gaps closed together, because each one is a thing a real ledger is asked
for on day two.

**`ledger/periods.py` — a close that changes what the ledger accepts.** Every
posting was stamped `datetime('now')`, so "is January final?" had no answer:
nothing stopped a January-dated posting landing tomorrow. A close is not a flag
on a report, it is a rule — after it, a posting whose *effective* date falls in
that period is refused.

That needs effective date to be a **separate column** from `created_at`. A
backdated correction is January money recorded in February, and conflating the
two means you can never backdate at all — which sounds safe and is exactly why
people post to the wrong period instead.

The two policies for a late item are implemented separately and neither is the
default: **restate** reopens January and changes a published number;
**adjust forward** leaves January alone and books it in February. Reopening
requires a named approver *and* a recorded reason — "reopened" with no reason is
the audit-trail equivalent of no audit trail.

**`ledger/reporting.py` — one number, and the residual it creates.** With EUR
and USD balances side by side there is no "total assets" until somebody names a
rate. Consolidation needs **two**: closing for balance-sheet items, average for
income-statement items. Translating revenue at the closing rate makes a month's
earnings move because the currency moved on the last day.

The residual between the two rates is the **cumulative translation adjustment**,
and it belongs in equity rather than P&L — it is not a gain anybody realised.
**A consolidation reporting no CTA has almost certainly used one rate for
everything**, so it is a named line and a test asserts a single-currency book
produces exactly zero.

**`ledger/adjustments.py` — authorizations that change size.** Hotels increment
for room service, fuel pumps adjust to the real amount, restaurants increment
for the tip. Without this transition the workarounds are both wrong: void and
re-authorize loses the original authorization date that the scheme's expiry
clock runs from, and capturing the difference moves money before the stay ends.

An adjustment is an authorize of a different size — the same two accounts,
signed — so it never pays the merchant. The rule that matters: **a decrement can
never go below what is already captured.** Partial capture makes that reachable
(capture 80, decrement to 50, and the book says you hold less than you have paid
out), and a test drives exactly that sequence.

**Capture and refund now take an `Idempotency-Key` too.** A retried capture is
worse than a retried authorize: an authorize holds funds, a capture *moves*
them, and because partial capture is legal the second call usually finds
remaining authorization to consume. Nothing about it looks wrong.

## What is NOT built

1. **Snapshot-backed balances on Postgres.** The experiment above shows the
   derived design fixes most of the contention and moves the cost to the read
   path, where it grows with history. Neither extreme is the answer; periodic
   snapshots are, and the SQLite side already has that machinery. Porting it is
   the remaining work, and it is now a specified job rather than a hunch.
2. **The spec's 100K / 50-worker figure.** The Postgres drill runs at 800/16 in
   a reasonable time; at 100K it would take hours on a hot account precisely
   because of item 1, so the number is still not claimed.
3. ~~**A connection pool.**~~ **partly done, and it exposed a real bug.**
   `Ledger` held a thread-local connection to `":memory:"` — which names a
   database **private to the connection**, so every thread got its own EMPTY
   database. Thread A ran the schema; thread B opened a blank one with the same
   name. In-process tests never saw it because they build and use the Ledger on
   one thread; it surfaced the moment an HTTP request touched the idempotency
   table and got `no such table: idempotency_key` on a schema that plainly
   creates it. Fixed with a shared-cache URI plus a keepalive connection (a
   shared in-memory database dies with its last connection).

   Still per-thread rather than pooled, deliberately: a sqlite3 connection may
   not be shared across threads, and the interesting contention is the write
   lock — a pool would queue on the same lock one step earlier and measure the
   queue instead of the database. Superseded note: `PgLedger` holds one
   connection per instance, which
   stands in for a pool at one connection per worker thread. A real service
   needs pgbouncer or an application pool with a sizing argument behind it.
4. ~~**Idempotency on the HTTP capture and refund endpoints.**~~ **DONE** —
   and the situation was backwards: `Idempotency-Key` was REQUIRED on
   `/postings` and absent from capture, refund and void, so the most dangerous
   endpoints were the only unprotected ones. Every capture used the hardcoded
   request id `"req-cap-<payment_id>"`, so two different captures shared one and
   no retry was ever detected.

   All three now require the header, bind the payload to the key (409 on reuse
   with a different amount), and return `Idempotent-Replay`. `void` gained an
   `idempotency_key` — it was the only one of the three without one, and "the
   state machine happens to reject the second call" is a different guarantee
   from "this is idempotent": the caller got a 422 for a retry that in fact
   succeeded. Verified over HTTP: a retried capture replays the same txn id and
   `captured_minor` stays at 4,000. Superseded note: The library
   functions take an `Idempotency-Key` now; `serve.py` exposes it only on
   `/payments/authorize`, so the guarantee is available to a library caller and
   not yet to an HTTP one.
5. ~~**Rates from a source.**~~ **DONE** — `ledger/rates.py` is a rate STORE
   with the properties a consolidation actually needs: dated (a rate is a fact
   about an instant), **immutable once published** (a restatement is a new
   effective date, not an edit, or last quarter's report stops reproducing),
   sourced (`ecb | provider | manual` — a manual override is legitimate and is
   the one an auditor asks about), Decimal-only enforced at the boundary, and it
   **raises on a missing rate** rather than defaulting to 1.0 or carrying the
   last one forward, either of which produces a consolidation that balances and
   is wrong.

   `fetch_ecb` pulls real ECB reference rates and is explicit — never called at
   import or by a reporting run, because a consolidation whose numbers depend on
   whether a web request succeeded is not reproducible. It inverts EUR-per-unit
   to unit-per-EUR in one place, and labels the average as a stand-in because
   the daily file has no period mean. Superseded note: `RateSet` takes closing
   and average rates as
   declared inputs. Nothing fetches them, and nothing checks them against a
   published fixing -- a consolidation is only as good as the rate table
   somebody typed in.
6. ~~**Scheme-specific adjustment rules.**~~ **DONE** — `ledger/schemes.py`,
   deliberately OUTSIDE the ledger. `adjust` enforces what is true of
   double-entry (a decrement may never fall below what is captured); this
   enforces what is true of Visa in 2026. A limit inside the ledger is a limit
   the ledger cannot be operated without, and scheme rules change quarterly.

   Constraints by count, by cumulative ratio **against the ORIGINAL** (three 15%
   increments on a running total is 52%, not 45%), by MCC eligibility, and by an
   expiry clock that runs from the original authorization — the rule most likely
   to be got wrong, and re-introducing it would undo what `adjustments.py`
   avoided by not re-authorizing. A scheme with no incremental support says so
   rather than being treated as a small limit. Limits are `ASSUMED_`, not
   quoted: real ones are licensed and versioned. Superseded note: The mechanism
   is there; the card
   networks each cap how many increments an authorization may take and how far
   it may grow, and none of that is modelled.
7. ~~**Period close wired into `post()`.**~~ **DONE** — `Ledger.post` now calls
   `periods.guard` itself. "Available rather than enforced" is the same shape as
   every other bug this repo has found: a rule nothing calls is a rule that is
   not in effect.

   Two decisions came with it. An unstated `effective_on` defaults to **today**,
   not to exempt — a posting with no stated date IS being made today, and
   treating the omission as exempt would make the guard optional by silence. And
   the period tables moved into `ledger/schema.sql`: a guard that queries a
   table which may not exist fails **open** on a fresh database, which is the
   state every test starts in, so having `guard` tolerate a missing table would
   have recreated exactly the problem being fixed.
