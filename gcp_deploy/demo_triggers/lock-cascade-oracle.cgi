#!/bin/sh
# lock-cascade-oracle.cgi
# CGI handler served by busybox httpd on the Oracle VM's demo-trigger
# container. Stages a REAL blocking-session scenario against the Oracle
# instance running on THIS SAME VM -- unlike AlloyDB/MySQL, talks to the
# oracle-db container directly via the mounted docker socket, no
# --network host needed. Same mechanism as simulate_blocking_vm.sh: one
# holder locks a row via SELECT ... FOR UPDATE and holds it with
# DBMS_SESSION.SLEEP(180), 9 waiters queue up behind it. Requires
# X-Trigger-Secret to match TRIGGER_SECRET.
if [ "$HTTP_X_TRIGGER_SECRET" != "$TRIGGER_SECRET" ]; then
  echo "Status: 403 Forbidden"
  echo "Content-type: text/plain"
  echo
  echo "forbidden"
  exit 0
fi
HOLD_SECONDS=180
WAITERS=9
PW=$(awk '/name: oracle-db/,/^---$/' /var/lib/toolbox/tools.yaml | grep '^password:' | cut -d: -f2- | sed 's/^ *//;s/ *$//')
echo "Content-type: application/json"
echo
if [ -z "$PW" ]; then
  echo '{"status":"error","detail":"could not read Oracle password"}'
  exit 0
fi
ORACLE_CONTAINER=$(docker ps --filter "name=oracle-db" --format "{{.Names}}" | head -n1)
if [ -z "$ORACLE_CONTAINER" ]; then
  echo '{"status":"error","detail":"could not find running oracle-db container"}'
  exit 0
fi
DSN="executor_sa/${PW}@127.0.0.1:1521/FREEPDB1"
docker exec -i "$ORACLE_CONTAINER" sqlplus -s "$DSN" > /tmp/demo_oracle_setup.log 2>&1 << 'SQL'
WHENEVER SQLERROR CONTINUE
CREATE TABLE demo_lock_target (id NUMBER PRIMARY KEY, val NUMBER);
INSERT INTO demo_lock_target (id, val) VALUES (1, 0);
COMMIT;
EXIT;
SQL
nohup docker exec -i "$ORACLE_CONTAINER" sqlplus -s "$DSN" > /tmp/demo_oracle_holder.log 2>&1 << 'SQL' &
SELECT * FROM demo_lock_target WHERE id = 1 FOR UPDATE;
EXEC DBMS_SESSION.SLEEP(180);
COMMIT;
EXIT;
SQL
sleep 3
i=1
while [ "$i" -le "$WAITERS" ]; do
  nohup docker exec -i "$ORACLE_CONTAINER" sqlplus -s "$DSN" > "/tmp/demo_oracle_waiter_$i.log" 2>&1 << 'SQL' &
SELECT * FROM demo_lock_target WHERE id = 1 FOR UPDATE;
COMMIT;
EXIT;
SQL
  sleep 0.3
  i=$((i + 1))
done
echo "{\"status\":\"staged\",\"engine\":\"oracle\",\"scenario\":\"lock_cascade\",\"waiters\":$WAITERS,\"hold_seconds\":$HOLD_SECONDS,\"expect_detection_minutes\":\"1-2\"}"
