#!/bin/bash
# simulate_connection_storm_mysql.sh -- opens REAL plain (non-transactional)
# idle connections against the live MySQL instance to push connection_pct
# over config.CONNECTION_PCT_THRESHOLD, so the deployed agent's
# Sense -> Predict -> Reason -> Act loop can genuinely detect a connection
# storm and route mysql_reset_all_connections through the real Tier 3
# human-approval gate.
#
# NOTE: MySQL's real max_connections here is 4030, and CONNECTION_PCT_THRESHOLD
# is a single value SHARED across all engines (config.py has no per-engine
# variant) -- so temporarily lowering it for this rehearsal will also make
# AlloyDB's OWN baseline connection_pct (~4%) cross the same threshold,
# likely producing an extra, unplanned AlloyDB Tier 3 PENDING_APPROVAL
# alongside the MySQL one. This is harmless (Tier 3 always requires human
# approval, and AlloyDB's reset-all-connections action is already fully
# proven) -- just don't be surprised by it.
set -e
HOST="10.120.0.3"
PORT="3306"
DB="selfhealing"
DBUSER="app_user"
MYSQLIMAGE="mysql:8.0"
HOLD_SECONDS=300
CONNECTION_COUNT=60
sudo docker image inspect "$MYSQLIMAGE" > /dev/null 2>&1 || sudo docker pull "$MYSQLIMAGE"
PW=$(sudo awk '/name: mysql-db/,/^---$/' /var/lib/toolbox/tools.yaml | grep '^password:' | cut -d: -f2- | xargs)
if [ -z "$PW" ]; then
  echo "Could not extract the MySQL password from /var/lib/toolbox/tools.yaml -- aborting."
  exit 1
fi
echo "Using MySQL host: $HOST:$PORT/$DB as $DBUSER"
echo "Opening $CONNECTION_COUNT plain idle connections, held for ${HOLD_SECONDS}s"
for i in $(seq 1 "$CONNECTION_COUNT"); do
  nohup sudo docker run --rm -i --network host "$MYSQLIMAGE" \
    mysql -h "$HOST" -P "$PORT" -u "$DBUSER" -p"$PW" "$DB" \
    > "/tmp/demo_conn_storm_mysql_$i.log" 2>&1 <<SQL &
SELECT 1;
\! sleep $HOLD_SECONDS
SQL
  sleep 0.1
done
echo ""
echo "$CONNECTION_COUNT connections opened."
echo "connection_pct should now read well above the (temporarily lowered) threshold."
echo "To clean up early: sudo docker ps -q --filter ancestor=$MYSQLIMAGE | xargs -r sudo docker kill"
