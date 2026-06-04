# PR 3: Agent sandbox load generator, metrics, and real Run — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the agent_sandbox benchmark's `Run` real by porting the host-side load generator and metrics into `linux_benchmarks/` and wiring them up, sourcing namespace/name/shape config from the PR 2 resource spec.

**Architecture:** Two verbatim-ported modules (`agent_sandbox_loadgen.py`, `agent_sandbox_metrics.py`) live next to `agent_sandbox_benchmark.py`. `Run` reads the load-shape flags and the `K8sAgentSandbox` resource spec (`benchmark_spec.container_cluster.agent_sandbox.spec`), drives `LoadGenerator.run()` (a shared Kubernetes Watch stream), and turns the resulting `ClaimRecord`s into PKB samples via `build_samples`.

**Tech Stack:** Python, `kubernetes` Python client (watch stream), absl flags, PKB `sample`, `pkb_common_test_case` + `mock`/fakes.

**Branch:** Cut `geojaz/agent-sandbox-benchmark` from `geojaz/agent-sandbox-resource` (PR 2's branch; PR 3 stacks on it). Push to `onix` for review when done; do not open the PR until told.

**Source of truth for the ports:** `geojaz/agent-sandbox-1`. The loadgen and metrics modules are self-contained (they take namespace/name as arguments and define no flags), so they copy with no edits. Only the benchmark's `Run` is adapted (Task 4). Read the copied source before writing each test and assert its REAL behavior.

---

## File structure

| File | Responsibility | Action |
|------|----------------|--------|
| `requirements.txt` | declare the `kubernetes` client dependency | Modify (1 line) |
| `perfkitbenchmarker/linux_benchmarks/agent_sandbox_loadgen.py` | host-side SandboxClaim load generator (watch-based) | Create (verbatim copy) |
| `perfkitbenchmarker/linux_benchmarks/agent_sandbox_metrics.py` | turn `ClaimRecord`s into PKB samples | Create (verbatim copy) |
| `perfkitbenchmarker/linux_benchmarks/agent_sandbox_benchmark.py` | real `Run` + load-shape flags | Modify |
| `tests/linux_benchmarks/agent_sandbox_loadgen_test.py` | unit tests: shape resolution, record properties, run() with a fake driver | Create |
| `tests/linux_benchmarks/agent_sandbox_metrics_test.py` | unit tests: build_samples, percentile, peak | Create |
| `tests/resources/kubernetes/k8s_agent_sandbox_test.py` | add a `Run` wiring test (mocked loadgen/metrics) | Modify |

**Out of scope:** provider changes (GKE/EKS/AKS, PR 4+), and any change to the resource lifecycle from PR 2.

---

## Task 1: Declare the kubernetes client dependency

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Check how the branch declared it and add the same line**

```bash
git show geojaz/agent-sandbox-1:requirements.txt | grep -i kubernetes
```
Expected: a line like `kubernetes>=31.0.0`.

- [ ] **Step 2: Add that exact line to requirements.txt**

Append `kubernetes>=31.0.0` to `requirements.txt` (match the version the branch used from Step 1; keep the file's existing ordering style — if the file is alphabetized, insert in order, otherwise append).

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "agent_sandbox: add kubernetes python client dependency"
```

---

## Task 2: Port the load generator + tests

**Files:**
- Create: `perfkitbenchmarker/linux_benchmarks/agent_sandbox_loadgen.py`
- Test: `tests/linux_benchmarks/agent_sandbox_loadgen_test.py`

- [ ] **Step 1: Copy the loadgen module verbatim into linux_benchmarks/**

```bash
git show geojaz/agent-sandbox-1:perfkitbenchmarker/linux_packages/agent_sandbox_loadgen.py \
  > perfkitbenchmarker/linux_benchmarks/agent_sandbox_loadgen.py
```

- [ ] **Step 2: Verify it imports cleanly (no edits expected)**

```bash
/Users/eric.hole/dev/onix/PerfKitBenchmarker-internal/.venv/bin/python -c "from perfkitbenchmarker.linux_benchmarks import agent_sandbox_loadgen; print('import OK')"
```
Expected: `import OK`. The module imports the `kubernetes` client and stdlib only; it defines no flags and references no sibling agent_sandbox module, so no edits should be needed. If the import fails because `kubernetes` is not installed in the venv, install it (`/Users/eric.hole/dev/onix/PerfKitBenchmarker-internal/.venv/bin/pip install 'kubernetes>=31.0.0'`) and retry. If it fails for any OTHER reason (a real cross-module reference), report it before editing.

- [ ] **Step 3: Read the copied module, then write characterization tests**

Read `agent_sandbox_loadgen.py` to confirm the exact signatures and formulas, then create `tests/linux_benchmarks/agent_sandbox_loadgen_test.py`. Use this as a template and ADAPT each assertion to the real source (field names, the exact `resolve_run_shape` rules, and the `ClaimRecord` property formulas):

```python
"""Tests for the agent_sandbox load generator."""

import unittest

from perfkitbenchmarker.linux_benchmarks import agent_sandbox_loadgen
from tests import pkb_common_test_case


class ResolveRunShapeTest(pkb_common_test_case.PkbCommonTestCase):

  def testQpsAndDurationDeriveTotal(self):
    shape = agent_sandbox_loadgen.resolve_run_shape(qps=10.0, duration=5.0)
    self.assertEqual(shape.qps, 10.0)
    self.assertEqual(shape.duration, 5.0)
    self.assertEqual(shape.total, 50)

  def testQpsAndTotalDeriveDuration(self):
    shape = agent_sandbox_loadgen.resolve_run_shape(qps=10.0, total=100)
    self.assertEqual(shape.total, 100)
    self.assertEqual(shape.duration, 10.0)


class ClaimRecordTest(pkb_common_test_case.PkbCommonTestCase):

  def testStartupTime(self):
    rec = agent_sandbox_loadgen.ClaimRecord(name='c0')
    rec.requested_at = 100.0
    rec.ready_at = 102.5
    self.assertAlmostEqual(rec.startup_time_s, 2.5)

  def testLifecycleAndExecDurations(self):
    rec = agent_sandbox_loadgen.ClaimRecord(name='c1')
    rec.requested_at = 100.0
    rec.exec_started_at = 103.0
    rec.exec_completed_at = 108.0
    rec.released_at = 110.0
    self.assertAlmostEqual(rec.exec_duration_s, 5.0)
    self.assertAlmostEqual(rec.total_lifecycle_s, 10.0)


if __name__ == '__main__':
  unittest.main()
```

If `ClaimRecord`'s constructor requires positional fields rather than attribute assignment, build it the way the dataclass actually requires. If `startup_time_s` returns `None` when `ready_at` is unset, add a test for that None path too. Match the real code.

- [ ] **Step 4: (Best effort) add a LoadGenerator.run() test with a fake driver**

Read how `LoadGenerator.run(shape, clock=, sleeper=)` uses its `driver` (which methods it calls, how readiness is signaled — e.g. via maps the watch thread populates). Write a minimal in-memory fake driver that satisfies that interface and marks each created claim Ready immediately, then assert `run()` returns one `ClaimRecord` per claim in submission order with `ready_at` set. Inject `clock` and `sleeper` (a no-op sleeper and a monotonic counter clock) so the test is fast and deterministic.

If the `LoadGenerator`/`ClaimDriver` coupling makes a faithful fake impractical within this task, STOP and report DONE_WITH_CONCERNS describing the coupling, rather than writing a brittle or trivial test. The pure tests in Step 3 are the priority.

- [ ] **Step 5: Run the tests**

```bash
/Users/eric.hole/dev/onix/PerfKitBenchmarker-internal/.venv/bin/python -m pytest tests/linux_benchmarks/agent_sandbox_loadgen_test.py -v
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add perfkitbenchmarker/linux_benchmarks/agent_sandbox_loadgen.py tests/linux_benchmarks/agent_sandbox_loadgen_test.py
git commit -m "agent_sandbox: port the SandboxClaim load generator"
```

---

## Task 3: Port the metrics module + tests

**Files:**
- Create: `perfkitbenchmarker/linux_benchmarks/agent_sandbox_metrics.py`
- Test: `tests/linux_benchmarks/agent_sandbox_metrics_test.py`

- [ ] **Step 1: Copy the metrics module verbatim**

```bash
git show geojaz/agent-sandbox-1:perfkitbenchmarker/linux_packages/agent_sandbox_metrics.py \
  > perfkitbenchmarker/linux_benchmarks/agent_sandbox_metrics.py
```

- [ ] **Step 2: Verify import**

```bash
/Users/eric.hole/dev/onix/PerfKitBenchmarker-internal/.venv/bin/python -c "from perfkitbenchmarker.linux_benchmarks import agent_sandbox_metrics; print('import OK')"
```
Expected: `import OK`. It imports `perfkitbenchmarker.sample` and the loadgen module (for the `ClaimRecord` type, if at all) plus stdlib. If it imports the loadgen via the OLD `linux_packages` path, update that one import to `from perfkitbenchmarker.linux_benchmarks import agent_sandbox_loadgen`. Report if you had to change anything.

- [ ] **Step 3: Read the copied module, then write tests**

Read `agent_sandbox_metrics.py` for exact behavior, then create `tests/linux_benchmarks/agent_sandbox_metrics_test.py`. Template (adapt metric names/values to the real source):

```python
"""Tests for the agent_sandbox metrics."""

import unittest

from perfkitbenchmarker.linux_benchmarks import agent_sandbox_loadgen
from perfkitbenchmarker.linux_benchmarks import agent_sandbox_metrics
from tests import pkb_common_test_case


class PercentileTest(pkb_common_test_case.PkbCommonTestCase):

  def testPercentile(self):
    self.assertAlmostEqual(
        agent_sandbox_metrics.percentile([1.0, 2.0, 3.0, 4.0], 50), 2.5)


class BuildSamplesTest(pkb_common_test_case.PkbCommonTestCase):

  def _record(self, name, requested_at, ready_at):
    rec = agent_sandbox_loadgen.ClaimRecord(name=name)
    rec.requested_at = requested_at
    rec.ready_at = ready_at
    return rec

  def testStartupSamplesAndCounts(self):
    records = [
        self._record('c0', 100.0, 101.0),
        self._record('c1', 100.0, 103.0),
    ]
    samples = agent_sandbox_metrics.build_samples(
        records, peak_concurrency=2, metadata={'target_qps': 10.0})
    by_name = {s.metric: s for s in samples}
    self.assertIn('startup_time_p50', by_name)
    self.assertEqual(by_name['peak_concurrency'].value, 2)
    self.assertEqual(by_name['success_count'].value, 2)
    # Metadata is threaded onto the samples.
    self.assertEqual(by_name['startup_time_p50'].metadata['target_qps'], 10.0)
```

Verify the exact metric names (`startup_time_p50`, `peak_concurrency`, `success_count`, etc.) against the source and adjust. If `build_samples` requires the records to have additional fields set (e.g. `released_at` for lifecycle metrics), only set what the asserted metrics need, and assert that lifecycle/exec metrics are ABSENT when their inputs are unset (matches the "emitted only if data exists" behavior).

- [ ] **Step 4: Run the tests**

```bash
/Users/eric.hole/dev/onix/PerfKitBenchmarker-internal/.venv/bin/python -m pytest tests/linux_benchmarks/agent_sandbox_metrics_test.py -v
```
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add perfkitbenchmarker/linux_benchmarks/agent_sandbox_metrics.py tests/linux_benchmarks/agent_sandbox_metrics_test.py
git commit -m "agent_sandbox: port the benchmark metrics"
```

---

## Task 4: Wire the real Run + load-shape flags

**Files:**
- Modify: `perfkitbenchmarker/linux_benchmarks/agent_sandbox_benchmark.py`
- Test: `tests/resources/kubernetes/k8s_agent_sandbox_test.py`

- [ ] **Step 1: Write the failing Run wiring test**

Add to `tests/resources/kubernetes/k8s_agent_sandbox_test.py` (re-add `from unittest import mock` if not present). The test mocks the loadgen and metrics so it verifies wiring only, not the real watch behavior:

```python
class AgentSandboxRunTest(pkb_common_test_case.PkbCommonTestCase):

  def testRunSourcesConfigFromResourceSpec(self):
    from perfkitbenchmarker.linux_benchmarks import agent_sandbox_benchmark
    from perfkitbenchmarker.linux_benchmarks import agent_sandbox_loadgen
    from perfkitbenchmarker.linux_benchmarks import agent_sandbox_metrics
    from perfkitbenchmarker.resources.kubernetes import k8s_agent_sandbox

    # Build a real spec so namespace/name/runtime_class/replicas come from it.
    sandbox_spec = k8s_agent_sandbox_spec.K8sAgentSandboxConfigSpec(
        _COMPONENT, flag_values=FLAGS, type='Kubernetes',
        namespace='sandboxes', sandbox_warmpool={'replicas': 4})
    sandbox = k8s_agent_sandbox.K8sAgentSandbox(sandbox_spec, mock.Mock())
    bm_spec = mock.Mock()
    bm_spec.container_cluster.agent_sandbox = sandbox
    bm_spec.container_cluster.GetResourceMetadata.return_value = {}

    sentinel = [mock.sentinel.sample]
    with mock.patch.object(
        agent_sandbox_loadgen, 'ClaimDriver') as mock_driver, \
        mock.patch.object(
            agent_sandbox_loadgen, 'LoadGenerator') as mock_gen, \
        mock.patch.object(
            agent_sandbox_metrics, 'build_samples',
            return_value=sentinel) as mock_build:
      mock_gen.return_value.run.return_value = ['rec']
      mock_gen.return_value.peak_concurrency = 4
      result = agent_sandbox_benchmark.Run(bm_spec)

    # Namespace comes from the spec, not a flag.
    self.assertEqual(
        mock_driver.call_args.kwargs['namespace'], 'sandboxes')
    # Template/warmpool use the shared resource name constant.
    self.assertEqual(
        mock_driver.call_args.kwargs['template_name'],
        k8s_agent_sandbox._SANDBOX_NAME)
    self.assertIs(result, sentinel)
```

Adjust the `ClaimDriver` kwarg assertions to match the real call (keyword vs positional) you implement in Step 3 — keep them consistent.

- [ ] **Step 2: Run, confirm it fails**

```bash
/Users/eric.hole/dev/onix/PerfKitBenchmarker-internal/.venv/bin/python -m pytest tests/resources/kubernetes/k8s_agent_sandbox_test.py -k AgentSandboxRun -v
```
Expected: FAIL (`Run` still returns `[]`; the mocked calls are never made).

- [ ] **Step 3: Implement the real Run + flags**

In `perfkitbenchmarker/linux_benchmarks/agent_sandbox_benchmark.py`:

Add imports near the top (with the existing imports):
```python
from perfkitbenchmarker.linux_benchmarks import agent_sandbox_loadgen
from perfkitbenchmarker.linux_benchmarks import agent_sandbox_metrics
from perfkitbenchmarker.resources.kubernetes import k8s_agent_sandbox
```

Add the load-shape flags (module level, after `FLAGS = flags.FLAGS`). Do NOT add `agent_sandbox_namespace` (it already exists in `k8s_agent_sandbox_spec.py`):
```python
_QPS = flags.DEFINE_float(
    'agent_sandbox_qps', 10.0,
    'Target SandboxClaim submission rate (claims per second).')
_DURATION = flags.DEFINE_float(
    'agent_sandbox_duration', 60.0,
    'Submission window in seconds. Combined with qps to derive total.')
_TOTAL = flags.DEFINE_integer(
    'agent_sandbox_total', None,
    'Total claims to submit. If set, overrides duration derivation.')
_MAX_CONCURRENT = flags.DEFINE_integer(
    'agent_sandbox_max_concurrent', 200, 'Maximum in-flight claims.')
_READY_TIMEOUT = flags.DEFINE_integer(
    'agent_sandbox_ready_timeout', 180, 'Per-claim ready timeout in seconds.')
_WORKLOAD_DURATION = flags.DEFINE_integer(
    'agent_sandbox_workload_duration', 0,
    'Seconds to run a CPU busy-loop inside each sandbox after it becomes '
    'Ready, before releasing it. 0 (default) releases immediately (pure '
    'provisioning benchmark). >0 holds the sandbox so peak_concurrency '
    'reflects simultaneously-alive sandboxes and exec/lifecycle metrics emit.')
_CLAIM_TTL = flags.DEFINE_integer(
    'agent_sandbox_claim_ttl_seconds', 120,
    'Server-side TTL set on each SandboxClaim so the controller auto-deletes '
    'orphaned claims if the driver is hard-killed. 0 disables the TTL.')
```

Replace the stub `Run` with:
```python
def Run(benchmark_spec):
  """Submits claims at the target rate and returns latency samples."""
  spec = benchmark_spec.container_cluster.agent_sandbox.spec
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
  workload_duration = _WORKLOAD_DURATION.value
  executor = None
  if workload_duration > 0:
    pool_size = _MAX_CONCURRENT.value + 50
    executor = agent_sandbox_loadgen.StreamExecExecutor(
        core_v1_api=agent_sandbox_loadgen._make_core_v1_api(pool_size),
        custom_objects_api=agent_sandbox_loadgen._make_custom_objects_api(
            pool_size),
        namespace=spec.namespace,
        workload_duration=workload_duration)
  generator = agent_sandbox_loadgen.LoadGenerator(
      driver, ready_timeout=_READY_TIMEOUT.value,
      max_concurrent=_MAX_CONCURRENT.value,
      workload_executor=executor, workload_duration=workload_duration)
  records = generator.run(shape)
  metadata = {
      'target_qps': shape.qps,
      'duration': shape.duration,
      'total_claims': shape.total,
      'warmpool_replicas': spec.sandbox_warmpool.replicas,
      'runtime_class': spec.sandbox_template.runtime_class,
  }
  metadata.update(benchmark_spec.container_cluster.GetResourceMetadata())
  return agent_sandbox_metrics.build_samples(
      records, generator.peak_concurrency, metadata)
```

IMPORTANT: the `StreamExecExecutor` construction uses private helper names (`_make_core_v1_api` / `_make_custom_objects_api`). The ported loadgen may name these differently (the research saw `_new_api_client` / `_load_kube_config`). Use whatever the ported `agent_sandbox_loadgen.py` actually exposes for building the CoreV1Api and CustomObjectsApi; read the module and match. If the workload-executor path is hard to wire because those helpers are not exposed, keep the `workload_duration > 0` branch consistent with the source's original Run (which is the contract).

- [ ] **Step 4: Run the wiring test (and the whole module)**

```bash
/Users/eric.hole/dev/onix/PerfKitBenchmarker-internal/.venv/bin/python -m pytest tests/resources/kubernetes/k8s_agent_sandbox_test.py -v
/Users/eric.hole/dev/onix/PerfKitBenchmarker-internal/.venv/bin/python -c "from perfkitbenchmarker.linux_benchmarks import agent_sandbox_benchmark; print('import OK')"
```
Expected: all tests pass; import prints `import OK`.

- [ ] **Step 5: Commit**

```bash
git add perfkitbenchmarker/linux_benchmarks/agent_sandbox_benchmark.py tests/resources/kubernetes/k8s_agent_sandbox_test.py
git commit -m "agent_sandbox: implement real Run driving the load generator"
```

---

## Final verification

- [ ] Run all the new/affected tests:
```bash
/Users/eric.hole/dev/onix/PerfKitBenchmarker-internal/.venv/bin/python -m pytest tests/linux_benchmarks/agent_sandbox_loadgen_test.py tests/linux_benchmarks/agent_sandbox_metrics_test.py tests/resources/kubernetes/k8s_agent_sandbox_test.py -v
```
- [ ] Confirm the loadgen/metrics modules are byte-identical to source except for any single import-path fix in metrics: `git show geojaz/agent-sandbox-1:perfkitbenchmarker/linux_packages/agent_sandbox_loadgen.py | diff - perfkitbenchmarker/linux_benchmarks/agent_sandbox_loadgen.py` (expect no diff), same for metrics (expect at most the one loadgen import path line).
- [ ] Confirm `agent_sandbox_namespace` is defined only once (in `k8s_agent_sandbox_spec.py`), not re-added to the benchmark: `grep -rn "agent_sandbox_namespace" perfkitbenchmarker/`.
- [ ] Confirm the branch diff vs `geojaz/agent-sandbox-resource` contains only the files in the file-structure table.
- [ ] Push `geojaz/agent-sandbox-benchmark` to `onix` (do NOT open the PR yet).

---

## Self-review notes (author)

- **Spec coverage:** loadgen port (Task 2), metrics port (Task 3), real Run sourcing config from the resource spec (Task 4), load-shape flags on the benchmark (Task 4), namespace from spec / no duplicate flag (Task 4 + final check), dependency (Task 1), focused tests on pure + injectable + wiring surface (Tasks 2-4). The optional `LoadGenerator.run()` fake-driver test (Task 2 Step 4) covers the design's run() test, with an explicit escape hatch if the driver coupling is impractical.
- **Verification points flagged for the implementer** (not placeholders): exact `ClaimRecord` construction/property formulas, exact metric names in `build_samples`, the real private helper names for building API clients, and the metrics module's loadgen import path. Each names the source of truth.
- **Naming consistency:** `_SANDBOX_NAME` (from k8s_agent_sandbox.py), `spec.namespace`, `spec.sandbox_template.runtime_class`, `spec.sandbox_warmpool.replicas`, `resolve_run_shape`, `ClaimDriver`, `LoadGenerator.run`, `build_samples`, `peak_concurrency` — all match the PR 2 code and the mapped loadgen/metrics interfaces.

**Next step:** execute task-by-task via superpowers:subagent-driven-development.
