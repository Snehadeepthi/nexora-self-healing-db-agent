#!/bin/bash
# simulate_blocking_vm.sh -- opens a REAL blocking-session scenario against
# the live Oracle instance on this VM, so the deployed agent's Sense -> Predict
# -> Reason -> Act loop can genuinely detect and auto-heal it (Tier 1,
# kill_blocking_session) with zero human input. Same real-lock technique as
# adk_agent/seed/simulate_blocking.py, reimplemented here via `docker exec
# sqlplus` since this VM's shell doesn't have python-oracledb wired up.
#
# One holder session locks a row without committing; 9 waiter sessions each
# try to lock the SAME row and genuinely block -- that's what populates
# v$session.blocking_session, the exact signal poll_telemetry/predict.py's
# AnomalyDetector act on (threshold: active_blocked_sessions >= 8, confirmed
# over 2 consecutive ticks per config.CONSECUTIVE_ANOMALY_THRESHOLD).
#
# The holder sleeps 180s while still holding the lock (DBMS_SESSION.SLEEP,
# not DBMS_LOCK.SLEEP -- the latter isn't EXECUTE-granted to PUBLIC by
# default and would need an extra grant; DBMS_SESSION.SLEEP is, since 18c).
# That's enough time for two full ticks (Cloud Scheduler ticks every minute)
# to confirm the anomaly and for the agent to act -- after which the holder
# commits and any still-waiting sessions drain on their own within seconds.
set -e

PW=$(sudo grep -m1 "^password:" /var/lib/toolbox/tools.yaml | cut -d: -f2- | xargs)
ORACLE_CONTAINER=$(sudo docker ps --filter "name=oracle-db" --format "{{.Names}}" | head -n1)
if [ -z "$ORACLE_CONTAINER" ]; then
  echo "Could not find the running oracle-xe container -- is the VM fully booted?"
  exit 1
fi
DSN="executor_sa/${PW}@127.0.0.1:1521/FREEPDB1"

echo "Using Oracle container: $ORACLE_CONTAINER"

# --- ensure the demo lock target table exists and is seeded (idempotent) ---
sudo docker exec -i "$ORACLE_CONTAINER" sqlplus -s "$DSN" > /tmp/demo_setup.log 2>&1 << 'SQL'
WHENEVER SQLERROR CONTINUE
CREATE TABLE demo_lock_target (id NUMBER PRIMARY KEY, val NUMBER);
INSERT INTO demo_lock_target (id, val) VALUES (1, 0);
COMMIT;
EXIT;
SQL
echo "demo_lock_target table ready"

# --- holder: lock the row, hold it for 180s, then release ---
nohup sudo docker exec -i "$ORACLE_CONTAINER" sqlplus -s "$DSN" > /tmp/demo_holder.log 2>&1 << 'SQL' &
SELECT * FROM demo_lock_target WHERE id = 1 FOR UPDATE;
EXEC DBMS_SESSION.SLEEP(180);
COMMIT;
EXIT;
SQL
HOLDER_PID=$!
echo "[holder] started (pid $HOLDER_PID), locked id=1, holding for 180s"

sleep 3   # give the holder time to actually acquire the lock before waiters pile on

# --- 9 waiters: each blocks on the same row, then commits once granted ---
WAITER_COUNT=9
for i in $(seq 1 "$WAITER_COUNT"); do
  nohup sudo docker exec -i "$ORACLE_CONTAINER" sqlplus -s "$DSN" > "/tmp/demo_waiter_$i.log" 2>&1 << 'SQL' &
SELECT * FROM demo_lock_target WHERE id = 1 FOR UPDATE;
COMMIT;
EXIT;
SQL
  sleep 0.3
done

echo ""
echo "Opened $WAITER_COUNT waiting session(s) against demo_lock_target id=1."
echo "active_blocked_sessions should now read >= $WAITER_COUNT (threshold is 8)."
echo ""
echo "Give the agent 2 poll ticks to confirm the anomaly (config.CONSECUTIVE_ANOMALY_THRESHOLD)"
echo "-- either wait ~2 minutes for Cloud Scheduler's automatic ticks, or run:"
echo "     curl -s -X POST \"\$URL/tick\""
echo "  twice, a few seconds apart."
echo ""
echo "You can sanity-check the live count directly with:"
echo "  docker exec -i \"$ORACLE_CONTAINER\" sqlplus -s \"$DSN\" <<< \"SELECT COUNT(*) FROM v\\\$session WHERE blocking_session IS NOT NULL; EXIT;\""
echo ""
echo "The holder auto-releases after 180s on its own -- no cleanup needed unless"
echo "you want to end the scenario early (kill the holder's docker exec process)."