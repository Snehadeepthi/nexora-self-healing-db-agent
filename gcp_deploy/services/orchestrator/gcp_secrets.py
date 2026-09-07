"""
gcp_secrets.py
Tiny Secret Manager helper shared by main.py's cold-start bootstrap. Nothing
here is reference-implementation logic -- it exists purely so
oracle_client.py and main.py don't each reimplement the same
access_secret_version() call.
"""
import os

from google.cloud import secretmanager

_client = None


def get_secret(secret_id: str, project_id: str = None) -> str:
    global _client
    if _client is None:
        _client = secretmanager.SecretManagerServiceClient()
    project_id = project_id or os.environ["GCP_PROJECT"]
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = _client.access_secret_version(name=name)
    return response.payload.data.decode("UTF-8")
