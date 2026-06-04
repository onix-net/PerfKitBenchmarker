# PR 3: Agent sandbox load generator, metrics, and real Run - design

## Context

- Part of the agent_sandbox refactor (see onix-internal/agent-sandbox-resource-refactor-plan.md). PR 2 (#6732) landed the Kubernetes resource and a stub benchmark whose Run returns []. PR 3 makes Run real: it ports the host-side load generator and metrics from the original branch (geojaz/agent-sandbox-1) and wires them into the benchmark.
- The load generator creates SandboxClaim custom resources at a target QPS and measures the time from claim creation to a Ready status condition, using a single shared Kubernetes Watch stream (not per-claim polling) so it does not flood the apiserver under concurrency.
- Based on the PR 2 branch (geojaz/agent-sandbox-resource) since PR 3 stacks on it. Still not runnable on GKE until PR 4 adds the gVisor-capable nodepools.

## Settled decisions

1. Keep the `kubernetes` Python client and the shared Watch design. Adds `kubernetes>=31.0.0` to requirements.txt. The watch stream is core to measuring claim-to-Ready latency accurately at QPS; kubectl polling would regress fidelity.
2. Location: the loadgen and metrics modules live in perfkitbenchmarker/linux_benchmarks/ next to the benchmark (they are host-side benchmark driver and measurement logic, not cluster resources or installable packages).
3. Tests: focused unit tests on the pure and injectable surface (no live cluster).
4. Load-shape flags stay benchmark-level. Namespace, template/warmpool name, runtime class, and warmpool replicas are sourced from the resource spec (set in PR 2), not from benchmark flags.
5. Port the loadgen and metrics verbatim (minimal changes: module location, and sourcing namespace/name from the resource spec). Do not refactor the working watch-based design.

## Files

New (ported verbatim from geojaz/agent-sandbox-1, relocated to linux_benchmarks/):

- `perfkitbenchmarker/linux_benchmarks/agent_sandbox_loadgen.py`: host-side generator. Public surface: `RunShape`, `resolve_run_shape`, `ClaimRecord`, `WorkloadExecutor` / `NoopExecutor` / `StreamExecExecutor`, `ClaimDriver`, `LoadGenerator`. Main entry `LoadGenerator.run(shape)` returns a list of `ClaimRecord` in submission order. Uses kubernetes client (`CustomObjectsApi`, `CoreV1Api`, `watch.Watch`), thread pool sized `max_concurrent + 50`, 429 retries with backoff, connection-pool tuning, watch reconnect on 410 Gone.
- `perfkitbenchmarker/linux_benchmarks/agent_sandbox_metrics.py`: `build_samples(records, peak_concurrency, metadata)` returns `list[sample.Sample]`; plus `percentile` and `_peak_from_records`.

Modify:

- `perfkitbenchmarker/linux_benchmarks/agent_sandbox_benchmark.py`: replace the stub Run with the real Run; add the load-shape flags. Keep `GetConfig` / `Prepare` / `Cleanup`.
- `requirements.txt`: add `kubernetes>=31.0.0`.

## Metrics emitted (for reference)

All as PKB samples:

| Metric | Unit | Notes |
|---|---|---|
| `startup_time_p50`, `p90`, `p95`, `p99` | seconds | Percentile of claim-to-Ready time |
| `startup_time_max` | seconds | Max claim-to-Ready time |
| `exec_duration_s_*` | seconds | Only when `workload_duration > 0` |
| `total_lifecycle_s_*` | seconds | Only when `workload_duration > 0` |
| `submit_qps` | count/sec | |
| `completion_qps` | count/sec | |
| `peak_concurrency` | count | |
| `warm_served_fraction` | fraction | |
| `success_count` | count | |
| `error_count` | count | |
| `error_count_*` | count | Per error type |

## Key adaptation: source config from the resource, not dead flags

The old Run pulled namespace, template name, warmpool name, runtime class, and warmpool replicas from benchmark flags that no longer exist (PR 2 moved them into the spec and collapsed the template/warmpool name into one shared constant `_SANDBOX_NAME`). The new Run reads them from the resource:

```python
sandbox = benchmark_spec.container_cluster.agent_sandbox   # K8sAgentSandbox
spec = sandbox.spec
shape = agent_sandbox_loadgen.resolve_run_shape(
    qps=_QPS.value,
    duration=_DURATION.value if _TOTAL.value is None else None,
    total=_TOTAL.value)
driver = agent_sandbox_loadgen.ClaimDriver(
    namespace=spec.namespace,
    template_name=k8s_agent_sandbox._SANDBOX_NAME,
    warmpool_name=k8s_agent_sandbox._SANDBOX_NAME,
    claim_ttl_seconds=_CLAIM_TTL.value,
    max_concurrent=_MAX_CONCURRENT.value)
# ... optional StreamExecExecutor when workload_duration > 0 ...
generator = agent_sandbox_loadgen.LoadGenerator(
    driver, ready_timeout=_READY_TIMEOUT.value,
    max_concurrent=_MAX_CONCURRENT.value,
    workload_executor=executor, workload_duration=workload_duration)
records = generator.run(shape)
metadata = {
    'target_qps': shape.qps, 'duration': shape.duration,
    'total_claims': shape.total,
    'warmpool_replicas': spec.sandbox_warmpool.replicas,
    'runtime_class': spec.sandbox_template.runtime_class,
}
metadata.update(benchmark_spec.container_cluster.GetResourceMetadata())
return agent_sandbox_metrics.build_samples(
    records, generator.peak_concurrency, metadata)
```

Note: the exact private helper names for building the API clients (for the `StreamExecExecutor`) are whatever the ported loadgen defines; match the source.

## Flags (benchmark-level)

Define in `agent_sandbox_benchmark.py`:

| Flag | Type | Default | Notes |
|---|---|---|---|
| `agent_sandbox_qps` | float | 10.0 | |
| `agent_sandbox_duration` | float | 60.0 | |
| `agent_sandbox_total` | int | None | |
| `agent_sandbox_max_concurrent` | int | 200 | |
| `agent_sandbox_ready_timeout` | int | 180 | |
| `agent_sandbox_workload_duration` | int | 0 | |
| `agent_sandbox_claim_ttl_seconds` | int | 120 | |

Do NOT define `agent_sandbox_namespace` here. It is already defined in `k8s_agent_sandbox_spec.py` from PR 2; the benchmark reads namespace from the resource spec.

## Data flow

```
Run
  -> read load-shape flags + benchmark_spec.container_cluster.agent_sandbox.spec
  -> resolve_run_shape -> ClaimDriver + LoadGenerator
  -> LoadGenerator.run() (shared Watch stream) -> list[ClaimRecord]
  -> build_samples(records, peak_concurrency, metadata) -> list[sample.Sample]
```

The loadgen uses the active kubeconfig context that PKB set during cluster provisioning (`config.load_kube_config()`), the same context the install path uses.

## Testing (focused unit tests)

- Metrics: `build_samples` over a synthetic list of `ClaimRecord` asserts the percentile names/values, `peak_concurrency`, `warm_served_fraction`, and success/error counts. `percentile` and `_peak_from_records` tested directly.
- Loadgen pure: `resolve_run_shape` (any two of qps/duration/total resolve the third), `ClaimRecord` computed properties (`startup_time_s`, `exec_duration_s`, `total_lifecycle_s`).
- `LoadGenerator.run()`: driven by a FAKE `ClaimDriver` (in-memory, no kubernetes client) with injected clock and sleeper, asserts one record per claim in submission order and that `max_concurrent` is respected.
- Benchmark Run: with `agent_sandbox_loadgen` and `agent_sandbox_metrics` mocked, assert `Run` constructs the `ClaimDriver` with the spec-derived namespace and the shared `_SANDBOX_NAME`, and returns the samples `build_samples` produced.

## Out of scope (later PRs)

- GKE / EKS / AKS provider changes that make the benchmark actually provision and run (PR 4 onward). PR 3 makes Run real but it remains runnable only where the cluster already supports the sandbox nodepools.
- Any change to the resource lifecycle (`_Create` / `_Delete`) from PR 2.

## Risks

- New dependency: `kubernetes>=31.0.0`. Justified by the watch-based design; upstream reviewers may ask, the answer is the accuracy/load argument.
- The loadgen relies on the active kubeconfig context being set by PKB provisioning. This matches the existing install path, so no new assumption.
- Verbatim port: keep the watch reconnect, 429 retry, and connection-pool logic intact. Do not simplify them.

Next step: turn this into an implementation plan (writing-plans) once approved.
