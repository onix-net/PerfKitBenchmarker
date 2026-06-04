#!/usr/bin/env bash
#
# asb-bench.sh — local dev runner for the agent_sandbox PKB benchmark.
#
# NOT part of the upstream benchmark. Mirrors the v1 run.sh ergonomics:
# stand up ONE cluster (RUN_URI), run MANY tests against it, tear down once.
# Each `test` writes results + logs to its own dir so runs never overwrite.
#
# Verbs:
#   up        provision + prepare the cluster (once)
#   provision provision only (mint the PKB spec; no prepare)
#   prepare   re-run prepare only (change controller/warmpool knobs, no reprovision)
#   test      run the load generator once -> per-test output dir
#   down      cleanup + teardown the cluster
#   bench     provision,prepare,run,cleanup,teardown in one shot
#   claims    kubectl delete all sandboxclaims in the namespace (clear orphans)
#   summary   (re)generate summary.md from results.json (latest test, a TEST_ID, or --all)
#
# Any value below can be overridden via environment variable. Extra flags passed
# after the verb are forwarded to pkb.py (e.g. asb-bench.sh test --agent_sandbox_qps=20).
#
# Reuse an existing cluster (skip create/delete): set STATIC_CLUSTER to the GKE
# cluster name and RUN_URI to its run id, then provision once to mint the spec:
#   RUN_URI=202605311002 STATIC_CLUSTER=pkb-202605311002 ./asb-bench.sh provision
#   RUN_URI=202605311002 ./asb-bench.sh test
#
# Examples:
#   ./asb-bench.sh up
#   QPS=10 TOTAL=600 ./asb-bench.sh test
#   CLAIM_WORKERS=20 ./asb-bench.sh prepare        # retune controller, then:
#   QPS=15 ./asb-bench.sh test
#   ./asb-bench.sh down

set -uo pipefail

# ---------------- CONFIG (override via env) ----------------
# _ASB_DIR = the directory this script lives in (resolves symlinks). Other
# wrapper files (asb-summary.py) live alongside it.
_ASB_SELF="${BASH_SOURCE[0]}"
while [ -h "$_ASB_SELF" ]; do                      # resolve symlinks
  _ASB_DIR="$(cd -P "$(dirname "$_ASB_SELF")" >/dev/null 2>&1 && pwd)"
  _ASB_SELF="$(readlink "$_ASB_SELF")"
  [ "${_ASB_SELF#/}" = "$_ASB_SELF" ] && _ASB_SELF="$_ASB_DIR/$_ASB_SELF"
done
_ASB_DIR="$(cd -P "$(dirname "$_ASB_SELF")" >/dev/null 2>&1 && pwd)"

# PKB_DIR = repo root = nearest ancestor of this script that contains pkb.py.
# This lets the wrapper live at the repo root OR in a subdir (e.g. eh/) and
# still find pkb.py and the .venv. Override with PKB_DIR=... if needed.
_find_pkb_root() {
  local d="$_ASB_DIR"
  while [ "$d" != "/" ]; do
    [ -f "$d/pkb.py" ] && { printf '%s\n' "$d"; return 0; }
    d="$(dirname "$d")"
  done
  printf '%s\n' "$_ASB_DIR"   # fallback: script dir
}
PKB_DIR="${PKB_DIR:-$(_find_pkb_root)}"
PYTHON="${PYTHON:-.venv/bin/python}"        # relative to PKB_DIR
RUN_URI="${RUN_URI:-202605311400}"
CLOUD="${CLOUD:-GCP}"                # GCP (GKE) or AWS (EKS)
OUT_ROOT="${OUT_ROOT:-$HOME/asb-runs}"

PROJECT="${PROJECT:-ehole-benchmark-temp-z8s8}"
ZONE="${ZONE:-us-central1-a}"
OWNER="${OWNER:-eric_hole}"

# cluster topology
GENERAL_POOL_MACHINE_TYPE="${GENERAL_POOL_MACHINE_TYPE:-c4-standard-4}"
GENERAL_POOL_NODES="${GENERAL_POOL_NODES:-2}"
SANDBOX_POOL_MACHINE_TYPE="${SANDBOX_POOL_MACHINE_TYPE:-c4-standard-16}"
SANDBOX_POOL_NODES="${SANDBOX_POOL_NODES:-5}"
SANDBOX_POOL_MAX_PODS="${SANDBOX_POOL_MAX_PODS:-200}"

