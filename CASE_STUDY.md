# Building NEXORA: A Technical Case Study

### Three engineering decisions behind a self-healing database agent — and how each one was proven, not just claimed

NEXORA is a self-healing database agent that watches Oracle, AlloyDB, and MySQL simultaneously, diagnoses operational incidents, and remediates them through a shared reasoning layer built on Google's Agent Development Kit (ADK). It was built for the Patchamomma 2026 hackathon and is live in production today.

Most write-ups about a project like this describe the happy path: sense, predict, reason, act, learn. This one doesn't. It's about three moments where the design didn't work the first time — a network path that silently corrupted an authentication handshake, a guardrail system that had to be designed against an LLM's own confidence, and a safety mechanism that wasn't trusted until it was proven live, at 2 a.m., on the actual production system. Each section below covers one of those moments: what broke, why, and how it was fixed and verified.

## 1. The O5LOGON Story — when "just route through the VPC connector" broke Oracle

NEXORA's database access never touches SQL directly. Every read and write goes through MCP Toolbox for Databases, which exposes each engine as a small set of allowlisted tools. The natural architecture is one shared Toolbox instance serving all three engines — simpler to operate, cheaper to run, one thing to monitor instead of two.

That's what NEXORA ran for most of its build. Toolbox lived as its own Cloud Run service, reaching Oracle's listener on its Compute Engine VM over a Serverless VPC Access connector — the standard, documented way for a Cloud Run service to reach a VM on a private network.

It didn't work. Every connection attempt from that path failed with a generic `ORA-01017: invalid username/password`, even with credentials that were verified correct against the same database moments earlier from a direct connection. The obvious hypotheses — a typo in Secret Manager, a stale password, a missed IAM grant — were all ruled out one at a time. The credentials were fine. The path wasn't.

Oracle's `O5LOGON` handshake is a binary authentication protocol, not a text-based one like most HTTP-era database drivers. Routing it through the VPC connector's network address translation layer was corrupting the handshake in transit — not rejecting it outright, not timing out, just subtly mangling enough bytes that Oracle's own generic-credentials error was the only symptom that ever surfaced. From the outside, it looked exactly like a password problem. It was actually a wire-protocol problem.

The fix wasn't a redesign — it was an acknowledgment that Oracle's connection has a constraint AlloyDB and MySQL don't. Toolbox's own `tools.yaml` already defined each engine as an independent named source, so the actual change was operational, not architectural: Oracle's Toolbox instance moved onto the same VM as the Oracle database itself, talking to it over `127.0.0.1` — a loopback connection that can't be corrupted by a network path it never traverses. AlloyDB and MySQL, which never exhibited this failure mode, stayed on a shared Toolbox instance on Cloud Run, reached over OIDC-authenticated IAM.

That split had a second-order benefit that only became obvious afterward: a single shared Toolbox instance was also a single point of failure across all three engines. An OS panic or heavy I/O thrashing on the Oracle VM would have blinded AlloyDB and MySQL's telemetry too, for a reason that had nothing to do with them. Splitting Toolbox by necessity also happened to cut the blast radius of an Oracle-side failure from three engines down to one — verified live by deliberately stopping the Oracle VM's Toolbox container and confirming `/database/health?db=alloydb` and `?db=mysql` stayed healthy throughout while only Oracle correctly reported unreachable.

**The lesson:** a generic auth error is not proof of a credentials problem. When a well-documented pattern fails in a way that doesn't match its own failure modes, the fastest path to a real answer is testing the transport layer directly, not re-checking the same credentials for the fourth time.

## 2. Three tiers, one decision layer — guardrails an LLM can't talk its way around

An agent that can restart a listener or kill a database session is only as trustworthy as the boundary around what it's allowed to decide unilaterally. NEXORA's answer is a three-tier guardrail system, and the design constraint that shaped it was specific: the tier a given action falls under has to be decided by policy, not by the model's own judgment call in the moment.

- **Tier 1** executes automatically and silently — actions like `kill_blocking_session` or `kill_runaway_query`, where the blast radius of being wrong is small and the fix is trivially reversible in effect (the session was going to be killed by a timeout eventually anyway).
- **Tier 2** executes automatically but loudly — actions like `flush_shared_pool` or `terminate_idle_in_transaction`, logged to the audit trail and pushed to Slack as an informational ping, not a request.
- **Tier 3** never executes without a human. Actions like `restart_listener` or `reset_all_connections` post an approval card to Slack and wait.

