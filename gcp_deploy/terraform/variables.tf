variable "project_id" {
  type        = string
  description = "GCP project ID to deploy into."
}

variable "region" {
  type        = string
  default     = "us-central1"
  description = "Region for Cloud Run, BigQuery, the VPC connector, and Cloud Scheduler."
}

variable "zone" {
  type        = string
  default     = "us-central1-a"
  description = "Zone for the Oracle Compute Engine VM."
}

variable "oracle_db_password" {
  type        = string
  sensitive   = true
  description = "Password for the Oracle XE ORACLE_PASSWORD / APP_USER account. Stored in Secret Manager; also passed to the VM's container metadata at boot (see database.tf's comment on this tradeoff)."
}

variable "notify_slack_webhook_url" {
  type        = string
  sensitive   = true
  default     = ""
  description = "Slack incoming webhook URL for Tier 3 approval requests and status reports. Leave blank to deploy with a placeholder you can update in Secret Manager later."
}

variable "gemini_model" {
  type        = string
  default     = "gemini-2.5-flash"
  description = "Vertex AI Gemini model used by the Reason stage."
}

variable "alert_email" {
  type        = string
  default     = "snehacprojects@gmail.com"
  description = "Email address for Cloud Monitoring reliability alerts (see monitoring.tf) -- Scheduler tick failures and orchestrator 5xx errors."
}

variable "alloydb_password" {
  type        = string
  sensitive   = true
  description = "Password for the AlloyDB cluster's initial 'postgres' superuser. Stored in Secret Manager, same pattern as oracle_db_password."
}

variable "mysql_password" {
  type        = string
  sensitive   = true
  description = "Password for the Cloud SQL MySQL instance's app_user account. Stored in Secret Manager, same pattern as oracle_db_password / alloydb_password."
}

variable "demo_trigger_secret" {
  description = "Shared secret the orchestrator sends (X-Trigger-Secret header) to authenticate to the demo-trigger listener on the Oracle VM."
  type        = string
  sensitive   = true
}