# AWS / EKS node pool sizing (used when CLOUD=AWS)
EKS_GENERAL_POOL_MACHINE_TYPE="${EKS_GENERAL_POOL_MACHINE_TYPE:-m8i.xlarge}"
EKS_GENERAL_POOL_NODES="${EKS_GENERAL_POOL_NODES:-2}"
EKS_SANDBOX_POOL_MACHINE_TYPE="${EKS_SANDBOX_POOL_MACHINE_TYPE:-m8i.4xlarge}"
EKS_SANDBOX_POOL_NODES="${EKS_SANDBOX_POOL_NODES:-4}"
AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ZONE="${AWS_ZONE:-us-east-1a}"

# controller
CONTROLLER_REF="${CONTROLLER_REF:-32c4f231a116f76eb707fe34510b8143d61268ae}"
# CONTROLLER_IMAGE="${CONTROLLER_IMAGE:-us-central1-docker.pkg.dev/ehole-benchmark-temp-z8s8/default/agent-sandbox-controller:combined-fix-20260528}"
CONTROLLER_IMAGE="${CONTROLLER_IMAGE:-us-central1-docker.pkg.dev/k8s-staging-images/agent-sandbox/agent-sandbox-controller:v20260527-v0.4.6-31-gd43447b-main}"
CLAIM_WORKERS="${CLAIM_WORKERS:-100}"
SANDBOX_WORKERS="${SANDBOX_WORKERS:-60}"
KUBE_API_BURST="${KUBE_API_BURST:-600}"
LEADER_ELECT="${LEADER_ELECT:-false}"
ENABLE_TRACING="${ENABLE_TRACING:-true}"
OTEL_ENDPOINT="${OTEL_ENDPOINT:-http://otel-collector.observability:4317}"
CONTROLLER_CPU_REQUEST="${CONTROLLER_CPU_REQUEST:-500m}"
CONTROLLER_CPU_LIMIT="${CONTROLLER_CPU_LIMIT:-2}"
CONTROLLER_MEM_REQUEST="${CONTROLLER_MEM_REQUEST:-256Mi}"
CONTROLLER_MEM_LIMIT="${CONTROLLER_MEM_LIMIT:-1Gi}"

# warmpool + template
WARMPOOL="${WARMPOOL:-python-sandbox-warmpool}"
WARMPOOL_REPLICAS="${WARMPOOL_REPLICAS:-150}"
TEMPLATE="${TEMPLATE:-python-runtime-template}"

# GKE provisioning
GKE_MONITORING_COMPONENTS="${GKE_MONITORING_COMPONENTS:-SYSTEM,API_SERVER,SCHEDULER,CONTROLLER_MANAGER,POD,DEPLOYMENT,STATEFULSET,DAEMONSET,HPA,STORAGE,CADVISOR,KUBELET}"

# load generator (test verb)
QPS="${QPS:-10}"
TOTAL="${TOTAL:-600}"
DURATION="${DURATION:-}"
MAX_CONCURRENT="${MAX_CONCURRENT:-}"
READY_TIMEOUT="${READY_TIMEOUT:-}"
NAMESPACE="${NAMESPACE:-default}"
WORKLOAD_DURATION="${WORKLOAD_DURATION:-20}"   # seconds to hold+exec each sandbox; 0 = pure provisioning
EXEC_CONTAINER="${EXEC_CONTAINER:-}"

# adopt an already-running cluster instead of provisioning. Set to the GKE
# cluster name (e.g. pkb-202605311002); empty = normal PKB-managed lifecycle.
# When set, create/delete are skipped (PKB user_managed) and a kubeconfig is
# derived for the load generator. RUN_URI must match the cluster (pkb-<RUN_URI>).
STATIC_CLUSTER="${STATIC_CLUSTER:-}"
STATIC_NETWORK="${STATIC_NETWORK:-pkb-network-${RUN_URI}}"  # existing network+subnet to adopt (PKB names it pkb-network-<run_uri>)
STATIC_KUBECONFIG="${STATIC_KUBECONFIG:-}"            # explicit kubeconfig; auto-derived if empty
GET_CREDENTIALS_FLAGS="${GET_CREDENTIALS_FLAGS:---dns-endpoint}"  # these clusters use the DNS control-plane endpoint

# ---------------- internals ----------------
RUN_DIR="/tmp/perfkitbenchmarker/runs/${RUN_URI}"

pkb() { ( cd "$PKB_DIR" && "$PYTHON" pkb.py --accept_licenses "$@" ); }

