#!/bin/sh
# lock-cascade-mysql.cgi
# CGI handler served by busybox httpd on the Oracle VM's demo-trigger
# container. Stages a REAL lock cascade against the live MySQL instance:
# one holder takes a row lock via SELECT ... FOR UPDATE and holds it via
# MySQL's own SELECT SLEEP() (keeps the connection actively busy rather
# than idle -- same reasoning as simulate_blocking_mysql.sh). WAITERS
# sessions then queue up behind the same lock. active_blocked_sessions has
# no idle-time floor, so 10 waiters comfortably clears the real
# blocked_sessions_threshold=8 with margin. Requires X-Trigger-Secret to
# match TRIGGER_SECRET.
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
HOLD_SECONDS=180
WAITERS=10
PW=$(awk '/name: mysql-db/,/^---$/' /var/lib/toolbox/tools.yaml | grep '^password:' | cut -d: -f2- | sed 's/^ *//;s/ *$//')
echo "Content-type: application/json"
echo
if [ -z "$PW" ]; then
  echo '{"status":"error","detail":"could not read MySQL password"}'
  exit 0
fi
docker image inspect "$MYSQLIMAGE" >/dev/null 2>&1 || docker pull "$MYSQLIMAGE" >/dev/null 2>&1
nohup docker run --rm -e MYSQL_PWD="$PW" --network host "$MYSQLIMAGE" \
  mysql -h "$HOST" -P "$PORT" -u "$DBUSER" -D "$DB" \
  -e "START TRANSACTION; SELECT * FROM lock_test WHERE id=1 FOR UPDATE; SELECT SLEEP(${HOLD_SECONDS}); COMMIT;" \
  > /tmp/demo_mysql_lockholder.log 2>&1 &
sleep 2
i=1
while [ "$i" -le "$WAITERS" ]; do
  nohup docker run --rm -e MYSQL_PWD="$PW" --network host "$MYSQLIMAGE" \
    mysql -h "$HOST" -P "$PORT" -u "$DBUSER" -D "$DB" \
    -e "SET SESSION innodb_lock_wait_timeout = 300; START TRANSACTION; SELECT * FROM lock_test WHERE id=1 FOR UPDATE; SELECT SLEEP(1); COMMIT;" \
    > "/tmp/demo_mysql_lockwaiter_$i.log" 2>&1 &
  sleep 0.3
  i=$((i + 1))
done
echo "{\"status\":\"staged\",\"engine\":\"mysql\",\"scenario\":\"lock_cascade\",\"waiters\":$WAITERS,\"hold_seconds\":$HOLD_SECONDS,\"expect_detection_minutes\":\"1-2\"}"
