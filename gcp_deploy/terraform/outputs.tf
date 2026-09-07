output "orchestrator_url" {
  value       = google_cloud_run_v2_service.orchestrator.uri
  description = "The private Cloud Run URL. Only the Scheduler SA (and anyone you grant roles/run.invoker) can call it."
}

output "toolbox_url" {
  value       = "http://${google_compute_instance.oracle_db.network_interface[0].network_ip}:5000"
  description = "MCP Toolbox for Databases, running as a plain container on the Oracle VM. VPC-internal only. To debug directly, SSH to the VM (via IAP) and curl 127.0.0.1:5000."
}

output "oracle_vm_internal_ip" {
  value       = google_compute_instance.oracle_db.network_interface[0].network_ip
  description = "Private IP of the Oracle XE VM -- not reachable from outside the VPC."
}

output "oracle_vm_name" {
  value       = google_compute_instance.oracle_db.name
  description = "Pass to `gcloud compute ssh` (via IAP) for direct access to the DB host."
}

output "bigquery_dataset" {
  value       = google_bigquery_dataset.db_ops.dataset_id
  description = "BigQuery dataset holding telemetry, audit_log, and runbooks."
}

output "artifact_registry_repo" {
  value       = google_artifact_registry_repository.repo.repository_id
  description = "Where cloudbuild.yaml pushes the orchestrator container image."
}

output "alloydb_cluster_id" {
  value       = google_alloydb_cluster.primary.cluster_id
  description = "AlloyDB cluster id, for gcloud alloydb commands."
}
output "alloydb_primary_ip" {
  value       = google_alloydb_instance.primary.ip_address
  description = "Private IP of the AlloyDB primary instance -- reachable from the Oracle VM (same VPC) where Toolbox runs, via VPC peering. Not reachable from outside the VPC."
}

output "mysql_instance_connection_name" {
  value       = google_sql_database_instance.mysql.connection_name
  description = "Cloud SQL connection name for the MySQL instance."
}

output "mysql_private_ip" {
  value       = google_sql_database_instance.mysql.private_ip_address
  description = "Private IP address of the MySQL instance."
}
