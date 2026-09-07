FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8080
EXPOSE 8080

# --workers 1: this service holds pipeline state (the consecutive-anomaly
# counter, the Tier 3 pending_approvals queue) as in-process singletons,
# exactly like orchestrator.Orchestrator's instance attributes locally.
# Multiple worker processes would each get their own copy and silently
# diverge -- see main.py's module docstring.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "60", "main:app"]
