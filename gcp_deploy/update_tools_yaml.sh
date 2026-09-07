#!/bin/bash
# update_tools_yaml.sh -- hot-patches the LIVE tools.yaml on this VM's
# running Toolbox container to add the new find_blocking_session tool (see
# db_tools.py / pipeline.py's docstrings for why the organic Tier 1 demo
# needs it: kill_blocking_session requires a real sid/serial#, and nothing
# gave the LLM a grounded way to know it before this).
#
# Captures the already-resolved password out of the CURRENT tools.yaml
# before overwriting it (same trick the boot script's grant fix uses), then
# re-runs the exact same placeholder substitution the boot script does, then
# restarts the toolbox container so it picks up the new tool definition --
# Toolbox only reads tools.yaml at startup, not on file change.
#
# This is the "make it work right now" step. The source of truth
# (gcp_deploy/tools_db/tools.yaml) has already been updated to match, so a
# future `terraform apply` + VM reboot will also pick this up permanently --
# this script is what makes it live on the ALREADY-RUNNING VM without
# waiting for that.
set -e

PW=$(grep -m1 "^password:" /var/lib/toolbox/tools.yaml | cut -d: -f2- | xargs)
ORACLE_CONTAINER=$(docker ps --filter "name=klt-oracle-xe" --format "{{.Names}}" | head -n1)
echo "Using Oracle container: $ORACLE_CONTAINER"

cat > /var/lib/toolbox/tools.yaml << 'TOOLBOX_YAML_EOF'
# MCP Toolbox for Databases -- tool definitions for the self-healing agent.
#
# This file is the single source of truth for every SQL statement the agent
# can ever run against Oracle. It is a direct, 1:1 translation of
# config.ALLOWLIST (see ../config.py, copied unchanged from the original
# reference implementation) into Toolbox's tool format -- same action names,
# same tiers (documented in each tool's description so the model sees them),
# same statement templates, same param schemas. config.py stays the single
# source of truth for TIERING and SIGNOFF enforcement (that happens in
# ../guardrail_callbacks.py, in our own process, not in Toolbox); this file
# is only responsible for *how the SQL actually runs*.
#
# One deliberate omission: restart_listener. It's an OS-level `lsnrctl
# restart`, not SQL -- Toolbox is a SQL/database tool server and has no path
# to a host shell. It stays a local ADK FunctionTool in ../db_tools.py that
# fails loudly with the same honest error the reference GCP deployment's
# oracle_client.py already documents, rather than being faked here.
#
# ALTER SYSTEM statements don't accept Oracle bind variables the way normal
# DML does, so the dynamic values below use Toolbox's `templateParameters`
# (textual substitution into the statement, Go-template `{{.name}}` syntax)
# rather than `parameters` (driver-bound placeholders) -- the same reason
# the original reference implementation builds these with Python `.format()`
# instead of a parameterized driver call. See act.py's docstring for the
# same tradeoff stated in the original codebase.

kind: source
name: oracle-db
type: oracle
host: ${ORACLE_HOST}
port: ${ORACLE_PORT}
serviceName: ${ORACLE_SERVICE}
user: ${ORACLE_USER}
password: ${ORACLE_PASSWORD}

---
# --- Read tools (Sense stage -- called directly via toolbox-core, no LLM) ---

kind: tool
name: poll_telemetry
type: oracle-sql
source: oracle-db
readOnly: true
description: >
  Sense-stage poll: current count of sessions blocked by another session's
  lock, and the latest host CPU utilization reading. No parameters.
statement: |
  SELECT
    (SELECT COUNT(*) FROM v$session WHERE blocking_session IS NOT NULL) AS active_blocked_sessions,
    (SELECT NVL(ROUND(value), 0) FROM v$sysmetric
      WHERE metric_name = 'Host CPU Utilization (%)'
      ORDER BY begin_time DESC FETCH FIRST 1 ROW ONLY) AS cpu_utilization_pct
  FROM dual

---
kind: tool
name: describe_state
type: oracle-sql
source: oracle-db
readOnly: true
description: >
  Act-stage pre-action snapshot: same blocked-session count as
  poll_telemetry, taken immediately before a remediation statement runs, so
  every action_executed audit entry carries a pre-state snapshot (Risk 1:
  wrong-diagnosis blast radius).
