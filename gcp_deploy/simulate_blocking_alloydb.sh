#!/bin/bash
# simulate_blocking_alloydb.sh -- opens a REAL blocking-session scenario
# against the live AlloyDB instance, so the deployed agent's Sense ->
# Predict -> Reason -> Act loop can genuinely detect and auto-heal it
# (Tier 1, alloydb_kill_blocking_session) with zero human input. Postgres
# analog of simulate_blocking_vm.sh's Oracle technique -- same real-lock
# approach, via dockerized psql since this VM's Container-Optimized OS has
# no package manager and no psql on the host.
#
# One holder session locks a row without committing; 9 waiter sessions each
# try to lock the SAME row and genuinely block -- that's what populates
# pg_stat_activity rows with wait_event_type='Lock', the exact signal
# alloydb_poll_telemetry/predict.py's AnomalyDetector act on (threshold:
# active_blocked_sessions >= 8, confirmed over 2 consecutive ticks per
# config.CONSECUTIVE_ANOMALY_THRESHOLD).
#
# The holder sleeps 180s while still holding the lock (pg_sleep inside the
# same transaction/session). That's enough time for two full ticks (Cloud
# Scheduler ticks every minute) to confirm the anomaly and for the agent to
# act -- after which the holder commits and any still-waiting sessions
# drain on their own within seconds.
set -e

HOST="10.120.2.2"
PORT="5432"
DB="postgres"
DBUSER="postgres"
PGIMAGE="postgres:16-alpine"

sudo docker image inspect "$PGIMAGE" > /dev/null 2>&1 || sudo docker pull "$PGIMAGE"

PW=$(sudo awk '/name: alloydb-db/,/^---$/' /var/lib/toolbox/tools.yaml | grep '^password:' | cut -d: -f2- | xargs)
if [ -z "$PW" ]; then
  echo "Could not extract the AlloyDB password from /var/lib/toolbox/tools.yaml -- aborting."
  exit 1
fi

echo "Using AlloyDB host: $HOST:$PORT/$DB as $DBUSER"

# --- ensure the demo lock target table exists and is seeded (idempotent) ---
sudo docker run --rm -i --network host -e PGPASSWORD="$PW" "$PGIMAGE" \
  psql -h "$HOST" -p "$PORT" -U "$DBUSER" -d "$DB" -v ON_ERROR_STOP=0 \
  > /tmp/demo_setup_alloydb.log 2>&1 <<'SQL'
CREATE TABLE IF NOT EXISTS demo_lock_target (id INTEGER PRIMARY KEY, val INTEGER);
INSERT INTO demo_lock_target (id, val) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;
SQL
echo "demo_lock_target table ready"

# --- holder: lock the row, hold it for 180s, then release ---
nohup sudo docker run --rm -i --network host -e PGPASSWORD="$PW" "$PGIMAGE" \
  psql -h "$HOST" -p "$PORT" -U "$DBUSER" -d "$DB" \
  > /tmp/demo_holder_alloydb.log 2>&1 <<'SQL' &
BEGIN;
SELECT * FROM demo_lock_target WHERE id = 1 FOR UPDATE;
SELECT pg_sleep(180);
COMMIT;
SQL
HOLDER_PID=$!
echo "[holder] started (pid $HOLDER_PID), locked id=1, holding for 180s"
sleep 3   # give the holder time to actually acquire the lock before waiters pile on

# --- 9 waiters: each blocks on the same row, then commits once granted ---
WAITER_COUNT=9
for i in $(seq 1 "$WAITER_COUNT"); do
  nohup sudo docker run --rm -i --network host -e PGPASSWORD="$PW" "$PGIMAGE" \
    psql -h "$HOST" -p "$PORT" -U "$DBUSER" -d "$DB" \
    > "/tmp/demo_waiter_alloydb_$i.log" 2>&1 <<'SQL' &
BEGIN;
SELECT * FROM demo_lock_target WHERE id = 1 FOR UPDATE;
COMMIT;
SQL
  sleep 0.3
done

echo ""
echo "Opened $WAITER_COUNT waiting session(s) against demo_lock_target id=1."
echo "active_blocked_sessions should now read >= $WAITER_COUNT (threshold is 8)."
echo ""
echo "Give the agent 2 poll ticks to confirm the anomaly (config.CONSECUTIVE_ANOMALY_THRESHOLD)"
echo "-- either wait ~2 minutes for Cloud Scheduler's automatic ticks, or run:"
echo '     curl -s -X POST "$URL/tick?db=alloydb"'
echo "  twice, a few seconds apart."
echo ""
echo "You can sanity-check the live count directly (from this VM) with:"
echo "  sudo docker run --rm --network host -e PGPASSWORD=\"\$PW\" $PGIMAGE psql -h $HOST -p $PORT -U $DBUSER -d $DB -c \"SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock';\""
echo ""
echo "The holder auto-releases after 180s on its own -- no cleanup needed unless"
echo "you want to end the scenario early (kill the holder's process: kill $HOLDER_PID)."
