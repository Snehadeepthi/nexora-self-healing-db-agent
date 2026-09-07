#!/bin/bash
# simulate_blocking_mysql.sh
#
# MySQL analog of simulate_blocking_alloydb.sh -- one holder + N waiters
# contending for the same row lock via InnoDB's SELECT ... FOR UPDATE.
# Uses MySQL's own SELECT SLEEP(seconds) to hold the transaction open
# instead of a shell-level sleep -- the connection stays actively busy
# running a query rather than going idle, avoiding the TCP-keepalive
# drop this project hit with AlloyDB's idle-in-transaction test (see
# session notes: psql's \! sleep leaves the client blocked and the
# underlying TCP connection can silently drop during a long silent
# period). Run via: bash simulate_blocking_mysql.sh
set -e
MYSQL_HOST="10.120.0.3"
MYSQL_PORT="3306"
MYSQL_DB="selfhealing"
MYSQL_USER="app_user"
HOLD_SECONDS="${1:-90}"
WAITERS="${2:-3}"

PW=$(sudo cat /var/lib/toolbox/tools.yaml | awk '/name: mysql-db/,/^---$/' | grep '^password:' | awk '{print $2}')
if [ -z "$PW" ]; then
  echo "Could not extract MySQL password from live tools.yaml -- aborting."
  exit 1
fi

echo "Starting holder (holds row id=1 for ${HOLD_SECONDS}s)..."
nohup sudo docker run --rm -e MYSQL_PWD="$PW" mysql:8 \
  mysql -h "$MYSQL_HOST" -P "$MYSQL_PORT" -u "$MYSQL_USER" -D "$MYSQL_DB" \
  -e "START TRANSACTION; SELECT * FROM lock_test WHERE id=1 FOR UPDATE; SELECT SLEEP(${HOLD_SECONDS}); COMMIT;" \
  > /tmp/demo_mysql_holder.log 2>&1 &
echo "Holder PID: $!"

sleep 2

for i in $(seq 1 "$WAITERS"); do
  echo "Starting waiter $i..."
  nohup sudo docker run --rm -e MYSQL_PWD="$PW" mysql:8 \
    mysql -h "$MYSQL_HOST" -P "$MYSQL_PORT" -u "$MYSQL_USER" -D "$MYSQL_DB" \
    -e "SET SESSION innodb_lock_wait_timeout = 300; START TRANSACTION; SELECT * FROM lock_test WHERE id=1 FOR UPDATE; SELECT SLEEP(1); COMMIT;" \
    > "/tmp/demo_mysql_waiter_$i.log" 2>&1 &
  echo "Waiter $i PID: $!"
done

echo "Holder + ${WAITERS} waiters launched. Holder releases automatically after ${HOLD_SECONDS}s if nothing kills it first."
