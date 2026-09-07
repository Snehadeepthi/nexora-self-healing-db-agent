gcloud compute ssh "$VM" --zone="$ZONE" --command='
  sudo docker run --rm docker:24-cli sh -c \
    "which apk && apk add --no-cache busybox-extras >/dev/null 2>&1 && which httpd && echo APK_FIX_CONFIRMED"
'#!/bin/bash
# simulate_idle_in_transaction_mysql.sh -- opens REAL idle-in-transaction
# connections against the live MySQL (Cloud SQL) instance, so the deployed
# agent's Sense -> Predict -> Reason -> Act loop can genuinely detect and
# auto-heal it (Tier 2, mysql_terminate_idle_in_transaction) with zero human
# input.
#
# Each of N sessions starts a transaction, runs one query, then genuinely
# goes idle (via mysql client's \! shell-out meta-command, which pauses the
# client without sending anything to the server -- same technique as
# simulate_idle_in_transaction_alloydb.sh's psql \! usage) for long enough to
# cross the 300s cutoff baked into mysql_poll_telemetry's SQL AND
# sp_mysql_terminate_idle_in_transaction's own idle_seconds param, then
# commits.
#
# Detection needs BOTH the 300s idle threshold AND 2 consecutive ticks
# (config.CONSECUTIVE_ANOMALY_THRESHOLD) to confirm, so this takes ~5-6
# minutes total.
set -e
HOST="10.120.0.3"
PORT="3306"
DB="selfhealing"
DBUSER="app_user"
MYSQLIMAGE="mysql:8.0"
IDLE_SECONDS=900   # a bit over the 300s threshold, so it's still idle when ticks confirm it
SESSION_COUNT=4    # threshold is 3 (config.IDLE_IN_TRANSACTION_COUNT_THRESHOLD)
sudo docker image inspect "$MYSQLIMAGE" > /dev/null 2>&1 || sudo docker pull "$MYSQLIMAGE"
PW=$(sudo awk '/name: mysql-db/,/^---$/' /var/lib/toolbox/tools.yaml | grep '^password:' | cut -d: -f2- | xargs)
if [ -z "$PW" ]; then
  echo "Could not extract the MySQL password from /var/lib/toolbox/tools.yaml -- aborting."
  exit 1
fi
echo "Using MySQL host: $HOST:$PORT/$DB as $DBUSER"
echo "Opening $SESSION_COUNT idle-in-transaction sessions, each idle for ${IDLE_SECONDS}s"
for i in $(seq 1 "$SESSION_COUNT"); do
  nohup sudo docker run --rm -i --network host "$MYSQLIMAGE" \
    mysql -h "$HOST" -P "$PORT" -u "$DBUSER" -p"$PW" "$DB" \
    > "/tmp/demo_idle_mysql_$i.log" 2>&1 <<SQL &
START TRANSACTION;
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
