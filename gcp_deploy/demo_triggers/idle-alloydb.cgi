#!/bin/sh
# CGI handler served by busybox httpd on the Oracle VM's demo-trigger
# container. Launches REAL idle-in-transaction sessions against the live
# AlloyDB instance (same technique as simulate_idle_in_transaction_alloydb.sh,
# adapted to run from inside a container talking to the mounted docker
# socket -- no sudo needed). Requires X-Trigger-Secret to match TRIGGER_SECRET.
if [ "$HTTP_X_TRIGGER_SECRET" != "$TRIGGER_SECRET" ]; then
  echo "Status: 403 Forbidden"
  echo "Content-type: text/plain"
  echo
  echo "forbidden"
  exit 0
fi

HOST="10.120.2.2"
PORT="5432"
DB="postgres"
DBUSER="postgres"
PGIMAGE="postgres:16-alpine"
IDLE_SECONDS=900
SESSION_COUNT=4

PW=$(awk '/name: alloydb-db/,/^---$/' /var/lib/toolbox/tools.yaml | grep '^password:' | cut -d: -f2- | sed 's/^ *//;s/ *$//')

echo "Content-type: application/json"
echo
if [ -z "$PW" ]; then
  echo '{"status":"error","detail":"could not read AlloyDB password"}'
  exit 0
fi

docker image inspect "$PGIMAGE" >/dev/null 2>&1 || docker pull "$PGIMAGE" >/dev/null 2>&1

for i in $(seq 1 "$SESSION_COUNT"); do
  cat > "/tmp/trigger_idle_alloydb_$i.sql" << SQL
BEGIN;
SELECT 1;
\! sleep $IDLE_SECONDS
COMMIT;
SQL
  nohup docker run --rm -i --network host -e PGPASSWORD="$PW" "$PGIMAGE" \
    psql "host=$HOST port=$PORT dbname=$DB user=$DBUSER keepalives=1 keepalives_idle=60 keepalives_interval=15 keepalives_count=6" \
    < "/tmp/trigger_idle_alloydb_$i.sql" > "/tmp/trigger_idle_alloydb_$i.log" 2>&1 &
  sleep 0.3
done

echo "{\"status\":\"staged\",\"engine\":\"alloydb\",\"sessions\":$SESSION_COUNT,\"idle_seconds\":$IDLE_SECONDS,\"expect_detection_minutes\":\"5-6\"}"
