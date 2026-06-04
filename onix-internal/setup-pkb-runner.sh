#!/usr/bin/env bash
# Long-lived PKB benchmark network + runner VM in GCP.
#
# Creates a custom VPC that PKB deploys ephemeral GKE clusters into (via
# --gce_network_name/--gce_subnet_name), a persistent Cloud NAT for private-node
# egress, IAP-only SSH, and a c4-standard-16 runner VM you VS Code into over an
# IAP tunnel. Run once. Safe to re-run: each create is guarded so existing
# resources are skipped.
set -uo pipefail

PROJECT="ehole-benchmark-temp-z8s8"
REGION="us-central1"
ZONE="us-central1-a"
NET="pkb-bench-net"
SUBNET="pkb-bench-net"            # same name as the VPC (matches PKB's NAT naming)
SUBNET_RANGE="10.0.0.0/20"        # ~4094 node/VM primary IPs; GKE auto-allocates pod/svc ranges
RUNNER="pkb-runner"
RUNNER_MACHINE="c4-standard-16"

run() { echo "+ $*"; "$@"; }
# Create something only if a probe command fails (resource absent).
ensure() { # ensure <describe-cmd...> -- <create-cmd...>
  local probe=() create=() seen=0
  for a in "$@"; do
    if [[ "$a" == "--" ]]; then seen=1; continue; fi
    if [[ $seen -eq 0 ]]; then probe+=("$a"); else create+=("$a"); fi
  done
  if "${probe[@]}" >/dev/null 2>&1; then
    echo "= exists, skipping: ${create[*]:0:4} ..."
  else
    run "${create[@]}"
  fi
}

echo "### 0. Enable APIs"
run gcloud services enable compute.googleapis.com container.googleapis.com --project="$PROJECT"

echo "### 1. VPC + subnet (private Google access on, so private nodes/VM reach Google APIs)"
ensure gcloud compute networks describe "$NET" --project="$PROJECT" \
  -- gcloud compute networks create "$NET" --project="$PROJECT" \
       --subnet-mode=custom --bgp-routing-mode=regional
ensure gcloud compute networks subnets describe "$SUBNET" --project="$PROJECT" --region="$REGION" \
  -- gcloud compute networks subnets create "$SUBNET" --project="$PROJECT" \
       --network="$NET" --region="$REGION" --range="$SUBNET_RANGE" \
       --enable-private-ip-google-access

echo "### 2. Cloud Router + NAT (names match PKB's <subnet>-router/-nat so its per-run create is a no-op)"
ensure gcloud compute routers describe "${SUBNET}-router" --project="$PROJECT" --region="$REGION" \
  -- gcloud compute routers create "${SUBNET}-router" --project="$PROJECT" \
       --network="$NET" --region="$REGION"
ensure gcloud compute routers nats describe "${SUBNET}-nat" --router="${SUBNET}-router" --project="$PROJECT" --region="$REGION" \
  -- gcloud compute routers nats create "${SUBNET}-nat" --project="$PROJECT" \
       --router="${SUBNET}-router" --region="$REGION" \
       --auto-allocate-nat-external-ips --nat-all-subnet-ip-ranges

echo "### 3. Firewall: IAP SSH to the runner + intra-VPC"
ensure gcloud compute firewall-rules describe "${NET}-allow-iap-ssh" --project="$PROJECT" \
  -- gcloud compute firewall-rules create "${NET}-allow-iap-ssh" --project="$PROJECT" \
       --network="$NET" --direction=INGRESS --action=ALLOW --rules=tcp:22 \
       --source-ranges=35.235.240.0/20 --target-tags=pkb-runner
ensure gcloud compute firewall-rules describe "${NET}-allow-internal" --project="$PROJECT" \
  -- gcloud compute firewall-rules create "${NET}-allow-internal" --project="$PROJECT" \
       --network="$NET" --direction=INGRESS --action=ALLOW --rules=tcp,udp,icmp \
       --source-ranges="$SUBNET_RANGE"

echo "### 4. Runner VM (no external IP; SSH via IAP). Boot disk: hyperdisk-balanced (required by c4)."
echo "    Uses the default compute SA; you will 'gcloud auth login' as yourself on the VM."
ensure gcloud compute instances describe "$RUNNER" --project="$PROJECT" --zone="$ZONE" \
  -- gcloud compute instances create "$RUNNER" --project="$PROJECT" --zone="$ZONE" \
       --machine-type="$RUNNER_MACHINE" \
       --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
       --boot-disk-size=100GB --boot-disk-type=hyperdisk-balanced \
       --network="$NET" --subnet="$SUBNET" --no-address \
       --tags=pkb-runner \
       --metadata=startup-script='#!/bin/bash
set -e
apt-get update
apt-get install -y python3-pip python3-venv git curl apt-transport-https ca-certificates gnupg build-essential
echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" > /etc/apt/sources.list.d/google-cloud-sdk.list
curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
apt-get update
apt-get install -y google-cloud-cli google-cloud-cli-gke-gcloud-auth-plugin kubectl
'

echo
echo "### DONE. To connect (VS Code remote uses the same IAP ProxyCommand):"
echo "  gcloud compute ssh ${RUNNER} --project=${PROJECT} --zone=${ZONE} --tunnel-through-iap"
echo
echo "### On the VM, auth as yourself and set up PKB (startup script installed gcloud/kubectl/python):"
echo "  gcloud auth login && gcloud auth application-default login"
echo "  gcloud config set project ${PROJECT}"
echo "  git clone https://github.com/onix-net/PerfKitBenchmarker.git pkb && cd pkb"
echo "  git checkout geojaz/agent-sandbox-gke   # full stack: resource + Run + GKE"
echo "  python3 -m venv ~/.venv && source ~/.venv/bin/activate"
echo "  pip install -r requirements.txt   # includes kubernetes>=31.0.0"
