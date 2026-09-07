#!/usr/bin/env python3
"""
Decouples gcp_audit.py's BigQueryAuditLog.log() from the actual BigQuery
network write. Today log() calls self.client.insert_rows_json(...)
synchronously inline -- every single audit event (anomaly_onset,
anomaly_detected, action_executed, approval_timeout, ...) adds a real
network round-trip directly onto the calling engine's own onset-to-kill
critical path. Each engine now holds its own pipeline_lock (see the
per-engine lock fix), so this stopped being a cross-engine blocking risk --
but it was still needlessly serializing an engine's OWN latency behind
BigQuery's response time.

Fix: log() now updates the in-memory _cache synchronously (unchanged --
this is what mttd_seconds()/mttr_seconds() read, so those stay exactly as
fast as before) and enqueues the row, returning immediately. A single
dedicated background daemon thread drains the queue and performs the
actual insert_rows_json calls one at a time, in the same order events were
logged. A failed background write is logged loudly via Python logging
(visible in Cloud Logging) instead of raised -- there's no caller left to
catch it by the time a queued write actually runs, matching
log_telemetry()'s existing best-effort philosophy in this same file.

Trade-off, stated honestly (see the added __init__ comment): a handful of
rows queued but not yet flushed could be lost if the instance is killed at
that exact moment. BigQuery durability was never guaranteed to the caller
synchronously either -- log() previously raised only on a REJECTED insert,
nothing protected against the process dying between insert_rows_json
returning and the caller's next step -- so this narrows an existing window
rather than opening a new one.

Safety: four independent anchors (imports, __init__'s _cache line,
log_telemetry's def line, and the full log() method body), each verified
to occur exactly once before anything is written. Backs up the file first;
aborts cleanly with no changes if any anchor doesn't match exactly once.
"""
import os

ROOT = "gcp_deploy/services/orchestrator"
FNAME = "gcp_audit.py"
path = os.path.join(ROOT, FNAME)

with open(path, "r") as f:
    src = f.read()


def verify_once(content, anchor, label):
    n = content.count(anchor)
    if n != 1:
        raise SystemExit(
            f"ABORT: expected exactly 1 match for anchor ({label}), found {n}.\n"
            f"--- anchor ---\n{anchor}\n--------------\n"
            f"No files have been modified. Paste this error back for a corrected patch."
        )


# ---------------------------------------------------------------------
# 1. imports + module logger
# ---------------------------------------------------------------------
anchor1 = (
    "import json\n"
    "import os\n"
    "import re\n"
    "from datetime import datetime, timezone\n"
    "\n"
    "from google.cloud import bigquery\n"
)
verify_once(src, anchor1, "imports block")
replacement1 = (
    "import json\n"
    "import logging\n"
    "import os\n"
    "import queue\n"
    "import re\n"
    "import threading\n"
    "from datetime import datetime, timezone\n"
    "\n"
    "from google.cloud import bigquery\n"
    "\n"
    "logger = logging.getLogger(__name__)\n"
)
new_src = src.replace(anchor1, replacement1, 1)

# ---------------------------------------------------------------------
# 2. __init__ -- add the write queue + background writer thread
# ---------------------------------------------------------------------
anchor2 = "        self._cache = []  # this instance's own events, for fast mttd/mttr lookups\n"
verify_once(new_src, anchor2, "__init__ _cache line")
replacement2 = anchor2 + (
    "        # Reliability/latency safeguard: log() used to call\n"
    "        # bigquery_client.insert_rows_json() synchronously inline, so every\n"
    "        # audit event added a real network round-trip directly onto the\n"
    "        # calling engine's own detection-to-remediation critical path.\n"
    "        # Each engine now holds its own pipeline_lock (see pipeline.py), so\n"
    "        # this stopped being a cross-engine blocking risk -- but it was\n"
    "        # still needlessly serializing this engine's own onset-to-kill\n"
    "        # latency behind BigQuery's response time. A single dedicated\n"
    "        # background thread now drains a queue and performs the actual\n"
    "        # insert_rows_json calls one at a time, in the same order they\n"
    "        # were logged -- log() itself just enqueues and returns. events()/\n"
    "        # mttd_seconds()/mttr_seconds() are unaffected: they only ever read\n"
    "        # _cache, which log() still updates synchronously and immediately,\n"
    "        # before the row is even enqueued.\n"
    "        # Trade-off, stated honestly: a handful of rows queued but not yet\n"
    "        # flushed could be lost if the instance is killed at that exact\n"
    "        # moment (scale-down, deploy, crash). BigQuery durability was never\n"
    "        # guaranteed to the caller synchronously either -- log() previously\n"
    "        # raised only on a REJECTED insert, nothing protected against the\n"
    "        # process dying between insert_rows_json returning and the\n"
    "        # caller's next step -- so this narrows an existing window rather\n"
    "        # than opening a new one. A failed background write is logged\n"
    "        # loudly via Python logging (visible in Cloud Logging) instead of\n"
    "        # raised, matching log_telemetry()'s existing best-effort philosophy.\n"
    "        self._write_queue = queue.Queue()\n"
    "        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)\n"
    "        self._writer_thread.start()\n"
)
new_src = new_src.replace(anchor2, replacement2, 1)

