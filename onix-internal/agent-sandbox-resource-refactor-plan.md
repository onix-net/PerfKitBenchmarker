# Agent Sandbox: linux_package to resource refactor plan

## Context

PR #6718 is an open upstream WIP that bundles the whole agent_sandbox feature. Reviewer feedback (from the Google contact): `agent_sandbox.py` is currently a "linux_package" but should be a proper PKB resource, modeled on `wg_serving_inference_server`.

**Key finding:** `agent_sandbox.py` at `perfkitbenchmarker/linux_packages/agent_sandbox.py` is a linux_package in name and location only. It implements none of the linux_package interface (no `Install`/`AptInstall`/`YumInstall`). It already imports from `perfkitbenchmarker.resources.container_service` (`kubectl`, `kubernetes_commands`). It is cluster domain logic filed in the wrong place.

**Reference pattern that already exists in-tree:** `wg_serving_inference_server`. It is an aspect on `KubernetesCluster` (`cluster.inference_server`), constructed in the cluster `__init__`, lifecycled via the cluster's create/delete dependency hooks, and discovered by an auto-registry keyed on a type attribute (`INFERENCE_SERVER_TYPE`), not `CLOUD`.

## Decisions (settled)

1. **Attach point:** the agent sandbox is an aspect on `container_cluster` (`KubernetesCluster`), mirroring `inference_server`. NOT a direct attribute on `benchmark_spec`. Rationale: a sandbox cannot exist without a cluster; the cluster already provides construction and lifecycle wiring; hanging it on `benchmark_spec` would duplicate that. The circular-dependency concern the reviewer raised is solved by the base/factory split (see below).

2. **Discovery axis:** `REQUIRED_ATTRS = ['SANDBOX_TYPE']` (values like `'oss'`, later `'gke-managed'`), mirroring `INFERENCE_SERVER_TYPE`. This is what enables the OSS-vs-GKE-managed split.

3. **Standalone fixes land first,** as independent small PRs. The two that matter: event-poller `field_selector` filtering, and the AWS `fileb://` key-pair fix. The uncommitted kubeconfig-guard change in `kubernetes_cluster.py` is a local teardown helper and will be stashed, not sent upstream.

## Circular dependency: how it's avoided

The `inference_server` pattern already solves this. Three edges:

- **`perfkitbenchmarker/resources/agent_sandbox.py`:** base class `BaseAgentSandbox` + factory `GetAgentSandbox(spec, cluster)`. Imports `kubernetes_cluster` only under `TYPE_CHECKING` (type hints only).
- **`perfkitbenchmarker/resources/container_service/kubernetes_cluster.py`:** imports the BASE `agent_sandbox` module and constructs `self.agent_sandbox` in `__init__`. This is the cloud-agnostic "root" change (no GKE/EKS).
- **`perfkitbenchmarker/resources/kubernetes/oss_agent_sandbox.py`:** the concrete OSS impl. Imports `kubernetes_cluster` for real.

No cycle: the cluster-to-sandbox edge only ever touches the BASE module; the sandbox-to-cluster edge lives only in the concrete subclass; the auto-registry connects them.

## Proposed file layout

```
resources/agent_sandbox.py            # BaseAgentSandbox + GetAgentSandbox factory
resources/agent_sandbox_spec.py       # BaseAgentSandboxConfigSpec
resources/kubernetes/
  oss_agent_sandbox.py                # OSS impl (current install_stack logic moves here, into _Create/_Delete)
  oss_agent_sandbox_spec.py           # OSS spec
# + an agent_sandbox field decoder added to the container-cluster spec,
#   so it nests under container_cluster: in the benchmark YAML (like inference_server:)
```

## PR sequence

| # | Label | Commits / scope | Dependencies |
|---|-------|-----------------|--------------|
| 0a | Standalone: event-poller fix | `278120b69` field_selector filtering. Performance fix. | None |
| 0b | Standalone: AWS key-pair fix | `c479fb726` fileb:// key-pair import. | None |
| 1 | Skeleton | New files with class/spec/registration wired, stub bodies. The Google contact wants to mirror this internally for BUILD-file changes and offered to push the skeleton themselves. **Action: coordinate on who sends the skeleton before opening it.** | 0a, 0b landed |
| 2 | Resource + root wiring | Move `install_stack` logic into OSS resource's `_Create`/`_Delete`; add `agent_sandbox` decoder to container-cluster spec; construct `self.agent_sandbox` in base `KubernetesCluster`. Benchmark is a stub config spec proving the resource builds and registers. No GKE/EKS changes. | 1 |
| 3 | Fleshed-out benchmark | `agent_sandbox_loadgen.py` + `agent_sandbox_metrics.py` + real `Run` logic. | 2 |
| 4 | GKE | `085776433` (private nodes, DNS endpoint, Dataplane V2, cost allocation, monitoring, `max_pods_per_node`, nodepool labels/taints). First reproducible/runnable benchmark. | 3 |
| 5 | EKS | `080e92e65` (nodepool `node_labels` and `node_taints` applied to node groups). | 3 |
| 6 | AKS | New work, not yet started. | 3 |

Note: the uncommitted kubeconfig guard is stashed, not shipped.

## Risks and open items

- **gVisor install is OSS-specific.** The gvisor-installer DaemonSet + RuntimeClass logic belongs in the OSS subclass, not the base. A GKE-managed sandbox would provision the runtime differently. Keep the base abstract about "ensure the runtime class exists."

- **Flags vs spec.** About 30 `agent_sandbox_*` flags currently live in the benchmark file. Controller-tuning and stack flags should migrate into the spec decoders (config-driven, like `wg_serving`). Load-shape flags (`qps`, `duration`, `concurrency`) can stay as benchmark flags. Decide the exact cut line during PR 2/3.

- **Coordination.** The contact may push the skeleton (PR 1) internally first for BUILD files. Sync on ownership before opening PR 1.

## Next step

Send PR 0a and PR 0b (the two standalone fixes), then sync with the contact on who owns the skeleton PR.
