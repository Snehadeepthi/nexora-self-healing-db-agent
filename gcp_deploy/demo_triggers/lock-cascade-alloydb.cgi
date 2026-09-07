#!/bin/sh
# lock-cascade-alloydb.cgi
# CGI handler served by busybox httpd on the Oracle VM's demo-trigger
# container. Stages a REAL blocking-session scenario against the live
# AlloyDB instance. Same mechanism as simulate_blocking_alloydb.sh: one
# holder locks a row via SELECT ... FOR UPDATE and holds it with
# pg_sleep(180), 9 waiters queue up behind it. Requires X-Trigger-Secret
# to match TRIGGER_SECRET.
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
HOLD_SECONDS=180
WAITERS=9
PW=$(awk '/name: alloydb-db/,/^---$/' /var/lib/toolbox/tools.yaml | grep '^password:' | cut -d: -f2- | sed 's/^ *//;s/ *$//')
echo "Content-type: application/json"
echo
if [ -z "$PW" ]; then
  echo '{"status":"error","detail":"could not read AlloyDB password"}'
  exit 0
fi
docker image inspect "$PGIMAGE" >/dev/null 2>&1 || docker pull "$PGIMAGE" >/dev/null 2>&1
docker run --rm -i --network host -e PGPASSWORD="$PW" "$PGIMAGE" \
  psql -h "$HOST" -p "$PORT" -U "$DBUSER" -d "$DB" -v ON_ERROR_STOP=0 \
  > /tmp/demo_alloydb_setup.log 2>&1 << 'SQL'
CREATE TABLE IF NOT EXISTS demo_lock_target (id INTEGER PRIMARY KEY, val INTEGER);
INSERT INTO demo_lock_target (id, val) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;
SQL
nohup docker run --rm -i --network host -e PGPASSWORD="$PW" "$PGIMAGE" \
  psql -h "$HOST" -p "$PORT" -U "$DBUSER" -d "$DB" \
  > /tmp/demo_alloydb_holder.log 2>&1 << 'SQL' &
BEGIN;
SELECT * FROM demo_lock_target WHERE id = 1 FOR UPDATE;
SELECT pg_sleep(180);
COMMIT;
SQL
sleep 3
i=1
while [ "$i" -le "$WAITERS" ]; do
  nohup docker run --rm -i --network host -e PGPASSWORD="$PW" "$PGIMAGE" \
    psql -h "$HOST" -p "$PORT" -U "$DBUSER" -d "$DB" \
    > "/tmp/demo_alloydb_waiter_$i.log" 2>&1 << 'SQL' &
BEGIN;
SELECT * FROM demo_lock_target WHERE id = 1 FOR UPDATE;
COMMIT;
SQL
  sleep 0.3
  i=$((i + 1))
done
echo "{\"status\":\"staged\",\"engine\":\"alloydb\",\"scenario\":\"lock_cascade\",\"waiters\":$WAITERS,\"hold_seconds\":$HOLD_SECONDS,\"expect_detection_minutes\":\"1-2\"}"
