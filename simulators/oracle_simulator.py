"""
simulators/oracle_simulator.py
Local stand-in for Oracle Database@Google Cloud (Step 2 of the implementation
guide) plus the python-oracledb driver calls used in sense.py and act.py.
Produces synthetic V$SESSION / V$SYSSTAT-style telemetry, including:
  - normal noise
  - genuine incidents (sustained anomalies)
  - maintenance-window spikes that LOOK anomalous but should be ignored (Risk 2)
  - a simulated outage window to exercise the circuit breaker (Risk 5)
"""

import random
from datetime import datetime, timezone


class OracleOutage(Exception):
    """Raised by poll() while the simulated instance is unreachable."""


class OracleSimulator:
    def __init__(self, seed=42):
        self._rng = random.Random(seed)
        self._tick = 0
        self.outage_ticks = set()      # tick numbers where the instance is "down"
        self.incident_ticks = {}       # tick -> blocked_sessions value (genuine incident)
        self.maintenance_ticks = {}    # tick -> blocked_sessions value (benign spike)

    def schedule_incident(self, tick, blocked_sessions=18, duration=3):
        for t in range(tick, tick + duration):
            self.incident_ticks[t] = blocked_sessions

    def schedule_maintenance_spike(self, tick, blocked_sessions=15, duration=2):
        for t in range(tick, tick + duration):
            self.maintenance_ticks[t] = blocked_sessions

    def schedule_outage(self, tick, duration=4):
        for t in range(tick, tick + duration):
            self.outage_ticks.add(t)

    def poll(self):
        """Simulates one Sense-stage poll of V$SESSION / V$SYSSTAT."""
        t = self._tick
        self._tick += 1

        if t in self.outage_ticks:
            raise OracleOutage(f"tick {t}: simulated Oracle Database@Google Cloud outage")

        if t in self.incident_ticks:
            blocked = self.incident_ticks[t]
        elif t in self.maintenance_ticks:
            blocked = self.maintenance_ticks[t]
        else:
            blocked = self._rng.randint(0, 2)

        cpu_pct = min(99, self._rng.randint(10, 30) + (blocked * 3))

        return {
            "tick": t,
            "ts": datetime.now(timezone.utc).isoformat(),
            "active_blocked_sessions": blocked,
            "cpu_utilization_pct": cpu_pct,
        }


class DbExecutor:
    """Stand-in for the executor-sa's scoped connection used by act.py. Real
    production code calls oracledb here; this just records statements and can
    be told to fail on specific ticks to exercise the circuit breaker."""

    def __init__(self, fail_ticks=None):
        self.fail_ticks = fail_ticks or set()
        self._tick = 0
        self.executed_statements = []

    def describe_state(self):
        """Stand-in for the pre-action snapshot act.py takes before running
        a Tier 1/2 statement (Risk 1)."""
        return {"active_blocked_sessions_snapshot": "captured", "tick": self._tick}

    def execute_statement(self, statement: str):
        t = self._tick
        self._tick += 1
        if t in self.fail_ticks:
            raise RuntimeError(f"simulated executor-sa connection failure at tick {t}")
        self.executed_statements.append(statement)
        return True
