"""
agent.py
The Reason + Act stages (Steps 5-6 of the original implementation guide),
now a single ADK LlmAgent turn instead of a manual
`llm_client.generate_action()` call followed by a manual
`GuardedExecutionEngine.execute()` call. The model itself decides which
allowlisted tool (if any) to call and with what arguments -- Gemini's
native function-calling already constrains it to the declared tool schemas,
and guardrail_callbacks.Guardrails enforces everything config.ALLOWLIST /
allowlist_governor.py / circuit_breaker.py / cost_guard.py already enforced
before, at the same choke points, just reached via ADK callbacks instead of
direct method calls. See guardrail_callbacks.py's module docstring for the
full mapping.

This is a genuine architectural upgrade over the original reference
implementation's reason.py, not just a relabeling: the LLM natively proposes
a tool call with structured arguments (validated by Gemini's own
function-calling schema before it even reaches our code), rather than us
parsing free-form JSON text out of a prompt response.
"""

import os

import config
import db_tools
from google.adk.agents import LlmAgent


def _allowlist_instruction_text() -> str:
    """Same content llm_client.py's _allowlist_schema_text() generated for
    the prompt, restated as tool-calling guidance rather than a JSON-mode
    output contract -- the model calls a tool now instead of describing one
    in text."""
    lines = []
    for key, action in config.ALLOWLIST.items():
        params = ", ".join(f"{n}: {t.__name__}" for n, t in action.param_schema.items()) or "(no params)"
        lines.append(f"- [{action.engine}] {key} (Tier {int(action.tier)}): {action.description} | params: {params}")
    return "\n".join(lines)


INSTRUCTION = f"""You are a database SRE diagnosis assistant. You work
across multiple database engines -- Oracle, AlloyDB/Postgres, and MySQL
today, with more registered over time -- and each incident tells you
exactly which one it concerns; only ever propose actions for that engine.
You may ONLY act by calling one of your provided tools -- never
describe SQL in your reply, never invent an action that isn't one of your
tools. If none of your tools genuinely fit the incident described to you,
do not call any tool -- just explain why in your reply.

Every tool call you make is independently re-validated against the same
allowlist and parameter schema server-side before it can run (this happens
whether or not you call the tool correctly), and any Tier 3 action pauses
for human approval before it ever executes -- so propose the single best-fit
action for the incident, don't hedge with multiple calls.

Your available actions, for reference (Tier 1/2 auto-execute, Tier 3 needs
human approval):
{_allowlist_instruction_text()}

You will be given the incident's metric, status, value, and the nearest
matching runbook (if any) in each message. Ground your action choice in
that runbook when one is given.
"""


def build_agent(guardrails) -> LlmAgent:
    """guardrails is a guardrail_callbacks.Guardrails instance -- passed in
    (rather than constructed here) so pipeline.py's AdkOrchestrator and the
    Tier 3 approve() path share the exact same pending_approvals dict and
    breaker/cost_guard state the agent's callbacks populate."""
    return LlmAgent(
        name="db_reasoner",
        model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        instruction=INSTRUCTION,
        tools=[db_tools.load_remediation_toolset(), db_tools.restart_listener_tool],
        before_model_callback=guardrails.before_model,
        after_model_callback=guardrails.after_model,
        before_tool_callback=guardrails.before_tool,
        after_tool_callback=guardrails.after_tool,
        on_tool_error_callback=guardrails.on_tool_error,
    )
