"""Does dropping the cached balance actually fix the contention?

    python pg_hotrow_test.py --txns 600 --workers 16

`pg_drift_test.py` found an 88.7% serialization-failure rate on one hot account
and blamed the trigger-maintained `account_balance` row. The README then
asserted the fix -- aggregate the journal on read instead -- **with no experiment
behind it.** This is the experiment.

Same workload, same isolation, same retry loop, same hot account. The only
difference is whether a materialized balance row exists.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ledger.pg_nohotrow import DerivedLedger
from ledger.pg_store import DSN, PgLedger, RetryStats, SerializationExhausted

CASH = "hr:cash"
REV = "hr:revenue"


def _entries(amount, prefix=""):
    return [{"account_id": prefix + CASH, "direction": "D",
             "amount_minor": amount, "currency": "USD"},
            {"account_id": prefix + REV, "direction": "C",
             "amount_minor": amount, "currency": "USD"}]


def _drive(make_ledger, n, workers, label):
    stats = RetryStats()
    errors, lock = [], threading.Lock()
    per = max(1, n // workers)

    def work(w):
        led = make_ledger(stats)
        for i in range(per):
            try:
                led.post(_entries(100 + i), "hot", label,
                         "req-{}-{}".format(w, i))
            except SerializationExhausted:
                with lock:
                    errors.append("exhausted")
            except Exception as exc:                          # noqa: BLE001
                with lock:
                    errors.append("{}: {}".format(type(exc).__name__, exc)[:80])
        closer = getattr(led, "close", None) or getattr(led.base, "close", None)
        if closer:
            closer()

    threads = [threading.Thread(target=work, args=(w,)) for w in range(workers)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return stats, time.perf_counter() - t0, errors


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--txns", type=int, default=600)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--dsn", default=DSN)
    args = ap.parse_args()

    print("=" * 82)
    print("THE HOT-ROW HYPOTHESIS, TESTED")
    print("=" * 82)
    print("Claim under test: the trigger-maintained balance row is what makes")
    print("SERIALIZABLE fail on a hot account, and appending only would not.")
    print()
    print("{} postings, {} workers, one hot account, both designs.".format(
        args.txns, args.workers))

    # ------------------------------------------------------------ cached
    base = PgLedger(args.dsn)
    base.install_schema()
    base.open_account(REV, "revenue", "USD", floor_minor=-10**15,
                      overdraft_allowed=True)
    base.open_account(CASH, "asset", "USD", floor_minor=-10**15,
                      overdraft_allowed=True)
    base.close()

    cached_stats, cached_el, cached_err = _drive(
        lambda st: PgLedger(args.dsn, stats=st), args.txns, args.workers,
        "cached")

    # ----------------------------------------------------------- derived
    d_base = PgLedger(args.dsn)
    derived = DerivedLedger(d_base)
    derived.install_schema()
    derived.open_account(REV, "revenue", "USD", floor_minor=-10**15,
                         overdraft_allowed=True)
    derived.open_account(CASH, "asset", "USD", floor_minor=-10**15,
                         overdraft_allowed=True)
    d_base.close()

    derived_stats, derived_el, derived_err = _drive(
        lambda st: DerivedLedger(PgLedger(args.dsn, stats=st)),
        args.txns, args.workers, "derived")

    # ------------------------------------------------------------ report
    print("\n" + "=" * 82)
    print("RESULT")
    print("-" * 82)
    print("{:<28}{:>24}{:>24}".format("", "CACHED balance row", "DERIVED on read"))
    rows = [
        ("committed", cached_stats.committed, derived_stats.committed),
        ("serialization failures", cached_stats.serialization_failures,
         derived_stats.serialization_failures),
        ("retries exhausted", cached_stats.exhausted, derived_stats.exhausted),
        ("errors", len(cached_err), len(derived_err)),
    ]
    for name, a, b in rows:
        print("{:<28}{:>24,}{:>24,}".format(name, a, b))
    print("{:<28}{:>23.1%}{:>24.1%}".format(
        "retry rate", cached_stats.retry_rate, derived_stats.retry_rate))
    print("{:<28}{:>24,.0f}{:>24,.0f}".format(
        "txn/s",
        cached_stats.committed / cached_el if cached_el else 0,
        derived_stats.committed / derived_el if derived_el else 0))

    print()
    cr, dr = cached_stats.retry_rate, derived_stats.retry_rate
    if dr < cr / 2:
        print("HYPOTHESIS SUPPORTED, WITH A QUALIFICATION THAT MATTERS.")
        print()
        print("Removing the materialized balance took the retry rate from {:.1%}".format(cr))
        print("to {:.1%}, throughput from {:,.0f} to {:,.0f} txn/s, and the".format(
            dr, cached_stats.committed / cached_el if cached_el else 0,
            derived_stats.committed / derived_el if derived_el else 0))
        print("transactions lost outright from {} to {}.".format(
            cached_stats.exhausted, derived_stats.exhausted))
        print()
        print("The qualification: {:.1%} is not zero. Appends to different rows".format(dr))
        print("do not conflict, but the postings still share a transaction table,")
        print("a sequence and an index, and SERIALIZABLE still finds dependencies")
        print("there. So the balance row was the DOMINANT cause rather than the")
        print("only one -- which is a weaker claim than the README made, and the")
        print("one the measurement actually supports.")
    elif dr < cr:
        print("PARTIALLY SUPPORTED. The retry rate fell from {:.1%} to {:.1%},".format(cr, dr))
        print("so the balance row is A cause and not the only one -- the")
        print("transaction rows and the shared index still create contention.")
    else:
        print("HYPOTHESIS NOT SUPPORTED. The retry rate did not fall ({:.1%} vs".format(cr))
        print("{:.1%}), so the cached balance was not the bottleneck and the".format(dr))
        print("diagnosis in the README was wrong. That is the point of running")
        print("the experiment instead of publishing the reasoning.")

    # ------------------------------------------------------- what it costs
    print("\n" + "=" * 82)
    print("WHAT THE DERIVED DESIGN COSTS")
    print("-" * 82)
    # Measure on a WARM connection, repeated. The first attempt opened a fresh
    # connection per read and reported the cached read as SLOWER than the
    # derived one -- which is impossible when one scans a single row and the
    # other scans hundreds. It was timing SCRAM handshakes, not queries.
    entries = derived.entry_count(CASH)
    conn = derived.base._psycopg.connect(args.dsn, autocommit=True)
    from ledger.pg_nohotrow import DERIVED_BALANCE_SQL

    def _timed(sql, params, n=25):
        conn.execute(sql, params).fetchone()          # warm the plan
        t0 = time.perf_counter()
        for _ in range(n):
            out = conn.execute(sql, params).fetchone()[0]
        return (time.perf_counter() - t0) * 1000 / n, out

    cached_read_ms, cached_bal = _timed(
        "SELECT balance_minor FROM account_balance WHERE account_id = %s", (CASH,))
    derived_read_ms, bal = _timed(DERIVED_BALANCE_SQL, (CASH,))
    conn.close()

    print("{:<34}{:>16}{:>18}".format("", "cached", "derived"))
    print("{:<34}{:>16,}{:>18,}".format("balance", cached_bal, bal))
    print("{:<34}{:>15.2f}ms{:>16.2f}ms".format(
        "balance read", cached_read_ms, derived_read_ms))
    print("{:<34}{:>16}{:>18,}".format("rows scanned per read", "1", entries))
    print()
    print("That is the trade, and it is not free. The derived read scans every")
    print("entry for the account, so its cost grows with history: at a million")
    print("entries the balance query scans a million rows, and it is a query")
    print("every authorization makes.")
    print()
    print("The production answer is neither design on its own -- it is periodic")
    print("SNAPSHOTS: balance at a checkpoint plus the entries since. This repo")
    print("already has `balance_snapshot` and `balance_as_of_snapshotted` for")
    print("exactly that shape on the SQLite side. Porting it is the real work,")
    print("and this experiment is what says it is worth doing.")
    print("=" * 82)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
