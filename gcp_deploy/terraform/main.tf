terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.30"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

locals {
  apis = [
    "compute.googleapis.com",
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudscheduler.googleapis.com",
    "bigquery.googleapis.com",
    "aiplatform.googleapis.com",
    "secretmanager.googleapis.com",
    "vpcaccess.googleapis.com",
    "artifactregistry.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "monitoring.googleapis.com",
    "alloydb.googleapis.com",
    "servicenetworking.googleapis.com",
    "sqladmin.googleapis.com",
  ]
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}