statement: |
  SELECT COUNT(*) AS active_blocked_sessions_snapshot FROM v$session
  WHERE blocking_session IS NOT NULL

---
kind: tool
name: find_blocking_session
type: oracle-sql
source: oracle-db
readOnly: true
description: >
  Sense/Reason-stage lookup: the real SID and SERIAL# of the session
  currently HOLDING a blocking lock (not the sessions waiting on it) --
  grounds kill_blocking_session's required params in a real fact instead of
  leaving the model to guess them, which would either fail loudly
  (ORA-00030 on a made-up sid) or, worse, risk hitting an unrelated real
  session that happens to share a guessed sid. No parameters.
statement: |
  SELECT
    bs.sid AS blocking_sid,
    bs.serial# AS blocking_serial
  FROM v$session bs
  WHERE bs.sid = (
    SELECT blocking_session FROM v$session
    WHERE blocking_session IS NOT NULL
    FETCH FIRST 1 ROW ONLY
  )

---
# --- Remediation tools (Reason+Act stage -- LLM-callable, one per
#     config.ALLOWLIST entry, guarded by guardrail_callbacks.py) ---

kind: tool
name: kill_blocking_session
type: oracle-sql
source: oracle-db
description: >
  Tier 1 (auto-executes). Terminate a single session holding a blocking
  lock. Use when active_blocked_sessions is sustained_high and the nearest
  runbook points at lock contention.
templateParameters:
  - name: sid
    type: integer
    description: The blocking session's SID (v$session.sid).
  - name: serial
    type: integer
    description: The blocking session's SERIAL# (v$session.serial#), paired with sid.
statement: |
  ALTER SYSTEM KILL SESSION '{{.sid}},{{.serial}}' IMMEDIATE

---
kind: tool
name: kill_runaway_query
type: oracle-sql
source: oracle-db
description: >
  Tier 1 (auto-executes). Terminate a single runaway/long-running query
  session. Same mechanism as kill_blocking_session, kept as a distinct
  allowlisted action so the audit trail records the operator's actual
  diagnosis (lock contention vs. a runaway query), not just the SQL that ran.
templateParameters:
  - name: sid
    type: integer
    description: The runaway session's SID (v$session.sid).
  - name: serial
    type: integer
    description: The runaway session's SERIAL# (v$session.serial#), paired with sid.
statement: |
  ALTER SYSTEM KILL SESSION '{{.sid}},{{.serial}}' IMMEDIATE

---
kind: tool
name: flush_shared_pool
type: oracle-sql
source: oracle-db
description: >
  Tier 2 (auto-executes, logged loudly + paged). Flush the shared pool to
  relieve library cache contention. No parameters.
statement: |
  ALTER SYSTEM FLUSH SHARED_POOL

---
kind: tool
name: increase_pga_target
type: oracle-sql
source: oracle-db
description: >
  Tier 3 (requires human approval -- instance-wide change). Raise
  PGA_AGGREGATE_TARGET by one step to relieve memory pressure.
templateParameters:
  - name: target_mb
    type: integer
    description: New PGA_AGGREGATE_TARGET value, in megabytes.
statement: |
  ALTER SYSTEM SET PGA_AGGREGATE_TARGET = {{.target_mb}}M SCOPE=BOTH

---
kind: toolset
name: db_reads
tools:
  - poll_telemetry
  - describe_state
  - find_blocking_session

---
kind: toolset
name: db_remediation
tools:
  - kill_blocking_session
  - kill_runaway_query
  - flush_shared_pool
  - increase_pga_target

---
kind: toolset
name: db_ops
tools:
  - poll_telemetry
  - describe_state
  - find_blocking_session
  - kill_blocking_session
  - kill_runaway_query
  - flush_shared_pool
  - increase_pga_target
TOOLBOX_YAML_EOF

sed -i \
  -e 's|${ORACLE_HOST}|127.0.0.1|g' \
  -e 's|${ORACLE_PORT}|1521|g' \
  -e 's|${ORACLE_SERVICE}|XEPDB1|g' \
  -e 's|${ORACLE_USER}|executor_sa|g' \
  -e "s|\${ORACLE_PASSWORD}|${PW}|g" \
  /var/lib/toolbox/tools.yaml

docker restart toolbox
echo "tools.yaml updated with find_blocking_session and toolbox container restarted"