# Static-cluster mode. When STATIC_CLUSTER is set, adopt that already-running
# cluster instead of provisioning a new one. static_setup populates STATIC_FLAGS
# (forwarded to pkb) and exports KUBECONFIG so the load generator's in-Python
# load_kube_config() targets the cluster (PKB skips get-credentials for adopted
# clusters, so we supply the kubeconfig ourselves). Pass "with_override" when the
# run_stage includes provision, so PKB marks the cluster user_managed.
STATIC_FLAGS=()
ensure_static_kubeconfig() {
  # Echo a path to a usable kubeconfig for $STATIC_CLUSTER (stdout = path only).
  if [ -n "$STATIC_KUBECONFIG" ] && [ -s "$STATIC_KUBECONFIG" ]; then
    echo "$STATIC_KUBECONFIG"; return 0
  fi
  mkdir -p "$RUN_DIR"
  local kcfg="${RUN_DIR}/kubeconfig-asb"
  if [ ! -s "$kcfg" ]; then
    # Reuse a kubeconfig PKB already wrote into this run dir, else fetch one.
    local existing
    existing="$(ls "${RUN_DIR}"/kubeconfig[0-9]* 2>/dev/null | head -1)"
    if [ -n "$existing" ] && [ -s "$existing" ]; then
      cp -f "$existing" "$kcfg"
    else
      KUBECONFIG="$kcfg" gcloud container clusters get-credentials "$STATIC_CLUSTER" \
        --zone "$ZONE" --project "$PROJECT" $GET_CREDENTIALS_FLAGS >/dev/null 2>&1 \
        || { echo "!! get-credentials for $STATIC_CLUSTER failed (try GET_CREDENTIALS_FLAGS=)" >&2; return 1; }
    fi
  fi
  echo "$kcfg"
}
static_setup() {
  STATIC_FLAGS=()
  [ -n "$STATIC_CLUSTER" ] || return 0
  local kcfg
  kcfg="$(ensure_static_kubeconfig)" || return 1
  export KUBECONFIG="$kcfg"
  STATIC_FLAGS=(--kubeconfig="$kcfg")
  if [ "${1:-}" = "with_override" ]; then
    # static_cluster only adopts the cluster; the network is a separate top-level
    # resource. Reuse the existing network+subnet (auto-mode: subnet name ==
    # network name) so provision skips network/subnet/firewall creation.
    STATIC_FLAGS+=(
      --config_override=agent_sandbox.container_cluster.static_cluster="$STATIC_CLUSTER"
      --gce_network_name="$STATIC_NETWORK"
      --gce_subnet_name="$STATIC_NETWORK"
    )
    echo "==> reusing network: $STATIC_NETWORK"
  fi
  echo "==> static cluster: $STATIC_CLUSTER  (kubeconfig: $kcfg)"
}

# Capture the knobs used for this test so the run is reproducible and summary.md
# can embed them as its Invocation block (mirrors v1's env-var invocation).
write_invocation() {
  local out="$1" verb="${2:-test}"
  local kv=(
    "RUN_URI=$RUN_URI"
    "PROJECT=$PROJECT"
    "ZONE=$ZONE"
    "OWNER=$OWNER"
    "GENERAL_POOL_MACHINE_TYPE=$GENERAL_POOL_MACHINE_TYPE"
    "GENERAL_POOL_NODES=$GENERAL_POOL_NODES"
    "SANDBOX_POOL_MACHINE_TYPE=$SANDBOX_POOL_MACHINE_TYPE"
    "SANDBOX_POOL_NODES=$SANDBOX_POOL_NODES"
    "SANDBOX_POOL_MAX_PODS=$SANDBOX_POOL_MAX_PODS"
    "CONTROLLER_REF=$CONTROLLER_REF"
    "CONTROLLER_IMAGE=$CONTROLLER_IMAGE"
    "CLAIM_WORKERS=$CLAIM_WORKERS"
    "SANDBOX_WORKERS=$SANDBOX_WORKERS"
    "KUBE_API_BURST=$KUBE_API_BURST"
    "LEADER_ELECT=$LEADER_ELECT"
    "ENABLE_TRACING=$ENABLE_TRACING"
    "OTEL_ENDPOINT=$OTEL_ENDPOINT"
    "CONTROLLER_CPU_REQUEST=$CONTROLLER_CPU_REQUEST"
    "CONTROLLER_CPU_LIMIT=$CONTROLLER_CPU_LIMIT"
    "CONTROLLER_MEM_REQUEST=$CONTROLLER_MEM_REQUEST"
    "CONTROLLER_MEM_LIMIT=$CONTROLLER_MEM_LIMIT"
    "WARMPOOL=$WARMPOOL"
    "WARMPOOL_REPLICAS=$WARMPOOL_REPLICAS"
    "TEMPLATE=$TEMPLATE"
    "QPS=$QPS"
    "TOTAL=$TOTAL"
    "WORKLOAD_DURATION=$WORKLOAD_DURATION"
    "NAMESPACE=$NAMESPACE"
    "TEST_ID=$TEST_ID"
  )
  [ -n "$DURATION" ]       && kv+=("DURATION=$DURATION")
  [ -n "$MAX_CONCURRENT" ] && kv+=("MAX_CONCURRENT=$MAX_CONCURRENT")
  [ -n "$READY_TIMEOUT" ]  && kv+=("READY_TIMEOUT=$READY_TIMEOUT")
  [ -n "$STATIC_CLUSTER" ] && kv+=("STATIC_CLUSTER=$STATIC_CLUSTER")
  {
    printf '%s \\\n' "${kv[@]}"
    printf './asb-bench.sh %s\n' "$verb"
  } > "$out/invocation.sh"
}

