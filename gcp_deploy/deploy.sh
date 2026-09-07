#!/usr/bin/env bash
# Run this from Google Cloud Shell, from the project ROOT directory (the
# folder containing config.py, test_safety.py, and this gcp_deploy/ folder).
#
#   cd self_healing_db_agent
#   bash gcp_deploy/deploy.sh
#
# What it does, in order:
#   1. Sanity-checks you're in the right directory and terraform.tfvars exists.
#   2. terraform init + validate + apply (creates the VPC, the Oracle VM,
#      BigQuery, service accounts, Secret Manager entries, the Cloud Run
#      service shell, and the Cloud Scheduler job).
#   3. Runs the CI safety gate (pytest test_safety.py -v), then builds,
#      pushes, and deploys the orchestrator container image via Cloud Build.
#   4. Prints the URLs/commands you need to actually watch it run.
set -euo pipefail

ROOT_DIR="$(pwd)"
DEPLOY_DIR="$ROOT_DIR/gcp_deploy"
TF_DIR="$DEPLOY_DIR/terraform"

if [[ ! -f "$ROOT_DIR/test_safety.py" || ! -d "$DEPLOY_DIR" ]]; then
  echo "Run this from the project root (the folder with test_safety.py and gcp_deploy/ in it)." >&2
  echo "  cd self_healing_db_agent && bash gcp_deploy/deploy.sh" >&2
  exit 1
fi

if [[ ! -f "$TF_DIR/terraform.tfvars" ]]; then
  echo "Missing $TF_DIR/terraform.tfvars." >&2
  echo "Copy terraform.tfvars.example to terraform.tfvars and fill in your project_id" >&2
  echo "and oracle_db_password first:" >&2
  echo "  cp gcp_deploy/terraform/terraform.tfvars.example gcp_deploy/terraform/terraform.tfvars" >&2
  echo "  nano gcp_deploy/terraform/terraform.tfvars" >&2
  exit 1
fi

PROJECT_ID="$(grep -E '^project_id' "$TF_DIR/terraform.tfvars" | sed -E 's/.*=\s*"(.*)"/\1/')"
REGION="$(grep -E '^region' "$TF_DIR/terraform.tfvars" | sed -E 's/.*=\s*"(.*)"/\1/')"
REGION="${REGION:-us-central1}"

echo "=================================================================="
echo " Self-Healing DB Agent -- GCP deployment"
echo " Project: $PROJECT_ID"
echo " Region:  $REGION"
echo "=================================================================="

gcloud config set project "$PROJECT_ID" >/dev/null

echo
echo "--- Step 1/3: Terraform (infrastructure) ---"
cd "$TF_DIR"
terraform init -input=false
terraform validate
echo
echo "About to run 'terraform apply'. This creates real, billable resources"
echo "(the Oracle VM is the only always-on cost, roughly \$25-35/month if left"
echo "running -- see gcp_deploy/README_DEPLOY.md for the full cost breakdown)."
read -r -p "Continue? [y/N] " CONFIRM
if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
  echo "Aborted."
  exit 1
fi
terraform apply -auto-approve

echo
echo "--- Step 2/3: CI safety gate + build + deploy the orchestrator image ---"
cd "$ROOT_DIR"
gcloud builds submit --config=gcp_deploy/cloudbuild.yaml --region="$REGION" --substitutions=_REGION="$REGION" .

echo
echo "--- Step 3/3: done ---"
cd "$TF_DIR"
ORCHESTRATOR_URL="$(terraform output -raw orchestrator_url)"
cd "$ROOT_DIR"

cat <<EOF

==================================================================
 Deployed.

 Orchestrator URL (private -- IAM-gated, not public):
   $ORCHESTRATOR_URL

 Cloud Scheduler is now calling POST $ORCHESTRATOR_URL/tick every minute.

 To watch it live:
   gcloud logging tail "resource.type=cloud_run_revision AND resource.labels.service_name=self-healing-orchestrator" --format="value(textPayload)"

 To call it yourself (needs roles/run.invoker -- see README_DEPLOY.md):
   curl -X POST -H "Authorization: Bearer \$(gcloud auth print-identity-token)" $ORCHESTRATOR_URL/tick
   curl -H "Authorization: Bearer \$(gcloud auth print-identity-token)" $ORCHESTRATOR_URL/compliance

 Full instructions, including how to test a Tier 3 approval and how to tear
 everything down: gcp_deploy/README_DEPLOY.md
==================================================================
EOF
