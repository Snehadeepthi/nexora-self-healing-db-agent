#!/bin/bash
# simulate_idle_in_transaction_alloydb.sh -- opens REAL idle-in-transaction
# backends against the live AlloyDB instance, so the deployed agent's
# Sense -> Predict -> Reason -> Act loop can genuinely detect and auto-heal
# it (Tier 2, alloydb_terminate_idle_in_transaction) with zero human input.
#
# Each of N sessions opens a transaction, runs one query, then genuinely
# goes idle (via psql's \! shell-out meta-command, which pauses the client
# without sending anything to the server -- unlike pg_sleep, which would
# keep the backend in 'active' state instead of 'idle in transaction') for
# long enough to cross idle_in_transaction_count_threshold's state_change
# > 300s requirement, then commits.
#
# Detection needs BOTH the 300s idle threshold AND 2 consecutive ticks
# (config.CONSECUTIVE_ANOMALY_THRESHOLD) to confirm, so this scenario
# takes ~5-6 minutes total, not instant like the blocking-session one.
set -e

HOST="10.120.2.2"
PORT="5432"
DB="postgres"
DBUSER="postgres"
PGIMAGE="postgres:16-alpine"
IDLE_SECONDS=900   # a bit over the 300s threshold, so it's still idle when ticks confirm it
SESSION_COUNT=4    # threshold is 3 (config.IDLE_IN_TRANSACTION_COUNT_THRESHOLD)

sudo docker image inspect "$PGIMAGE" > /dev/null 2>&1 || sudo docker pull "$PGIMAGE"

PW=$(sudo awk '/name: alloydb-db/,/^---$/' /var/lib/toolbox/tools.yaml | grep '^password:' | cut -d: -f2- | xargs)
if [ -z "$PW" ]; then
  echo "Could not extract the AlloyDB password from /var/lib/toolbox/tools.yaml -- aborting."
  exit 1
fi

echo "Using AlloyDB host: $HOST:$PORT/$DB as $DBUSER"
echo "Opening $SESSION_COUNT idle-in-transaction sessions, each idle for ${IDLE_SECONDS}s"

for i in $(seq 1 "$SESSION_COUNT"); do
  nohup sudo docker run --rm -i --network host -e PGPASSWORD="$PW" "$PGIMAGE" \
    psql "host=$HOST port=$PORT dbname=$DB user=$DBUSER keepalives=1 keepalives_idle=60 keepalives_interval=15 keepalives_count=6" \
    > "/tmp/demo_idle_alloydb_$i.log" 2>&1 <<SQL &
BEGIN;
SELECT 1;
\! sleep $IDLE_SECONDS
COMMIT;
SQL
  echo "[session $i] started, will idle-in-transaction for ${IDLE_SECONDS}s"
  sleep 0.3
done

echo ""
echo "$SESSION_COUNT idle-in-transaction sessions opened."
echo "idle_in_transaction_count will read >= $SESSION_COUNT once each session crosses 300s idle (threshold is 3)."
