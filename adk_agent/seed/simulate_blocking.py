"""
seed/simulate_blocking.py
Demo helper: opens a REAL blocking-session scenario against the local
Oracle XE instance (docker-compose.yml) so a live pitch demo can trigger a
genuine Tier 1 auto-heal (kill_blocking_session) without waiting for
organic contention. This is the real-database equivalent of what the
original reference implementation's OracleSimulator faked in memory --
here it's actually real locks on a real table.

One session holds a row lock without committing (the "holder"); N more
sessions each try to lock the same row and genuinely block, which is what
populates v$session.blocking_session -- the exact signal
poll_telemetry/predict.py's AnomalyDetector act on. Each waiter releases
immediately once granted the lock, so Ctrl+C-ing the holder drains the
whole scenario cleanly within a few seconds.

Usage:
    ORACLE_PASSWORD=... python seed/simulate_blocking.py [waiter_count]
    # default waiter_count=9, clears config's blocked_sessions_threshold=8
"""
import os
import sys
import threading
import time

import oracledb

DSN = (
    f"{os.environ.get('ORACLE_HOST', 'localhost')}:"
    f"{os.environ.get('ORACLE_PORT', '1521')}/"
    f"{os.environ.get('ORACLE_SERVICE', 'XEPDB1')}"
)
USER = os.environ.get("ORACLE_USER", "executor_sa")
PASSWORD = os.environ["ORACLE_PASSWORD"]


def _ensure_demo_table(conn):
    with conn.cursor() as cur:
        try:
            cur.execute("CREATE TABLE demo_lock_target (id NUMBER PRIMARY KEY, val NUMBER)")
        except oracledb.DatabaseError:
            pass  # already exists from a previous run
        try:
            cur.execute("INSERT INTO demo_lock_target (id, val) VALUES (1, 0)")
            conn.commit()
        except oracledb.DatabaseError:
            pass  # already seeded


def _wait_then_release(idx: int):
    conn = oracledb.connect(user=USER, password=PASSWORD, dsn=DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM demo_lock_target WHERE id = 1 FOR UPDATE")  # blocks here
        conn.rollback()  # release immediately once granted -- drains cleanly, doesn't chain-block
    finally:
        conn.close()


def open_blockers(waiter_count: int):
    holder = oracledb.connect(user=USER, password=PASSWORD, dsn=DSN)
    _ensure_demo_table(holder)
    with holder.cursor() as cur:
        cur.execute("SELECT * FROM demo_lock_target WHERE id = 1 FOR UPDATE")
    print("[holder] locked demo_lock_target id=1, not committing")

    threads = [threading.Thread(target=_wait_then_release, args=(i,), daemon=True)
               for i in range(waiter_count)]
    for t in threads:
        t.start()
        time.sleep(0.15)

    print(f"Opened {waiter_count} waiting session(s) -- active_blocked_sessions should now "
          f"read >= {waiter_count}. Give the agent 2 poll ticks to confirm the anomaly "
          f"(config.CONSECUTIVE_ANOMALY_THRESHOLD).")
    print("Ctrl+C to release the holder and drain the scenario.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        holder.rollback()
        holder.close()
        print("[holder] released -- waiters will drain within a few seconds")


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 9
    open_blockers(count)
