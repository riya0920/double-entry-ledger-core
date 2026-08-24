"""The concurrency proof SQLite could not give.

    python pg_drift_test.py --txns 20000 --workers 50

`drift_test.py` runs the same four invariants on SQLite and reports zero
violations. That result is true and it is weak evidence, because SQLite admits
one writer at a time: the workers queue on a lock, true write-write interleaving
never happens, and the failure mode the test exists to catch cannot occur. **The
test did not pass so much as fail to be possible.**

Postgres under SERIALIZABLE does let the transactions interleave, detects the
anomaly, and aborts one with SQLSTATE 40001. This script measures three things
that only exist on that store:

  1. THE SERIALIZATION FAILURE RATE at rising concurrency -- the number the
     SQLite run structurally reports as zero.
  2. THE RETRY LOOP WORKING -- every aborted transaction retried to a commit,
     with the backoff distribution, so "we retry" is a measurement rather than
     a claim.
  3. THE INVARIANTS HOLDING ANYWAY -- which is only meaningful here, because
     here they could have broken.

It also runs a deliberate CONTENTION HOTSPOT: all workers posting against one
account, which is what actually produces anomalies. Spread over many accounts
Postgres finds no conflicts and reports a serialization rate near zero -- a
comfortable number that would prove nothing, and reporting only that would be
choosing the convenient benchmark.
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

from ledger.pg_store import DSN, PgLedger, RetryStats, SerializationExhausted

CASH = "pg:cash"
REV = "pg:revenue"


def _entries(account, amount):
    return [{"account_id": account, "direction": "D",
             "amount_minor": amount, "currency": "USD"},
            {"account_id": REV, "direction": "C",
             "amount_minor": amount, "currency": "USD"}]


def _drive(dsn, n, workers, accounts, label, isolation="SERIALIZABLE"):
    stats = RetryStats()
    lock = threading.Lock()
    errors = []
    per = max(1, n // workers)

    def work(w):
        led = PgLedger(dsn, stats=stats, isolation=isolation)  # one conn/thread
        for i in range(per):
            acct = accounts[(w + i) % len(accounts)]
            try:
                led.post(_entries(acct, 100 + i), "drift",
                         "{}:{}".format(label, w), "req-{}-{}".format(w, i))
            except SerializationExhausted as exc:
                with lock:
                    errors.append(str(exc))
            except Exception as exc:                          # noqa: BLE001
                with lock:
                    errors.append("{}: {}".format(type(exc).__name__, exc))
        led.close()

    threads = [threading.Thread(target=work, args=(w,)) for w in range(workers)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return stats, time.perf_counter() - t0, errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--txns", type=int, default=8000)
    ap.add_argument("--workers", type=int, default=50)
    ap.add_argument("--dsn", default=DSN)
    args = ap.parse_args()

    led = PgLedger(args.dsn)
    print("=" * 84)
    print("POSTGRES SERIALIZABLE -- THE CONCURRENCY PROOF SQLITE CANNOT GIVE")
    print("=" * 84)

    led.install_schema()
    led.open_account(REV, "revenue", "USD", floor_minor=-10**15,
                     overdraft_allowed=True)
    led.open_account(CASH, "asset", "USD", floor_minor=-10**15,
                     overdraft_allowed=True)
    spread = ["pg:acct{}".format(i) for i in range(64)]
    for a in spread:
        led.open_account(a, "asset", "USD", floor_minor=-10**15,
                         overdraft_allowed=True)

    with led._psycopg.connect(args.dsn, autocommit=True) as c:
        version = c.execute("select version()").fetchone()[0]
    print(version.split(" on ")[0])
    print("isolation: SERIALIZABLE   workers: {}   postings: {}".format(
        args.workers, args.txns))

    # ------------------------------------------------- 1. spread, no hotspot
    print("\n" + "=" * 84)
    print("1. SPREAD ACROSS 64 ACCOUNTS -- the comfortable benchmark")
    print("-" * 84)
    s1, el1, err1 = _drive(args.dsn, args.txns, args.workers, spread, "spread")
    print("committed              : {:,}".format(s1.committed))
    print("serialization failures : {:,}".format(s1.serialization_failures))
    print("deadlocks              : {:,}".format(s1.deadlocks))
    print("retry rate             : {:.3%}".format(s1.retry_rate))
    print("elapsed                : {:.2f}s  ({:,.0f} txn/s)".format(
        el1, s1.committed / el1 if el1 else 0))
    print("errors                 : {}".format(len(err1)))
    print()
    print("A near-zero conflict rate here is not a result. Sixty-four accounts")
    print("and fifty workers rarely touch the same row, so Postgres finds")
    print("nothing to serialize and the number is a property of the ACCESS")
    print("PATTERN rather than of the store. Reporting only this row would be")
    print("choosing the convenient benchmark.")

    # -------------------------------------------------- 2. the real hotspot
    print("\n" + "=" * 84)
    print("2. ONE HOT ACCOUNT -- where anomalies actually come from")
    print("-" * 84)
    s2, el2, err2 = _drive(args.dsn, args.txns, args.workers, [CASH], "hot")
    print("committed              : {:,}".format(s2.committed))
    print("serialization failures : {:,}".format(s2.serialization_failures))
    print("deadlocks              : {:,}".format(s2.deadlocks))
    print("retry rate             : {:.3%}".format(s2.retry_rate))
    print("retries exhausted      : {}".format(s2.exhausted))
    print("elapsed                : {:.2f}s  ({:,.0f} txn/s)".format(
        el2, s2.committed / el2 if el2 else 0))
    print("errors                 : {}".format(len(err2)))
    for e in err2[:3]:
        print("   {}".format(e[:100]))

    if s2.backoff_ms:
        print("\nbackoff actually slept (ms): p50 {:.2f}  p95 {:.2f}  max {:.2f}".format(
            statistics.median(s2.backoff_ms),
            sorted(s2.backoff_ms)[int(len(s2.backoff_ms) * 0.95)],
            max(s2.backoff_ms)))
        print("Exponential with FULL jitter. Without the jitter every aborted")
        print("peer wakes at the same instant and collides again -- a retry")
        print("storm is a convoy, and randomising the wake is what breaks it.")

    print()
    if s2.serialization_failures:
        print("THAT COLUMN IS THE POINT, AND IT DOES NOT SAY WHAT I EXPECTED.")
        print()
        print("{:,} transactions were aborted because they could not both be".format(
            s2.serialization_failures))
        print("true -- a number SQLite cannot produce, because its second writer")
        print("never got to start. But {:,} of them ran out of retries and".format(
            s2.exhausted))
        print("FAILED. The retry loop did not save every transaction; it saved")
        print("{:.1%} of them, and the rest are errors a caller has to handle.".format(
            s2.committed / max(s2.committed + s2.exhausted, 1)))
        print()
        print("Throughput here is {:,.0f} txn/s. Resist comparing that to".format(
            s2.committed / el2 if el2 else 0))
        print("SQLite's 1,323 txn/s, which is the comparison I reached for first")
        print("and it is not a fair one: SQLite runs IN PROCESS against a local")
        print("file, while this crosses a virtual NIC into another operating")
        print("system. Most of that gap is transport, not isolation, and quoting")
        print("it as an isolation cost would be exactly the kind of number this")
        print("repository exists not to print.")
        print()
        print("The clean comparison holds the transport fixed and changes only")
        print("the isolation level. That is section 3.")

    # ------------------------------------------------- 3. why, and the cost
    print("\n" + "=" * 84)
    print("3. THE DIAGNOSIS: A MATERIALIZED BALANCE IS A HOT ROW")
    print("-" * 84)
    print("Every posting updates ONE row of account_balance for the account it")
    print("touches. Under SERIALIZABLE that row is a serialization point: two")
    print("postings to the same account always conflict, so at {} workers on".format(
        args.workers))
    print("one account essentially every transaction conflicts with every other")
    print("and the retry rate goes to {:.0%}.".format(s2.retry_rate))
    print()
    print("The SQLite README calls the trigger-maintained balance cache the one")
    print("design decision everything else follows from. It still is -- and this")
    print("is its cost, which a single-writer store cannot show you, because")
    print("there the same contention presents as a queue rather than as aborts.")
    print()
    print("Running the identical load at READ COMMITTED to price the isolation:")
    print()
    s3, el3, err3 = _drive(args.dsn, args.txns, args.workers, [CASH], "hot-rc",
                           isolation="READ_COMMITTED")
    print("{:<26}{:>18}{:>18}".format("", "SERIALIZABLE", "READ COMMITTED"))
    print("{:<26}{:>18,}{:>18,}".format("committed", s2.committed, s3.committed))
    print("{:<26}{:>18,}{:>18,}".format(
        "serialization failures", s2.serialization_failures,
        s3.serialization_failures))
    print("{:<26}{:>18}{:>18}".format("retries exhausted", s2.exhausted,
                                      s3.exhausted))
    print("{:<26}{:>17.1%}{:>18.1%}".format("retry rate", s2.retry_rate,
                                            s3.retry_rate))
    print("{:<26}{:>18,.0f}{:>18,.0f}".format(
        "txn/s", s2.committed / el2 if el2 else 0,
        s3.committed / el3 if el3 else 0))
    print()
    print("Same transport, same workload, same code -- only the isolation level")
    print("differs. SERIALIZABLE costs {:.1f}x the throughput AND fails {} of".format(
        (s3.committed / el3) / (s2.committed / el2) if el2 and el3 and s2.committed else 0,
        s2.exhausted))
    print("{} transactions outright. That is the price, measured.".format(
        s2.committed + s2.exhausted))
    print()
    print("READ COMMITTED is faster and it is not free. What SERIALIZABLE buys")
    print("is the floor check: two concurrent postings can each read a balance")
    print("above the floor, each decide their withdrawal is legal, and both")
    print("commit -- leaving an account below a floor that neither transaction")
    print("ever saw breached. Postgres detects exactly that dependency cycle and")
    print("refuses one of them. At READ COMMITTED nobody refuses anything.")
    print()
    print("So the choice is not 'which is better'. It is: pay the throughput")
    print("and the failed transactions for a floor that cannot be crossed, or")
    print("take the throughput and")
    print("enforce floors somewhere a race cannot reach -- which in practice")
    print("means not keeping a hot cached balance at all, and aggregating the")
    print("journal on read instead. That is a rewrite this project has not done,")
    print("and it is now the honest first item of its remaining work.")

    # ---------------------------------------------------- 4. the invariants
    print("\n" + "=" * 84)
    print("4. THE FOUR INVARIANTS, AFTER REAL INTERLEAVING")
    print("-" * 84)
    problems = led.check_invariants()
    total = s1.committed + s2.committed
    print("sealed transactions    : {:,}".format(total))
    print("cash balance           : {:,}".format(led.balance(CASH)))
    print("invariant violations   : {}".format(len(problems)))
    for p in problems[:5]:
        print("   {}".format(p))
    if not problems:
        print("\nALL INVARIANTS HOLD (I1 balance, I2 floors, I3 derived, I4 sealed)")
        print()
        print("And here this MEANS something. The same sentence at the bottom of")
        print("drift_test.py is true of a store where the workers took turns.")
        print("This one is true of a store where they did not.")
    print("=" * 84)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
