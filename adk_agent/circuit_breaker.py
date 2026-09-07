"""
circuit_breaker.py
Risk 5 mitigation: multi-vendor coupling (Oracle + GCP + Vertex AI).

A minimal circuit breaker so a degraded dependency (Oracle unreachable,
Vertex AI erroring, Pub/Sub backlog) trips the pipeline into "alert-only"
mode instead of retrying into an outage or acting on partial/garbled data.
"""

import time
from enum import Enum

import config


class BreakerState(Enum):
    CLOSED = "closed"        # normal operation
    OPEN = "open"             # tripped -- calls short-circuit immediately
    HALF_OPEN = "half_open"   # trial period after reset_seconds elapses


class CircuitOpenError(Exception):
    pass


class CircuitBreaker:
    def __init__(self, name,
                 failure_threshold=config.CIRCUIT_BREAKER_FAILURE_THRESHOLD,
                 reset_seconds=config.CIRCUIT_BREAKER_RESET_SECONDS):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self.state = BreakerState.CLOSED
        self._failure_count = 0
        self._opened_at = None

    def _maybe_half_open(self, now):
        if self.state == BreakerState.OPEN and now - self._opened_at >= self.reset_seconds:
            self.state = BreakerState.HALF_OPEN

    def call(self, fn, *args, now=None, **kwargs):
        now = now if now is not None else time.time()
        self._maybe_half_open(now)

        if self.state == BreakerState.OPEN:
            reopen_at = self._opened_at + self.reset_seconds
            raise CircuitOpenError(
                f"[{self.name}] circuit is OPEN -- refusing call, pipeline should "
                f"fall back to alert-only mode until t={reopen_at:.0f}"
            )
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self._failure_count += 1
            if self._failure_count >= self.failure_threshold:
                self.state = BreakerState.OPEN
                self._opened_at = now
            raise
        else:
            # Any success resets the breaker, including from HALF_OPEN.
            self._failure_count = 0
            self.state = BreakerState.CLOSED
            return result
