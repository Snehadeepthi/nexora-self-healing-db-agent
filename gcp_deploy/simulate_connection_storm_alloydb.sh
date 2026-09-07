#!/bin/bash
# simulate_connection_storm_alloydb.sh -- opens REAL plain (non-transactional)
# idle connections against the live AlloyDB instance to push connection_pct
# over config.CONNECTION_PCT_THRESHOLD, so the deployed agent's
# Sense -> Predict -> Reason -> Act loop can genuinely detect a connection
# storm and route alloydb_reset_all_connections through the real Tier 3
# human-approval gate (PENDING_APPROVAL -> operator approves -> executes).
#
# Unlike simulate_idle_in_transaction_alloydb.sh, these connections do NOT
# open a transaction (no BEGIN) -- they just connect and idle, which still
# counts toward TOTAL_CONNECTIONS/MAX_CONNECTIONS (connection_pct) without
# also tripping the separate idle_in_transaction_count signal.
#
# NOTE: AlloyDB's real max_connections is 1000, so crossing the production
# CONNECTION_PCT_THRESHOLD (0.8 = 800 connections) isn't practical to stage.
# config.CONNECTION_PCT_THRESHOLD must be temporarily lowered (e.g. to 0.08)
# for this rehearsal -- revert it back to 0.8 immediately after.
set -e
HOST="10.120.2.2"
PORT="5432"
DB="postgres"
DBUSER="postgres"
PGIMAGE="postgres:16-alpine"
HOLD_SECONDS=300     # long enough for 2 consecutive 60s detection ticks
CONNECTION_COUNT=70  # opened ON TOP of baseline connections
sudo docker image inspect "$PGIMAGE" > /dev/null 2>&1 || sudo docker pull "$PGIMAGE"
PW=$(sudo awk '/name: alloydb-db/,/^---$/' /var/lib/toolbox/tools.yaml | grep '^password:' | cut -d: -f2- | xargs)
if [ -z "$PW" ]; then
  echo "Could not extract the AlloyDB password from /var/lib/toolbox/tools.yaml -- aborting."
  exit 1
fi
echo "Using AlloyDB host: $HOST:$PORT/$DB as $DBUSER"
echo "Opening $CONNECTION_COUNT plain idle connections, held for ${HOLD_SECONDS}s"
for i in $(seq 1 "$CONNECTION_COUNT"); do
  nohup sudo docker run --rm -i --network host -e PGPASSWORD="$PW" "$PGIMAGE" \
    psql "host=$HOST port=$PORT dbname=$DB user=$DBUSER keepalives=1 keepalives_idle=60 keepalives_interval=15 keepalives_count=6" \
    > "/tmp/demo_conn_storm_alloydb_$i.log" 2>&1 <<SQL &
SELECT 1;
\! sleep $HOLD_SECONDS
SQL
  sleep 0.1
done
echo ""
echo "$CONNECTION_COUNT connections opened."
echo "connection_pct should now read well above the (temporarily lowered) threshold."
echo "To clean up early: sudo docker ps -q --filter ancestor=$PGIMAGE | xargs -r sudo docker kill"
