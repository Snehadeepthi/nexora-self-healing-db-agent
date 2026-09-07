"""
cost_guard.py
Risk 6 mitigation: cost overrun at scale.

Wraps every Vertex AI (Gemini) call with a rolling-window rate limit and a
running cost estimate, so a noisy-telemetry storm or false-positive loop
cannot silently run up the bill. Exceeding either cap degrades gracefully to
"alert only, no LLM reasoning" rather than failing hard.
"""

import time
from collections import deque

import config


class CostGuard:
    def __init__(self,
                 max_calls_per_window=config.MAX_GEMINI_CALLS_PER_WINDOW,
                 window_seconds=config.GEMINI_CALL_WINDOW_SECONDS,
                 monthly_budget_usd=config.MONTHLY_BUDGET_USD,
                 cost_per_call_usd=0.02):
        self.max_calls_per_window = max_calls_per_window
        self.window_seconds = window_seconds
        self.monthly_budget_usd = monthly_budget_usd
        self.cost_per_call_usd = cost_per_call_usd
        self._call_times = deque()
        self._month_spend_usd = 0.0
        self._alerts = []

    def _prune(self, now):
        while self._call_times and now - self._call_times[0] > self.window_seconds:
            self._call_times.popleft()

    def check_and_record(self, now=None) -> bool:
        """Returns True if a Gemini call is allowed (and records it), False
        if the caller should fall back to alert-only mode instead."""
        now = now if now is not None else time.time()
        self._prune(now)

        if len(self._call_times) >= self.max_calls_per_window:
            self._alerts.append(
                f"Rate cap hit: {len(self._call_times)} Gemini calls in the last "
                f"{self.window_seconds}s window -- falling back to alert-only mode."
            )
            return False

        projected_spend = self._month_spend_usd + self.cost_per_call_usd
        if projected_spend > self.monthly_budget_usd:
            self._alerts.append(
                f"Monthly budget cap hit (${self.monthly_budget_usd}) -- "
                f"falling back to alert-only mode."
            )
            return False

        self._call_times.append(now)
        self._month_spend_usd += self.cost_per_call_usd
        return True

    @property
    def alerts(self):
        return list(self._alerts)

    @property
    def month_spend_usd(self):
        return round(self._month_spend_usd, 4)