The part that actually took engineering effort isn't the three-way split — it's making sure the LLM can't route around it. Every allowlisted action carries its tier as a property of the *action itself*, defined in code before any incident exists, not inferred by the model at decision time. When Reason+Act proposes a fix, the tier lookup happens against that static table — the model chooses *which* allowlisted action applies to the incident, but it has no path to choosing *how carefully* that action gets treated. An LLM confidently mis-assessing severity ("this looks safe enough to just run it") is a real failure mode for agentic systems; NEXORA doesn't ask the model to make that call at all.

Tier 3's approval flow has its own failure mode: what happens when the human never answers? An approval sitting in `action_pending_approval` indefinitely, while a lock cascade or connection storm keeps getting worse, is arguably more dangerous than either auto-executing or doing nothing. The fix is a 900-second TTL — if unanswered, the approval expires, logs an `approval_timeout` audit event, and re-notifies. A human still finds out. Nothing ran without consent. That mechanism is the subject of the next section, because a timeout that's only been unit-tested is a claim, not a fact.

**The lesson:** a tiered-autonomy system is only as safe as its least-flexible boundary. The moment a model can reason its way into a lower tier for a given action, the tiers are guidance, not guardrails. NEXORA keeps that decision entirely out of the model's hands.

## 3. Proving the guardrail, not just coding it — the 900-second live-fire test

`test_safety.py` covers the Tier 3 TTL logic — seven unit tests, all passing, exercising the expiry path against a mocked clock. That's necessary. It's not sufficient. A passing test suite proves the code does what the code was written to do; it doesn't prove the deployed system does what the test suite assumes about it. So on 2026-09-01, the TTL was tested against the live, running production system instead.

The setup required making a real anomaly achievable on a human timescale. AlloyDB's connection-percentage threshold was temporarily lowered from its production value of 0.8 to 0.08, making a real connection-storm anomaly reachable with roughly 100 held connections instead of an impractical ~800. A controlled connection storm was then launched from the Oracle VM against AlloyDB's actual private IP, holding around 100 concurrent connections open against the real database — not a mock, not a staged log entry.

Predict correctly confirmed the anomaly across two consecutive ticks, and Reason+Act proposed `alloydb_reset_all_connections`, a Tier 3 action. At that point the test stopped being a setup and became a wait: the approval request was deliberately left untouched. No Slack click. No dashboard interaction. Just the clock.

```
14:42:14 UTC  action_pending_approval — alloydb-incident-1788273661 created
14:58:07 UTC  approval_timeout — auto-expired at the 900s TTL boundary;
              audit event logged with the correct incident ID, action key,
              and detail message; matching Slack notification fired
```

Fifteen minutes and fifty-three seconds later — within the expected window of the 900-second boundary — the approval expired exactly as designed, logged to BigQuery with the correct incident context, and fired the matching Slack notification. The threshold was reverted to 0.8 and redeployed immediately after, with a clean, error-free deploy confirmed.

This same discipline — don't trust a mechanism until it's been forced to happen for real — was repeated later for two other guardrails ahead of Touchpoint 3: the suppression auto-clear (verified by watching one real incident log `action_pending_approval` → `approval_timeout` → `action_suppression_auto_cleared` in sequence, the last step with zero human input) and the Toolbox blast-radius split described in Section 1 (verified by actually stopping the Oracle-side container, not by reading the failover code and trusting it).

**The lesson:** for a system that's allowed to act autonomously on production infrastructure, "the tests pass" and "this works" are different claims. The second one requires inducing the actual failure condition against the actual deployed system and watching the audit trail, not the code, tell you what happened.

## What this adds up to

None of these three stories is about a feature working correctly the first time. They're about a generic error message that turned out to mean something specific, a design decision that had to remove a choice from the model rather than constrain it, and a safety mechanism that wasn't allowed to be "probably fine" — it had to be proven. That's the standard NEXORA was held to throughout: nine issues raised across two independent rounds of external program review, all closed with deployed fixes, and every one of the significant claims in this document — the O5LOGON diagnosis, the tier boundary, the TTL expiry — verified against the live, running system rather than asserted from the code.

---
*NEXORA — Self-Healing Database Agent · Built for Patchamomma 2026 · Sneha Deepthi Cheenepalli*
*Live: https://self-healing-orchestrator-casezd3hrq-uc.a.run.app/*