# ---------------------------------------------------------------------
# 3. add _writer_loop() right before log_telemetry()
# ---------------------------------------------------------------------
anchor3 = "    def log_telemetry(self, reading: dict):\n"
verify_once(new_src, anchor3, "log_telemetry def line")
replacement3 = (
    "    def _writer_loop(self):\n"
    '        """Runs on a single dedicated daemon thread for the lifetime of\n'
    "        this instance, draining self._write_queue one row at a time so\n"
    "        BigQuery writes never block whichever engine's thread called\n"
    "        log(). Errors are logged loudly (Cloud Logging picks these up)\n"
    "        rather than raised -- there is no caller left to catch them by\n"
    '        the time a queued write actually runs."""\n'
    "        while True:\n"
    "            row = self._write_queue.get()\n"
    "            try:\n"
    "                errors = self.client.insert_rows_json(self._table_ref, [row])\n"
    "                if errors:\n"
    '                    logger.error("BigQuery audit_log insert failed (background writer): %s | row=%s", errors, row)\n'
    "            except Exception as e:\n"
    '                logger.error("BigQuery audit_log insert raised (background writer): %s | row=%s", e, row)\n'
    "            finally:\n"
    "                self._write_queue.task_done()\n"
    "\n"
) + anchor3
new_src = new_src.replace(anchor3, replacement3, 1)

# ---------------------------------------------------------------------
# 4. log() -- enqueue instead of writing inline
# ---------------------------------------------------------------------
anchor4 = (
    '    def log(self, event_type: str, **detail):\n'
    '        ts = datetime.now(timezone.utc)\n'
    '        incident_id = detail.get("incident_id")\n'
    '        row = {\n'
    '            "ts": ts.isoformat(),\n'
    '            "event_type": event_type,\n'
    '            "incident_id": incident_id,\n'
    '            "detail": json.dumps(detail, default=str),\n'
    '        }\n'
    '        errors = self.client.insert_rows_json(self._table_ref, [row])\n'
    '        if errors:\n'
    '            raise RuntimeError(f"BigQuery insert failed: {errors}")\n'
    '        cached = {"ts": ts, "event_type": event_type, "incident_id": incident_id, "detail": detail}\n'
    '        self._cache.append(cached)\n'
    '        return cached\n'
)
verify_once(new_src, anchor4, "log() method body")
replacement4 = (
    '    def log(self, event_type: str, **detail):\n'
    '        ts = datetime.now(timezone.utc)\n'
    '        incident_id = detail.get("incident_id")\n'
    '        row = {\n'
    '            "ts": ts.isoformat(),\n'
    '            "event_type": event_type,\n'
    '            "incident_id": incident_id,\n'
    '            "detail": json.dumps(detail, default=str),\n'
    '        }\n'
    '        cached = {"ts": ts, "event_type": event_type, "incident_id": incident_id, "detail": detail}\n'
    '        self._cache.append(cached)\n'
    '        # Enqueue rather than write inline -- see the background-writer\n'
    '        # note in __init__. Returns immediately; the actual BigQuery\n'
    '        # write happens on the dedicated writer thread, in the same\n'
    '        # order events were logged.\n'
    '        self._write_queue.put(row)\n'
    '        return cached\n'
)
new_src = new_src.replace(anchor4, replacement4, 1)

# ---------------------------------------------------------------------
# All anchors verified -- back up and write.
# ---------------------------------------------------------------------
backup_path = path + ".bak.asyncaudit"
with open(backup_path, "w") as f:
    f.write(src)
with open(path, "w") as f:
    f.write(new_src)

print(f"OK: patched {path} (backup at {backup_path})")
print("BigQuery audit writes now go through a background queue instead of blocking log() callers.")
