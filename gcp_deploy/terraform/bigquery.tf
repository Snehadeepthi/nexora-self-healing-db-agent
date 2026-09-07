# db_ops dataset -- the production home for everything the reference
# implementation keeps in-memory (audit.AuditLog's event list,
# runbooks.RunbookStore's approved/pending lists) plus a telemetry table
# that starts accumulating history from the first tick, so the guide's
# Step 4 BigQuery ML ARIMA_PLUS model has something to train against once
# there's enough of it.

resource "google_bigquery_dataset" "db_ops" {
  dataset_id = "db_ops"
  location   = var.region
  depends_on = [google_project_service.apis]
}

resource "google_bigquery_table" "telemetry" {
  dataset_id = google_bigquery_dataset.db_ops.dataset_id
  table_id   = "telemetry"
  schema = jsonencode([
    { name = "ts", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "active_blocked_sessions", type = "INTEGER", mode = "NULLABLE" },
    { name = "cpu_utilization_pct", type = "INTEGER", mode = "NULLABLE" },
    { name = "in_maintenance_window", type = "BOOLEAN", mode = "NULLABLE" },
  ])
}

resource "google_bigquery_table" "audit_log" {
  dataset_id = google_bigquery_dataset.db_ops.dataset_id
  table_id   = "audit_log"
  schema = jsonencode([
    { name = "ts", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "event_type", type = "STRING", mode = "REQUIRED" },
    { name = "incident_id", type = "STRING", mode = "NULLABLE" },
    { name = "detail", type = "STRING", mode = "NULLABLE" }, # JSON-encoded text; see gcp_audit.py
  ])
}

resource "google_bigquery_table" "runbooks" {
  dataset_id = google_bigquery_dataset.db_ops.dataset_id
  table_id   = "runbooks"
  schema = jsonencode([
    { name = "title", type = "STRING", mode = "REQUIRED" },
    { name = "body", type = "STRING", mode = "REQUIRED" },
    { name = "resolution_action_key", type = "STRING", mode = "REQUIRED" },
    { name = "status", type = "STRING", mode = "REQUIRED" }, # approved | pending_review
    { name = "created_at", type = "TIMESTAMP", mode = "REQUIRED" },
    { name = "reviewed_by", type = "STRING", mode = "NULLABLE" },
  ])
}