# Render summary.md from the per-test results.json (v1-style).
gen_summary() {
  local out="$1"
  [ -f "$out/results.json" ] || return 0
  if ( cd "$PKB_DIR" && "$PYTHON" "$_ASB_DIR/asb-summary.py" "$out" ); then
    echo "==> wrote: $out/summary.md"
  else
    echo "!! summary generation failed (results.json intact)" >&2
  fi
}

usage() {
  grep '^#' "$0" | sed -e 's/^# \{0,1\}//' | sed -n '2,40p'
  exit 1
}

cluster_flags=(
  --agent_sandbox_general_pool_machine_type="$GENERAL_POOL_MACHINE_TYPE"
  --agent_sandbox_general_pool_nodes="$GENERAL_POOL_NODES"
  --agent_sandbox_sandbox_pool_machine_type="$SANDBOX_POOL_MACHINE_TYPE"
  --agent_sandbox_sandbox_pool_nodes="$SANDBOX_POOL_NODES"
  --agent_sandbox_sandbox_pool_max_pods_per_node="$SANDBOX_POOL_MAX_PODS"
  --gke_enable_private_nodes=true
  --gke_enable_dns_access=true
  --gke_enable_ip_access=false
  --gke_enable_dataplane_v2=true
  --gke_enable_managed_prometheus=true
  --gke_enable_cost_allocation=true
  --gke_monitoring_components="$GKE_MONITORING_COMPONENTS"
)

eks_cluster_flags=(
  --benchmarks=agent_sandbox_eks
  --agent_sandbox_eks_controller_ref="$CONTROLLER_REF"
  --agent_sandbox_eks_controller_image="$CONTROLLER_IMAGE"
  --agent_sandbox_eks_general_pool_machine_type="$EKS_GENERAL_POOL_MACHINE_TYPE"
  --agent_sandbox_eks_general_pool_nodes="$EKS_GENERAL_POOL_NODES"
  --agent_sandbox_eks_sandbox_pool_machine_type="$EKS_SANDBOX_POOL_MACHINE_TYPE"
  --agent_sandbox_eks_sandbox_pool_nodes="$EKS_SANDBOX_POOL_NODES"
  --agent_sandbox_eks_controller_claim_workers="$CLAIM_WORKERS"
  --agent_sandbox_eks_controller_sandbox_workers="$SANDBOX_WORKERS"
  --agent_sandbox_eks_controller_kube_api_burst="$KUBE_API_BURST"
  --agent_sandbox_eks_controller_leader_elect="$LEADER_ELECT"
  --agent_sandbox_eks_controller_enable_tracing="$ENABLE_TRACING"
  --agent_sandbox_eks_controller_otel_endpoint="$OTEL_ENDPOINT"
  --agent_sandbox_eks_controller_cpu_request="$CONTROLLER_CPU_REQUEST"
  --agent_sandbox_eks_controller_cpu_limit="$CONTROLLER_CPU_LIMIT"
  --agent_sandbox_eks_controller_memory_request="$CONTROLLER_MEM_REQUEST"
  --agent_sandbox_eks_controller_memory_limit="$CONTROLLER_MEM_LIMIT"
  --agent_sandbox_eks_warmpool="$WARMPOOL"
  --agent_sandbox_eks_warmpool_replicas="$WARMPOOL_REPLICAS"
  --agent_sandbox_eks_workload_duration="$WORKLOAD_DURATION"
)

