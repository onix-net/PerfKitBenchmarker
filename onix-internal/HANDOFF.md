# Agent Sandbox refactor — handoff brief

Read this first. It's the state of the work, the conventions, the gotchas, and what's left. Companion design/plan docs are in this same `onix-internal/` folder (see "Reference docs" at the end).

## What this project is

Reshape the PerfKitBenchmarker `agent_sandbox` feature from a `linux_package` into a proper PKB **resource**, contributed **upstream** to `GoogleCloudPlatform/PerfKitBenchmarker`, decomposed into a sequence of small reviewable PRs. The benchmark measures agent-sandbox SandboxClaim provisioning latency (kubernetes-sigs/agent-sandbox) at a target QPS.

## Repos / remotes (this checkout)

- `upstream` = `GoogleCloudPlatform/PerfKitBenchmarker` (the public repo PRs target; base = `master`).
- `onix` = `onix-net/PerfKitBenchmarker` (public fork; **all our branches are pushed here**).
- `origin` = `onix-net/PerfKitBenchmarker-internal` (internal mirror; the contact mirrors BUILD-file changes here — NOT where our branches live).

## Workflow conventions (follow these)

- Branch off `upstream/master`. Push branches to **`onix`**. The **user reviews on onix and opens the PRs to upstream themselves** — do NOT run `gh pr create` unless told to. (Exception: earlier in the session the user explicitly asked me to open 0a/0b/1/2; default is still "push, don't open".)
- **Never** add a `Co-Authored-By` trailer to commits (breaks the user's CLA workflow).
- `onix-internal/` is kept **untracked / out of upstream PRs** (planning docs, run scripts, this handoff). It rides on a dedicated notes branch, not on code branches.
- The agent's prior approach: brainstorm -> writing-plans -> subagent-driven execution (two-stage review per task). Heavy delegation to subagents for I/O and implementation.

## PR sequence + status (as of handoff)

Merge order: **6730 -> 6732 -> PR3 -> PR4 -> PR5 -> PR6**. The code branches are a stack: `skeleton <- resource <- benchmark <- gke` (each based on the previous), so a fix at a lower branch requires rebasing the ones above it (and force-pushing).

| PR | Branch (on onix) | Tip | State |
|----|------------------|-----|-------|
| 0a | `geojaz/k8s-event-field-selector` | — | **#6728 open** (event poller field_selector) |
| 0b | `geojaz/aws-ec2-import-key-pair-fileb` | — | **#6729 open** (aws fileb key-pair) |
| 1 (skeleton) | `geojaz/agent-sandbox-skeleton` | `b0d7d1867` | **#6730** approved, but the approval was **dismissed by a rename force-push** — needs re-stamp, then merge |
| 2 (resource) | `geojaz/agent-sandbox-resource` | `7954cf91c` | **#6732 open** |
| 3 (loadgen+metrics+Run) | `geojaz/agent-sandbox-benchmark` | `4ddb36ca0` | pushed to onix, **NOT opened** (user holding) |
| 4 (GKE) | `geojaz/agent-sandbox-gke` | `c712fd1c9` | pushed to onix, **NOT opened**. This is the **runnable** branch (full stack). |
| 5 (EKS) | — | — | **TODO**: cherry-pick `080e92e65` from `geojaz/agent-sandbox-1` (nodepool node_labels/taints on EKS node groups) |
| 6 (AKS) | — | — | **TODO**: new work, not started |

Source of the original monolithic implementation: branch **`geojaz/agent-sandbox-1`** (everything bundled). PRs 2-6 port pieces of it into the resource structure. PR4 was a clean cherry-pick of `085776433` (its tip commit shows a Tuesday *author* date because cherry-pick preserves it — not stale).

## Key design decisions (details in the design docs)

- **Resource model:** `agent_sandbox` is an aspect on `KubernetesCluster` (`cluster.agent_sandbox`), mirroring `wg_serving_inference_server`. It **installs during the cluster's `Create()` (provision stage)**. The benchmark `Prepare` is a **no-op**.
- **Classes:** base `BaseAgentSandbox` / `BaseAgentSandboxConfigSpec` (generic, in `resources/`); concrete `K8sAgentSandbox` / `K8sAgentSandboxConfigSpec` (in `resources/kubernetes/k8s_agent_sandbox.py` + `k8s_agent_sandbox_spec.py`), registered under `SANDBOX_TYPE='Kubernetes'`. (A future GKE-managed variant becomes a `controller` flag, not a new SANDBOX_TYPE.)
- **Spec shape:** top-level `manifest_ref` (renamed from the old `controller_ref`), `namespace`; nested sub-specs `controller`, `sandbox_template` (models the real SandboxTemplateSpec; advanced fields are accepted-but-stubbed), `sandbox_warmpool` (replicas). The SandboxTemplate and SandboxWarmPool share ONE internal name (`_SANDBOX_NAME = 'agent-sandbox'`) — no separate `template`/`warmpool` name flags.
- **`_Delete`** is a documented no-op (cluster teardown reclaims everything).
- **Flag split:** load-shape flags (qps/duration/total/max_concurrent/ready_timeout/workload_duration/claim_ttl_seconds) are defined in the **benchmark**; stack/controller config + `manifest_ref`/`namespace` are in **`k8s_agent_sandbox_spec.py`** and bridged via `_ApplyFlags` (using the idiomatic `flag_values['x'].present`).
- **Load generator** uses the `kubernetes` Python client + a single shared Watch stream (added `kubernetes>=31.0.0` to requirements.txt). loadgen + metrics live in `linux_benchmarks/` next to the benchmark.

## Gotchas already discovered (don't relearn these)

1. **Registration (fixed, but watch for regressions):** the concrete resource module MUST import its concrete spec module, or the `agent_sandbox_*` flags and `K8sAgentSandboxConfigSpec` never register at runtime (a real `pkb.py` run fails at flag parsing / config decode, even though unit tests pass because they import the spec module directly). Fixed in `k8s_agent_sandbox.py` (it now imports `k8s_agent_sandbox_spec`, mirroring `wg_serving_inference_server.py:35`). **TODO: add a regression test** that imports ONLY the benchmark and asserts a spec flag is registered.
2. **Flag renames vs the old run scripts:** `agent_sandbox_controller_ref` -> `agent_sandbox_manifest_ref`; the `agent_sandbox_template` / `agent_sandbox_warmpool` name flags are **gone**; the node-pool sizing flags (`agent_sandbox_sandbox_pool_*`, `agent_sandbox_general_pool_*`) are **gone** — node count / max_pods are now config-driven (`container_cluster.nodepools.sandbox.vm_count` / `.max_pods_per_node` in `BENCHMARK_CONFIG`, or `--config_override`). The old `ehole.sh`/`run_*.sh` scripts pass removed flags and will error.
3. **run_stage semantics changed:** install moved prepare -> provision. So:
   - `--run_stage=provision` stands up the cluster AND installs the stack.
   - `--run_stage=run` re-runs the loadgen on an existing cluster (`prepare` is a no-op).
   - Changing **install-time** settings (controller image/tuning, warmpool replicas, manifest_ref, runtime_class) does NOT take effect on a re-run; it needs a fresh provision (which means recreate, since `Create()` has a `created` guard). **Open design option (user said not yet):** add a public `K8sAgentSandbox.Reapply()` and call it from the benchmark `Prepare` so `--run_stage=prepare,run` re-applies the idempotent `kubectl apply` install and restores fast iteration without recreating the cluster.

## How to run on GKE (from the runner VM)

Infra is already built (see `setup-pkb-runner.sh`): VPC `pkb-bench-net` + subnet (same name, `10.0.0.0/20`, Private Google Access), Cloud Router + NAT `pkb-bench-net-nat`, IAP-only SSH, runner VM `pkb-runner` (c4-standard-16, no public IP) in project `ehole-benchmark-temp-z8s8` / `us-central1-a`. PKB deploys ephemeral GKE clusters into this VPC.

Run from the `geojaz/agent-sandbox-gke` checkout, venv active:
```
python pkb.py --benchmarks=agent_sandbox --cloud=GCP \
  --run_stage=provision --run_uri=<uri> \
  --project=ehole-benchmark-temp-z8s8 --zone=us-central1-a \
  --gce_network_name=pkb-bench-net --gce_subnet_name=pkb-bench-net \
  --gke_enable_private_nodes=True --gke_enable_dns_access=True \
  --agent_sandbox_qps=10 --agent_sandbox_warmpool_replicas=150 \
  --agent_sandbox_workload_duration=20 \
  --agent_sandbox_controller_claim_workers=100 \
  --agent_sandbox_controller_sandbox_workers=70 \
  --agent_sandbox_controller_kube_api_burst=800 \
  --agent_sandbox_controller_warmpool_workers=10
```
Then `--run_stage=run --run_uri=<uri>` for the loadgen metrics; `--run_stage=teardown --run_uri=<uri>` when done. **Stop the VM when idle** (`gcloud compute instances stop pkb-runner ...`). Likely next snag on a real run: GKE control-plane reachability from the VPC (DNS endpoint vs private IP).

## What's left (to carry to completion)

1. Add the registration regression test (gotcha #1); push to the relevant branch (resource), rebase the stack.
2. Get a clean GKE run end-to-end (PR2+PR3+PR4 on the gke branch) and capture results.
3. Decide on the `Reapply()`-in-Prepare option (gotcha #3) for iteration ergonomics.
4. Open PR3 and PR4 (the user holds these — confirm before opening). Get #6730 re-stamped + merged so the stack can land in order.
5. **PR5 (EKS):** new branch off the benchmark/gke stack, cherry-pick `080e92e65`. **PR6 (AKS):** new work.
6. Keep `onix-internal/` out of the code PRs.

## Reference docs (this folder)

- `agent-sandbox-resource-refactor-plan.md` — the original PR-sequence plan + rationale.
- `2026-06-03-pr2-k8s-agent-sandbox-design.md` / `2026-06-03-pr3-loadgen-metrics-design.md` — design specs.
- `2026-06-03-pr2-k8s-agent-sandbox-plan.md` / `2026-06-03-pr3-loadgen-metrics-plan.md` — implementation plans.
- `setup-pkb-runner.sh` — the VPC + runner VM provisioning script (idempotent).
