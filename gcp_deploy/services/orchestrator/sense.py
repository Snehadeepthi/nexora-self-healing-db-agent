"""
sense.py
Local/dev version of the Sense stage (Step 3 of the implementation guide).
In production this logic runs inside cloud_functions/poller/main.py, polling
real V$ views via python-oracledb on a Cloud Scheduler cron.

Risk 2 mitigation lives partly here: a reading taken inside a configured
maintenance window is tagged so predict.py can suppress it rather than
treating it as a candidate anomaly.
"""

from datetime import datetime

import config


def in_maintenance_window(ts_iso: str) -> bool:
    ts = datetime.fromisoformat(ts_iso)
    weekday, t = ts.weekday(), ts.time()
    return any(
        weekday == wd and start <= t <= end
        for wd, start, end in config.MAINTENANCE_WINDOWS
    )


def poll(oracle_client) -> dict:
    """oracle_client is anything exposing .poll() -- OracleSimulator locally,
    or a real oracledb connection wrapper in production. Propagates outage
    exceptions rather than swallowing them; orchestrator.py is responsible
    for catching them and falling back to alert-only mode."""
    reading = oracle_client.poll()
    reading["in_maintenance_window"] = in_maintenance_window(reading["ts"])
    return reading
