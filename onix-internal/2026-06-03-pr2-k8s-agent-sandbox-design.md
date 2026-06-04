# PR 2: Kubernetes agent sandbox resource - design

## Context

- Part of the agent_sandbox refactor (see onix-internal/agent-sandbox-resource-refactor-plan.md). Upstream feedback on PR #6718 asked that agent_sandbox become a proper PKB resource modeled on the kubernetes inference server.
- PR 1 (GitHub #6730, branch geojaz/agent-sandbox-skeleton, approved) landed the structure only: BaseAgentSandbox resource + GetAgentSandbox factory; BaseAgentSandboxConfigSpec + AgentSandboxConfigDecoder; the concrete Kubernetes stubs K8sAgentSandbox / K8sAgentSandboxConfigSpec (in resources/kubernetes/k8s_agent_sandbox.py and k8s_agent_sandbox_spec.py); and KubernetesCluster wiring that constructs and lifecycles cluster.agent_sandbox. Registered under SANDBOX_TYPE 'Kubernetes' (DEFAULT_SANDBOX_TYPE).
- PR 2 fills in the Kubernetes implementation: port the install orchestration from the old linux_packages/agent_sandbox.py into K8sAgentSandbox._Create, make the config spec config-driven, add the data manifests, add a minimal benchmark stub, and focused unit tests.
- Based on upstream/master. Reviewable but not runnable end to end until GKE/EKS nodepool support lands (PR 4/5).

## Settled decisions

1. Attach point: aspect on container_cluster (wired in PR 1).
2. Config-driven spec now: stack and controller-tuning config move into K8sAgentSandboxConfigSpec decoder fields; the existing agent_sandbox_* flags are kept and bridged via _ApplyFlags so prior flag usage still works.
3. _Delete: documented no-op. The cluster is ephemeral and torn down right after, which reclaims everything. Faithful to the old linux_package, which had no teardown. Real teardown for static/reused clusters is deferred.
4. Benchmark: minimal stub. BENCHMARK_CONFIG with a container_cluster + agent_sandbox block, GetConfig/Prepare/Cleanup, and a Run that returns an empty list with a TODO pointing at PR 3.
5. Tests: focused unit tests (decode/construct + manifest tuning injection), kubectl/cluster mocked.
6. Install helpers: private methods on K8sAgentSandbox (read self.spec / self.cluster).

## Spec shape

```yaml
container_cluster:
  ...
  agent_sandbox:
    type: Kubernetes
    manifest_ref: <git sha>        # git ref for the upstream CRDs/RBAC/controller manifests
    namespace: default
    controller:                    # ControllerSpec sub-spec
      image: <url>
      claim_workers / sandbox_workers / warmpool_workers / warmpool_max_batch_size
      kube_api_qps / kube_api_burst
      enable_tracing / otel_endpoint
      leader_elect
      cpu_request / cpu_limit / memory_request / memory_limit
    sandbox_template:              # SandboxTemplateSpec sub-spec
      runtime_class: runsc
      image / command / args
      cpu_request / cpu_limit / memory_request / memory_limit
      env / service_account
      labels / annotations
      network_policy_management: Managed       # stub
      env_vars_injection_policy: Disallowed    # stub
      service: null                            # stub
    sandbox_warmpool:              # SandboxWarmPoolSpec sub-spec
      replicas: 0
```

Three nested sub-specs, each a decoded `configs.spec.BaseSpec`: ControllerSpec, SandboxTemplateSpec, SandboxWarmPoolSpec.

**Top-level fields:**

| Field | Notes |
|---|---|
| `type` | 'Kubernetes' (the default) |
| `manifest_ref` | Renamed from the old controller_ref. The git ref used to fetch the upstream CRDs, RBAC, and controller manifests. |
| `namespace` | Kubernetes namespace to install into. |

**controller block fields:**

| Field | Notes |
|---|---|
| `image` | Controller container image URL. |
| `claim_workers`, `sandbox_workers`, `warmpool_workers`, `warmpool_max_batch_size` | Worker concurrency tuning. |
| `kube_api_qps`, `kube_api_burst` | Kubernetes API client rate limits. |
| `enable_tracing`, `otel_endpoint` | OpenTelemetry tracing config. |
| `leader_elect` | Enable leader election. |
| `cpu_request`, `cpu_limit`, `memory_request`, `memory_limit` | Resource requests and limits for the controller pod. |

Future (not PR 2): a gke_managed toggle for a platform-managed controller.

**sandbox_template block** models the real SandboxTemplateSpec (https://agent-sandbox.sigs.k8s.io/docs/api/#sandboxtemplatespec), whose core is a podTemplate (a core/v1 PodSpec) plus template-level toggles.

Pod-shape fields rendered into podTemplate.spec / podTemplate.metadata:

| Field | Kubernetes mapping |
|---|---|
| `runtime_class` | runtimeClassName |
| `image`, `command`, `args` | Container image, command, args |
| `cpu_request`, `cpu_limit`, `memory_request`, `memory_limit` | resources.requests / resources.limits |
| `env` | env |
| `service_account` | serviceAccountName |
| `labels`, `annotations` | podTemplate.metadata |

Template-level toggles accepted and validated but stubbed (TODO until a benchmark needs them):

- `network_policy_management`: Managed/Unmanaged, default Managed.
- `env_vars_injection_policy`: Disallowed/Allowed/Overrides, default Disallowed.
- `service` (bool).
- `volume_claim_templates` is omitted for now.

**sandbox_warmpool block** models SandboxWarmPoolSpec: `replicas` (required, minimum 0). `update_strategy` is omitted for now. It references the template by the shared internal name.

**Naming:** the SandboxTemplate and SandboxWarmPool are created under one shared internal name (not user-configured), so the warmpool's sandboxTemplateRef always lines up. This replaces the old separate `template` and `warmpool` name parameters.

## Files

**New:**

- `perfkitbenchmarker/linux_benchmarks/agent_sandbox_benchmark.py`: minimal stub benchmark (config + GetConfig/Prepare/Cleanup; Run returns []).
- `perfkitbenchmarker/data/agent_sandbox/gvisor-installer/daemonset.yaml`, `install.sh`, `runtimeclass.yaml`.
- `perfkitbenchmarker/data/agent_sandbox/sandbox-template.yaml.j2` (extended to take runtime_class, image, command, args, resources, env, service_account, labels, annotations as template vars).
- `perfkitbenchmarker/data/agent_sandbox/sandbox-warmpool.yaml.j2`.
- `tests/resources/kubernetes/k8s_agent_sandbox_test.py`.

**Flesh out (from PR 1 stubs):**

- `perfkitbenchmarker/resources/kubernetes/k8s_agent_sandbox.py`: implement K8sAgentSandbox._Create (orchestration) and private install helper methods; _Delete is a no-op.
- `perfkitbenchmarker/resources/kubernetes/k8s_agent_sandbox_spec.py`: K8sAgentSandboxConfigSpec fields plus the nested ControllerSpec / SandboxTemplateSpec / SandboxWarmPoolSpec sub-specs, and _ApplyFlags bridging the existing agent_sandbox_* flags.

**Flags:** the stack and controller-tuning agent_sandbox_* flags are defined in the spec module and bridged in _ApplyFlags. Load-shape flags (PR 3, benchmark Run) and node-pool sizing flags (PR 4/5, cluster/provider) are out of scope for PR 2.

## Data flow

```
BENCHMARK_CONFIG yaml
  container_cluster.agent_sandbox  -> AgentSandboxConfigDecoder -> K8sAgentSandboxConfigSpec (type=Kubernetes)
KubernetesCluster.__init__         -> GetAgentSandbox(spec, self) -> K8sAgentSandbox
cluster.Create()                   -> agent_sandbox.Create() -> _Create() installs the stack
cluster.Delete()                   -> agent_sandbox.Delete() -> _Delete() (no-op)
```

The cluster-side wiring already exists from PR 1.

## _Create orchestration

1. Install gVisor: apply the installer DaemonSet and RuntimeClass, wait for the DaemonSet rollout.
2. Install the controller: download the CRDs, RBAC, and controller manifests at manifest_ref; inject the controller image, tuning args, and resource requests/limits into the deployment manifest; apply; wait for the CRDs to be established and the deployment to roll out.
3. Apply the SandboxTemplate, rendered from the sandbox_template config via the extended j2 template.
4. Install the SandboxWarmPool with the configured replicas (skip if replicas is 0).

_Create raises on failure; PKB's Create() wrapper handles retry and timeout. All Kubernetes calls go through the existing kubernetes_commands / kubectl helpers against the cluster's kubeconfig context.

## _Delete

Documented pass. Cluster teardown reclaims all installed resources.

## Testing

- Decode and registration: a config dict containing the agent_sandbox block decodes to a K8sAgentSandboxConfigSpec with controller, sandbox_template, and sandbox_warmpool populated, and GetAgentSandbox returns a K8sAgentSandbox.
- Manifest tuning injection: unit-test the pure deployment-manifest transformation (controller image override, tuning args, resource requests/limits), with kubectl and cluster calls mocked. This is the highest-value pure logic to pin down.

## Out of scope (later PRs)

- Run logic: load generator and metrics (PR 3).
- Node-pool sizing, gVisor-capable nodepools, and provider changes (GKE PR 4, EKS PR 5, AKS PR 6). This is what makes the benchmark actually runnable.
- Real teardown for static or reused clusters.
- Rendering the stubbed template-level toggles (network policy, env injection policy, service, volume claims).

## Risks

- Extending sandbox-template.yaml.j2 to parameterize the pod fields: the new template vars must default to the values currently baked into the template so existing behavior is preserved.
- _Create performs network I/O (downloads upstream manifests at manifest_ref). This matches the old linux_package behavior.

Next step: turn this into an implementation plan (writing-plans) once approved.
