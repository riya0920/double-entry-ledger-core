"""The drift test: hammer hot accounts from many workers, then check the four
invariants against the journal.

Honest scope note (read this before quoting any number from it):
  * The store here is SQLite in WAL mode. SQLite admits exactly one writer at a
    time, so this harness proves *correctness under contention and retry*, not
    parallel write throughput. The Postgres SERIALIZABLE variant -- where the
    interesting failure (serialization anomaly -> 40001 -> retry loop) actually
    happens -- is the remaining work; see README.
  * Throughput below is therefore a floor for correctness testing, not a
    benchmark of the design.

Usage:  python drift_test.py --txns 20000 --workers 16
"""
from __future__ import annotations

import argparse
import random
import threading
import time
from pathlib import Path

from ledger import invariants
from ledger.core import Ledger, credit, debit

BIG = -10**15


def build(path: Path, n_accounts: int) -> Ledger:
    if path.exists():
        for p in [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]:
            p.unlink(missing_ok=True)
    lg = Ledger(path)
    lg.open_account("funding", "asset", "USD", floor_minor=BIG, overdraft_allowed=True)
    for i in range(n_accounts):
        # Floors at 0 with no overdraft: invariant I2 has teeth here.
        lg.open_account("acct:{:03d}".format(i), "liability", "USD",
                        floor_minor=BIG, overdraft_allowed=True)
    return lg


def worker(lg: Ledger, wid: int, n: int, accounts: list[str], stats: dict,
           lock: threading.Lock) -> None:
    rng = random.Random(1000 + wid)
    ok = retried = replays = conflicts = 0
    prev = None
    for i in range(n):
        a, b = rng.sample(accounts, 2)
        amt = rng.randint(1, 5_000)
        key = "w{}-{}".format(wid, i)
        payload = {"a": a, "b": b, "amt": amt}
        roll = rng.random()
        if prev and roll < 0.05:
            # honest client retry: same key, same payload -> must replay
            key, payload = prev
        elif prev and roll < 0.07:
            # buggy/hostile client: same key, different payload -> must 409
            key = prev[0]
        entries = [debit(payload["a"], payload["amt"], "USD"),
                   credit(payload["b"], payload["amt"], "USD")]
        for attempt in range(5):
            try:
                _body, _status, replayed = lg.post_idempotent(
                    key, payload, entries, actor="worker-{}".format(wid),
                    reason="transfer", request_id=key)
                ok += 1
                replays += int(replayed)
                break
            except Exception as exc:
                msg = str(exc)
                if "locked" in msg or "busy" in msg:
                    retried += 1
                    time.sleep(0.002 * (attempt + 1))
                    continue
                if "different payload" in msg:
                    conflicts += 1
                    break
                raise
        prev = (key, payload)
    with lock:
        stats["ok"] += ok
        stats["retried"] += retried
        stats["replayed"] += replays
        stats["conflicts"] += conflicts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--txns", type=int, default=20_000)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--accounts", type=int, default=25)
    ap.add_argument("--db", default="drift.db")
    args = ap.parse_args()

    lg = build(Path(args.db), args.accounts)
    accounts = ["acct:{:03d}".format(i) for i in range(args.accounts)]
    per = args.txns // args.workers
    stats = {"ok": 0, "retried": 0, "replayed": 0, "conflicts": 0}
    lock = threading.Lock()

    threads = [threading.Thread(target=worker,
                                args=(lg, w, per, accounts, stats, lock))
               for w in range(args.workers)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0

    con = lg._conn()
    sealed = con.execute("SELECT COUNT(*) c FROM journal_txn WHERE sealed=1").fetchone()["c"]
    entries = con.execute("SELECT COUNT(*) c FROM journal_entry").fetchone()["c"]
    keys = con.execute("SELECT COUNT(*) c FROM idempotency_key").fetchone()["c"]

    print("=" * 68)
    print("DRIFT TEST  workers={}  requested={}  accounts={}".format(
        args.workers, per * args.workers, args.accounts))
    print("-" * 68)
    print("requests ok          : {}".format(stats["ok"]))
    print("lock retries         : {}".format(stats["retried"]))
    print("idempotent replays   : {}  (same key+payload -> original result)".format(
        stats["replayed"]))
    print("409 conflicts        : {}  (same key, different payload)".format(
        stats["conflicts"]))
    print("sealed transactions  : {}".format(sealed))
    print("journal entries      : {}".format(entries))
    print("idempotency keys     : {}  (must equal sealed txns)".format(keys))
    print("elapsed              : {:.2f}s  ({:.0f} txn/s, single-writer store)".format(
        elapsed, sealed / elapsed if elapsed else 0))
    print("-" * 68)
    print(invariants.report(con))
    print("=" * 68)
    return 0 if not invariants.check_all(con) and keys == sealed else 1


if __name__ == "__main__":
    raise SystemExit(main())