eks_loadgen_flags=(
  --agent_sandbox_eks_qps="$QPS"
  --agent_sandbox_eks_warmpool="$WARMPOOL"
  --agent_sandbox_eks_template="$TEMPLATE"
  --agent_sandbox_eks_workload_duration="$WORKLOAD_DURATION"
)

controller_flags=(
  --agent_sandbox_controller_ref="$CONTROLLER_REF"
  --agent_sandbox_controller_image="$CONTROLLER_IMAGE"
  --agent_sandbox_controller_claim_workers="$CLAIM_WORKERS"
  --agent_sandbox_controller_sandbox_workers="$SANDBOX_WORKERS"
  --agent_sandbox_controller_kube_api_burst="$KUBE_API_BURST"
  --agent_sandbox_controller_leader_elect="$LEADER_ELECT"
  --agent_sandbox_controller_enable_tracing="$ENABLE_TRACING"
  --agent_sandbox_controller_otel_endpoint="$OTEL_ENDPOINT"
  --agent_sandbox_controller_cpu_request="$CONTROLLER_CPU_REQUEST"
  --agent_sandbox_controller_cpu_limit="$CONTROLLER_CPU_LIMIT"
  --agent_sandbox_controller_memory_request="$CONTROLLER_MEM_REQUEST"
  --agent_sandbox_controller_memory_limit="$CONTROLLER_MEM_LIMIT"
)

warmpool_flags=(
  --agent_sandbox_warmpool="$WARMPOOL"
  --agent_sandbox_warmpool_replicas="$WARMPOOL_REPLICAS"
)

loadgen_flags=(
  --agent_sandbox_qps="$QPS"
  --agent_sandbox_warmpool="$WARMPOOL"
  --agent_sandbox_template="$TEMPLATE"
  --agent_sandbox_workload_duration="$WORKLOAD_DURATION"
)
[ -n "$TOTAL" ]          && loadgen_flags+=(--agent_sandbox_total="$TOTAL")
[ -n "$DURATION" ]       && loadgen_flags+=(--agent_sandbox_duration="$DURATION")
[ -n "$MAX_CONCURRENT" ] && loadgen_flags+=(--agent_sandbox_max_concurrent="$MAX_CONCURRENT")
[ -n "$READY_TIMEOUT" ]  && loadgen_flags+=(--agent_sandbox_ready_timeout="$READY_TIMEOUT")
[ -n "$NAMESPACE" ]      && loadgen_flags+=(--agent_sandbox_namespace="$NAMESPACE")
[ -n "$EXEC_CONTAINER" ] && loadgen_flags+=(--agent_sandbox_exec_container="$EXEC_CONTAINER")

cmd="${1:-}"; [ -n "$cmd" ] && shift || true
[ -z "$cmd" ] && usage

