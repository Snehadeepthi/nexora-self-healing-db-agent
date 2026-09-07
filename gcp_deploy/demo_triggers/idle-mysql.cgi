#!/bin/sh
# CGI handler served by busybox httpd on the Oracle VM's demo-trigger
# container. Launches REAL idle-in-transaction sessions against the live
# MySQL (Cloud SQL) instance (same technique as
# simulate_idle_in_transaction_mysql.sh, adapted to run from inside a
# container talking to the mounted docker socket -- no sudo needed).
# Requires X-Trigger-Secret to match TRIGGER_SECRET.
if [ "$HTTP_X_TRIGGER_SECRET" != "$TRIGGER_SECRET" ]; then
  echo "Status: 403 Forbidden"
  echo "Content-type: text/plain"
  echo
  echo "forbidden"
  exit 0
fi

HOST="10.120.0.3"
PORT="3306"
DB="selfhealing"
DBUSER="app_user"
MYSQLIMAGE="mysql:8.0"
IDLE_SECONDS=900
SESSION_COUNT=4

PW=$(awk '/name: mysql-db/,/^---$/' /var/lib/toolbox/tools.yaml | grep '^password:' | cut -d: -f2- | sed 's/^ *//;s/ *$//')

echo "Content-type: application/json"
echo
if [ -z "$PW" ]; then
  echo '{"status":"error","detail":"could not read MySQL password"}'
  exit 0
fi

docker image inspect "$MYSQLIMAGE" >/dev/null 2>&1 || docker pull "$MYSQLIMAGE" >/dev/null 2>&1

for i in $(seq 1 "$SESSION_COUNT"); do
  cat > "/tmp/trigger_idle_mysql_$i.sql" << SQL
START TRANSACTION;
SELECT 1;
\! sleep $IDLE_SECONDS
COMMIT;
SQL
  nohup docker run --rm -i --network host "$MYSQLIMAGE" \
    mysql -h "$HOST" -P "$PORT" -u "$DBUSER" -p"$PW" "$DB" \
    < "/tmp/trigger_idle_mysql_$i.sql" > "/tmp/trigger_idle_mysql_$i.log" 2>&1 &
  sleep 0.3
done

echo "{\"status\":\"staged\",\"engine\":\"mysql\",\"sessions\":$SESSION_COUNT,\"idle_seconds\":$IDLE_SECONDS,\"expect_detection_minutes\":\"5-6\"}"
