"""
llm_client.py
Local/dev stand-in for Vertex AI Gemini 2.5 Flash (Step 5). Rather than
calling a real model, this returns a deterministic, schema-constrained action
proposal -- but it enforces the SAME contract the real prompt enforces in
production: the model may only return an action_key that exists in
config.ALLOWLIST plus scalar parameters, never SQL text.

Risk 1 mitigation: `generate_action()` validates its own output against the
allowlist's param_schema before returning it, and raises
HallucinatedActionError if the (simulated) model ever proposes something
outside the allowlist. This is the "hallucination firewall" restated as an
enforced contract rather than just a prompt instruction -- swap
`_propose_action_key()`'s body for a real
`GenerativeModel("gemini-2.5-flash").generate_content(prompt)` call to go to
production; the validation below stays exactly as-is.
"""

import config


class HallucinatedActionError(Exception):
    pass


def _propose_action_key(event: dict, runbook) -> str:
    """Stand-in for the model's reasoning. In production this is Gemini's
    response; here it's a direct, deterministic mapping so the demo is
    reproducible without an API key."""
    if runbook is not None:
        return runbook.resolution_action_key
    return "UNKNOWN"  # deliberately not in ALLOWLIST -- exercises the firewall


def generate_action(event: dict, runbook) -> dict:
    action_key = _propose_action_key(event, runbook)

    if action_key not in config.ALLOWLIST:
        raise HallucinatedActionError(
            f"Model proposed '{action_key}', which is not in the allowlist. "
            f"Rejected before it ever reached the executor."
        )

    action = config.ALLOWLIST[action_key]

    # Build minimal, deterministic scalar params for the reference demo. In
    # production these come from the model's structured JSON output and are
    # validated against `action.param_schema` field-by-field exactly as below.
    params = {}
    for name, ptype in action.param_schema.items():
        if name in ("sid", "serial"):
            params[name] = event.get(name, 101)
        elif name == "target_mb":
            params[name] = 4096
        elif name == "listener_name":
            params[name] = "LISTENER"
        else:
            params[name] = ptype()

    for name, value in params.items():
        expected_type = action.param_schema[name]
        if not isinstance(value, expected_type):
            raise HallucinatedActionError(
                f"Param '{name}' for action '{action_key}' failed schema "
                f"validation (expected {expected_type.__name__})."
            )

    return {
        "action_key": action_key,
        "params": params,
        "issue": event.get("query_text", "unknown"),
        "query_text": event.get("query_text", ""),
    }