case "$cmd" in
  up)
    static_setup with_override || exit 1
    if [ "$CLOUD" = "AWS" ]; then
      pkb --run_stage=provision,prepare --run_uri="$RUN_URI" \
        --cloud=AWS --zones="$AWS_ZONE" --owner="$OWNER" \
        "${eks_cluster_flags[@]}" \
        ${STATIC_FLAGS[@]+"${STATIC_FLAGS[@]}"} "$@"
    else
      pkb --benchmarks=agent_sandbox --run_stage=provision,prepare --run_uri="$RUN_URI" \
        --cloud=GCP --project="$PROJECT" --zone="$ZONE" --owner="$OWNER" \
        "${cluster_flags[@]}" "${controller_flags[@]}" "${warmpool_flags[@]}" \
        ${STATIC_FLAGS[@]+"${STATIC_FLAGS[@]}"} "$@"
    fi
    ;;
  provision)
    static_setup with_override || exit 1
    if [ "$CLOUD" = "AWS" ]; then
      pkb --run_stage=provision --run_uri="$RUN_URI" \
        --cloud=AWS --zones="$AWS_ZONE" --owner="$OWNER" \
        "${eks_cluster_flags[@]}" \
        ${STATIC_FLAGS[@]+"${STATIC_FLAGS[@]}"} "$@"
    else
      pkb --benchmarks=agent_sandbox --run_stage=provision --run_uri="$RUN_URI" \
        --cloud=GCP --project="$PROJECT" --zone="$ZONE" --owner="$OWNER" \
        "${cluster_flags[@]}" \
        ${STATIC_FLAGS[@]+"${STATIC_FLAGS[@]}"} "$@"
    fi
    ;;
  prepare)
    static_setup || exit 1
    pkb --benchmarks=agent_sandbox --run_stage=prepare --run_uri="$RUN_URI" --owner="$OWNER" \
      "${controller_flags[@]}" "${warmpool_flags[@]}" \
      ${STATIC_FLAGS[@]+"${STATIC_FLAGS[@]}"} "$@"
    ;;
  test)
    TEST_ID="${TEST_ID:-$(date +%Y%m%d-%H%M%S)}"
    OUT="${OUT_ROOT}/${RUN_URI}/${TEST_ID}"
    mkdir -p "$OUT"
    echo "==> test_id=$TEST_ID  out=$OUT"
    static_setup || exit 1
    write_invocation "$OUT" test
    if [ "$CLOUD" = "AWS" ]; then
      pkb --run_stage=run --run_uri="$RUN_URI" --owner="$OWNER" \
        "${eks_loadgen_flags[@]}" \
        ${STATIC_FLAGS[@]+"${STATIC_FLAGS[@]}"} \
        --json_path="$OUT/results.json" \
        --metadata=test_id:"$TEST_ID" \
        "$@" 2>&1 | tee "$OUT/console.log"
    else
      pkb --benchmarks=agent_sandbox --run_stage=run --run_uri="$RUN_URI" --owner="$OWNER" \
        "${loadgen_flags[@]}" \
        ${STATIC_FLAGS[@]+"${STATIC_FLAGS[@]}"} \
        --json_path="$OUT/results.json" \
        --metadata=test_id:"$TEST_ID" \
        "$@" 2>&1 | tee "$OUT/console.log"
    fi
    cp -f "${RUN_DIR}/pkb.log" "$OUT/pkb.log" 2>/dev/null || true
    echo "==> wrote: $OUT/{results.json,console.log,pkb.log}"
    gen_summary "$OUT"
    ;;
  down)
    pkb --benchmarks=agent_sandbox --run_stage=teardown --run_uri="$RUN_URI" --owner="$OWNER" "$@"
    ;;
  bench)
    TEST_ID="${TEST_ID:-$(date +%Y%m%d-%H%M%S)}"
    OUT="${OUT_ROOT}/${RUN_URI}/${TEST_ID}"
    mkdir -p "$OUT"
    static_setup with_override || exit 1
    write_invocation "$OUT" bench
    pkb --benchmarks=agent_sandbox --run_stage=provision,prepare,run,cleanup,teardown --run_uri="$RUN_URI" \
      --cloud=GCP --project="$PROJECT" --zone="$ZONE" --owner="$OWNER" \
      "${cluster_flags[@]}" "${controller_flags[@]}" "${warmpool_flags[@]}" "${loadgen_flags[@]}" \
      ${STATIC_FLAGS[@]+"${STATIC_FLAGS[@]}"} \
      --json_path="$OUT/results.json" --metadata=test_id:"$TEST_ID" \
      "$@" 2>&1 | tee "$OUT/console.log"
    cp -f "${RUN_DIR}/pkb.log" "$OUT/pkb.log" 2>/dev/null || true
    gen_summary "$OUT"
    ;;
  claims)
    ( cd "$PKB_DIR" && kubectl delete sandboxclaims --all -n "$NAMESPACE" )
    ;;
  summary)
    target="${1:-}"
    if [ "$target" = "--all" ]; then
      ( cd "$PKB_DIR" && "$PYTHON" "$_ASB_DIR/asb-summary.py" --all "${OUT_ROOT}/${RUN_URI}" )
    elif [ -n "$target" ]; then
      ( cd "$PKB_DIR" && "$PYTHON" "$_ASB_DIR/asb-summary.py" "${OUT_ROOT}/${RUN_URI}/${target}" )
    else
      latest=$(ls -1d "${OUT_ROOT}/${RUN_URI}"/*/ 2>/dev/null | sort | tail -1)
      [ -z "$latest" ] && { echo "no test dirs under ${OUT_ROOT}/${RUN_URI}" >&2; exit 1; }
      ( cd "$PKB_DIR" && "$PYTHON" "$_ASB_DIR/asb-summary.py" "$latest" )
    fi
    ;;
  *)
    echo "unknown verb: $cmd" >&2
    usage
    ;;
esac
