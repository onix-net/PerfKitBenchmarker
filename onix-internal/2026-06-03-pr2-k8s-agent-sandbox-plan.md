# PR 2: Kubernetes agent sandbox resource — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the approved PR 1 skeleton into a working Kubernetes agent sandbox resource: a config-driven `K8sAgentSandboxConfigSpec`, a `K8sAgentSandbox._Create` that installs the kubernetes-sigs/agent-sandbox stack, the data manifests, and a minimal benchmark stub, with focused unit tests.

**Architecture:** `K8sAgentSandbox` is a `BaseAgentSandbox` (a resource attached to `KubernetesCluster` as `cluster.agent_sandbox`, already wired in PR 1). Its spec nests three sub-specs (`controller`, `sandbox_template`, `sandbox_warmpool`). `_Create` orchestrates gVisor install → controller install → SandboxTemplate apply → SandboxWarmPool install, porting the logic from the old `linux_packages/agent_sandbox.py`. `_Delete` is a documented no-op (the ephemeral cluster teardown reclaims everything).

**Tech Stack:** Python, absl flags, PKB `configs.spec` / `configs.option_decoders`, PKB `resources.container_service.kubernetes_commands` / `kubectl`, jinja2 data templates, `pkb_common_test_case` + `mock` for tests.

**Branch:** Cut `geojaz/agent-sandbox-resource` from `geojaz/agent-sandbox-skeleton` (PR 1 / #6730 is not merged yet, so PR 2 stacks on it). Rebase onto `upstream/master` once #6730 lands.

**Source of truth for the port:** The old implementation lives at `git show geojaz/agent-sandbox-1:perfkitbenchmarker/linux_packages/agent_sandbox.py`. Several functions are copied near-verbatim; the mapping is given per task. Read that file before Task 4/5.

---

## File structure

| File | Responsibility | Action |
|------|----------------|--------|
| `perfkitbenchmarker/resources/kubernetes/k8s_agent_sandbox_spec.py` | Config spec + 3 nested sub-specs + flag bridging | Flesh out (PR1 stub) |
| `perfkitbenchmarker/resources/kubernetes/k8s_agent_sandbox.py` | `K8sAgentSandbox` resource: `_Create` orchestration, private install methods, module-level pure helpers, `_Delete` no-op | Flesh out (PR1 stub) |
| `perfkitbenchmarker/data/agent_sandbox/gvisor-installer/{daemonset.yaml,install.sh,runtimeclass.yaml}` | gVisor installer assets | Create (copy verbatim from old branch) |
| `perfkitbenchmarker/data/agent_sandbox/sandbox-template.yaml.j2` | SandboxTemplate manifest, parameterized | Create (extended from old branch) |
| `perfkitbenchmarker/data/agent_sandbox/sandbox-warmpool.yaml.j2` | SandboxWarmPool manifest | Create (copy verbatim) |
| `perfkitbenchmarker/linux_benchmarks/agent_sandbox_benchmark.py` | Minimal stub benchmark (config + Prepare via cluster construction; `Run` returns `[]`) | Create |
| `tests/resources/kubernetes/k8s_agent_sandbox_test.py` | Unit tests: spec decode/construct, flag override, controller-manifest injection | Create |

**Naming convention used throughout:** one shared internal name `_SANDBOX_NAME = 'agent-sandbox'` for the `SandboxTemplate`, the `SandboxWarmPool`, and the warmpool's `sandboxTemplateRef`, so they always line up (replaces the old separate `template`/`warmpool` name flags).

**Out of scope (later PRs):** load generator + metrics + real `Run` (PR 3); node-pool sizing, gVisor-capable nodepools, provider changes (GKE PR 4, EKS PR 5, AKS PR 6); real teardown for static clusters; rendering the stubbed `sandbox_template` fields (`command`, `args`, `env`, `service_account`, `annotations`, `network_policy_management`, `env_vars_injection_policy`, `service`).

---

## Task 1: Bring in the data manifests

**Files:**
- Create: `perfkitbenchmarker/data/agent_sandbox/gvisor-installer/daemonset.yaml`
- Create: `perfkitbenchmarker/data/agent_sandbox/gvisor-installer/install.sh`
- Create: `perfkitbenchmarker/data/agent_sandbox/gvisor-installer/runtimeclass.yaml`
- Create: `perfkitbenchmarker/data/agent_sandbox/sandbox-warmpool.yaml.j2`
- Create: `perfkitbenchmarker/data/agent_sandbox/sandbox-template.yaml.j2`

- [ ] **Step 1: Copy the three gVisor installer files and the warmpool template verbatim from the old branch**

```bash
mkdir -p perfkitbenchmarker/data/agent_sandbox/gvisor-installer
for f in gvisor-installer/daemonset.yaml gvisor-installer/install.sh gvisor-installer/runtimeclass.yaml sandbox-warmpool.yaml.j2; do
  git show geojaz/agent-sandbox-1:perfkitbenchmarker/data/agent_sandbox/$f \
    > perfkitbenchmarker/data/agent_sandbox/$f
done
```

- [ ] **Step 2: Create the extended `sandbox-template.yaml.j2`**

The old template only parameterized `template_name` and `runtime_class`. Extend it so the pod shape (image, resources, labels) comes from the spec, with defaults identical to the old hardcoded values so behavior is preserved. The stubbed fields (`command`, `args`, `env`, etc.) are NOT rendered yet.

```yaml
# Reusable blueprint for the sandboxes that SandboxClaim will provision.
# Pod-shape values come from the sandbox_template config block; the defaults
# below match the original hardcoded template.
#
# The security/placement fields below are REQUIRED by GKE's
# secure-sandbox-policy ValidatingAdmissionPolicy on Agent-Sandbox-enabled
# clusters. They are harmless on clusters that do not run the policy.
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxTemplate
metadata:
  name: {{ template_name }}
spec:
  podTemplate:
    metadata:
      labels:
{% for k, v in labels.items() %}        {{ k }}: "{{ v }}"
{% endfor %}    spec:
      runtimeClassName: {{ runtime_class }}
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
      nodeSelector:
        sandbox.gke.io/runtime: {{ runtime_class }}
      tolerations:
        - key: sandbox.gke.io/runtime
          operator: Equal
          value: {{ runtime_class }}
          effect: NoSchedule
      containers:
        - name: python-runtime
          image: {{ image }}
          ports:
            - containerPort: 8888
          readinessProbe:
            httpGet:
              path: "/"
              port: 8888
            initialDelaySeconds: 0
            periodSeconds: 1
          securityContext:
            capabilities:
              drop: ["ALL"]
          resources:
            requests:
              cpu: "{{ cpu_request }}"
              memory: "{{ memory_request }}"
              ephemeral-storage: "256Mi"
            limits:
              cpu: "{{ cpu_limit }}"
              memory: "{{ memory_limit }}"
      restartPolicy: "OnFailure"
```

Template variables: `template_name`, `runtime_class`, `image`, `cpu_request`, `cpu_limit`, `memory_request`, `memory_limit`, `labels` (a dict; defaults to `{'sandbox': 'python-sandbox-bench'}` when the spec value is None, supplied by the caller in Task 5).

- [ ] **Step 3: Commit**

```bash
git add perfkitbenchmarker/data/agent_sandbox/
git commit -m "agent_sandbox: add gVisor installer assets and sandbox manifests"
```

---

## Task 2: Spec sub-specs and `K8sAgentSandboxConfigSpec` fields

**Files:**
- Modify: `perfkitbenchmarker/resources/kubernetes/k8s_agent_sandbox_spec.py`
- Test: `tests/resources/kubernetes/k8s_agent_sandbox_test.py`

- [ ] **Step 1: Write the failing decode test**

Create `tests/resources/kubernetes/k8s_agent_sandbox_test.py`:

```python
"""Tests for the Kubernetes agent sandbox spec and resource."""

import unittest
from unittest import mock

from perfkitbenchmarker.resources import agent_sandbox
from perfkitbenchmarker.resources import agent_sandbox_spec
from perfkitbenchmarker.resources.kubernetes import k8s_agent_sandbox
from perfkitbenchmarker.resources.kubernetes import k8s_agent_sandbox_spec
from tests import pkb_common_test_case
from absl.testing import flagsaver

FLAGS = flagsaver.FLAGS if hasattr(flagsaver, 'FLAGS') else None
_COMPONENT = 'test_component'


class K8sAgentSandboxSpecTest(pkb_common_test_case.PkbCommonTestCase):

  def _Decode(self, **overrides):
    config = {'type': 'Kubernetes'}
    config.update(overrides)
    return k8s_agent_sandbox_spec.K8sAgentSandboxConfigSpec(
        _COMPONENT, flag_values=self.flags, **config
    )

  def testDefaults(self):
    spec = self._Decode()
    self.assertEqual(spec.type, 'Kubernetes')
    self.assertEqual(spec.namespace, 'default')
    self.assertIsInstance(
        spec.controller, k8s_agent_sandbox_spec.ControllerSpec
    )
    self.assertIsInstance(
        spec.sandbox_template, k8s_agent_sandbox_spec.SandboxTemplateSpec
    )
    self.assertIsInstance(
        spec.sandbox_warmpool, k8s_agent_sandbox_spec.SandboxWarmPoolSpec
    )
    # Sub-spec defaults.
    self.assertEqual(spec.sandbox_template.runtime_class, 'runsc')
    self.assertEqual(spec.sandbox_warmpool.replicas, 0)
    self.assertFalse(spec.controller.leader_elect)

  def testNestedOverrides(self):
    spec = self._Decode(
        manifest_ref='abc123',
        controller={'claim_workers': 8, 'leader_elect': True},
        sandbox_template={'runtime_class': 'gvisor', 'cpu_limit': '4'},
        sandbox_warmpool={'replicas': 5},
    )
    self.assertEqual(spec.manifest_ref, 'abc123')
    self.assertEqual(spec.controller.claim_workers, 8)
    self.assertTrue(spec.controller.leader_elect)
    self.assertEqual(spec.sandbox_template.runtime_class, 'gvisor')
    self.assertEqual(spec.sandbox_template.cpu_limit, '4')
    self.assertEqual(spec.sandbox_warmpool.replicas, 5)


if __name__ == '__main__':
  unittest.main()
```

Note: `self.flags` is provided by `PkbCommonTestCase`. If it is not, use `flag_values=FLAGS` after `FLAGS(['pkb'])`; confirm the base-class attribute name when you run the test.

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/resources/kubernetes/k8s_agent_sandbox_test.py -v`
Expected: FAIL (ImportError / AttributeError: `ControllerSpec` not defined, `K8sAgentSandboxConfigSpec` has no `namespace`).

- [ ] **Step 3: Implement the spec module**

Replace the body of `perfkitbenchmarker/resources/kubernetes/k8s_agent_sandbox_spec.py` (keep the license header) with:

```python
"""Spec for the Kubernetes agent sandbox."""

from perfkitbenchmarker.configs import option_decoders
from perfkitbenchmarker.configs import spec
from perfkitbenchmarker.resources import agent_sandbox_spec

_DEFAULT_MANIFEST_REF = '32c4f231a116f76eb707fe34510b8143d61268ae'
_DEFAULT_CONTROLLER_IMAGE = (
    'us-central1-docker.pkg.dev/k8s-staging-images/agent-sandbox/'
    'agent-sandbox-controller:v20260527-v0.4.6-31-gd43447b-main'
)
_DEFAULT_SANDBOX_IMAGE = (
    'registry.k8s.io/agent-sandbox/python-runtime-sandbox:v0.4.6'
)


class ControllerSpec(spec.BaseSpec):
  """Config for the agent-sandbox controller deployment."""

  def __init__(self, *args, **kwargs):
    self.image: str
    self.claim_workers: int | None
    self.sandbox_workers: int | None
    self.warmpool_workers: int | None
    self.warmpool_max_batch_size: int | None
    self.kube_api_burst: int | None
    self.kube_api_qps: int | None
    self.enable_tracing: bool
    self.otel_endpoint: str | None
    self.leader_elect: bool
    self.cpu_request: str
    self.cpu_limit: str
    self.memory_request: str
    self.memory_limit: str
    super().__init__(*args, **kwargs)

  @classmethod
  def _GetOptionDecoderConstructions(cls):
    result = super()._GetOptionDecoderConstructions()
    result.update({
        'image': (
            option_decoders.StringDecoder,
            {'default': _DEFAULT_CONTROLLER_IMAGE},
        ),
        'claim_workers': (
            option_decoders.IntDecoder, {'default': None, 'none_ok': True}),
        'sandbox_workers': (
            option_decoders.IntDecoder, {'default': None, 'none_ok': True}),
        'warmpool_workers': (
            option_decoders.IntDecoder, {'default': None, 'none_ok': True}),
        'warmpool_max_batch_size': (
            option_decoders.IntDecoder, {'default': None, 'none_ok': True}),
        'kube_api_burst': (
            option_decoders.IntDecoder, {'default': None, 'none_ok': True}),
        'kube_api_qps': (
            option_decoders.IntDecoder, {'default': None, 'none_ok': True}),
        'enable_tracing': (
            option_decoders.BooleanDecoder, {'default': False}),
        'otel_endpoint': (
            option_decoders.StringDecoder, {'default': None, 'none_ok': True}),
        'leader_elect': (
            option_decoders.BooleanDecoder, {'default': False}),
        'cpu_request': (option_decoders.StringDecoder, {'default': '500m'}),
        'cpu_limit': (option_decoders.StringDecoder, {'default': '2'}),
        'memory_request': (
            option_decoders.StringDecoder, {'default': '256Mi'}),
        'memory_limit': (option_decoders.StringDecoder, {'default': '1Gi'}),
    })
    return result

  @classmethod
  def _ApplyFlags(cls, config_values, flag_values):
    super()._ApplyFlags(config_values, flag_values)
    if flag_values['agent_sandbox_controller_image'].present:
      config_values['image'] = flag_values.agent_sandbox_controller_image
    for flag_name, key in (
        ('agent_sandbox_controller_claim_workers', 'claim_workers'),
        ('agent_sandbox_controller_sandbox_workers', 'sandbox_workers'),
        ('agent_sandbox_controller_warmpool_workers', 'warmpool_workers'),
        ('agent_sandbox_controller_warmpool_max_batch_size',
         'warmpool_max_batch_size'),
        ('agent_sandbox_controller_kube_api_burst', 'kube_api_burst'),
        ('agent_sandbox_controller_kube_api_qps', 'kube_api_qps'),
        ('agent_sandbox_controller_otel_endpoint', 'otel_endpoint'),
        ('agent_sandbox_controller_enable_tracing', 'enable_tracing'),
        ('agent_sandbox_controller_leader_elect', 'leader_elect'),
    ):
      if flag_values[flag_name].present:
        config_values[key] = flag_values[flag_name].value


class SandboxTemplateSpec(spec.BaseSpec):
  """Config for the SandboxTemplate (models SandboxTemplateSpec).

  Pod-shape fields (runtime_class, image, resources, labels) are rendered into
  the template. The remaining fields are accepted and validated stubs, not yet
  rendered (added when a benchmark needs them).
  """

  def __init__(self, *args, **kwargs):
    self.runtime_class: str
    self.image: str
    self.cpu_request: str
    self.cpu_limit: str
    self.memory_request: str
    self.memory_limit: str
    self.labels: dict | None
    self.command: list | None
    self.args: list | None
    self.env: dict | None
    self.service_account: str | None
    self.annotations: dict | None
    self.network_policy_management: str
    self.env_vars_injection_policy: str
    self.service: bool | None
    super().__init__(*args, **kwargs)

  @classmethod
  def _GetOptionDecoderConstructions(cls):
    result = super()._GetOptionDecoderConstructions()
    result.update({
        'runtime_class': (
            option_decoders.StringDecoder, {'default': 'runsc'}),
        'image': (
            option_decoders.StringDecoder, {'default': _DEFAULT_SANDBOX_IMAGE}),
        'cpu_request': (option_decoders.StringDecoder, {'default': '100m'}),
        'cpu_limit': (option_decoders.StringDecoder, {'default': '500m'}),
        'memory_request': (
            option_decoders.StringDecoder, {'default': '256Mi'}),
        'memory_limit': (option_decoders.StringDecoder, {'default': '1Gi'}),
        'labels': (
            option_decoders.TypeVerifier, {'default': None, 'none_ok': True}),
        # Stub fields: accepted and validated, not yet rendered.
        'command': (
            option_decoders.ListDecoder,
            {'item_decoder': option_decoders.StringDecoder(),
             'default': None, 'none_ok': True}),
        'args': (
            option_decoders.ListDecoder,
            {'item_decoder': option_decoders.StringDecoder(),
             'default': None, 'none_ok': True}),
        'env': (
            option_decoders.TypeVerifier, {'default': None, 'none_ok': True}),
        'service_account': (
            option_decoders.StringDecoder, {'default': None, 'none_ok': True}),
        'annotations': (
            option_decoders.TypeVerifier, {'default': None, 'none_ok': True}),
        'network_policy_management': (
            option_decoders.EnumDecoder,
            {'valid_values': ['Managed', 'Unmanaged'], 'default': 'Managed'}),
        'env_vars_injection_policy': (
            option_decoders.EnumDecoder,
            {'valid_values': ['Disallowed', 'Allowed', 'Overrides'],
             'default': 'Disallowed'}),
        'service': (
            option_decoders.BooleanDecoder, {'default': None, 'none_ok': True}),
    })
    return result

  @classmethod
  def _ApplyFlags(cls, config_values, flag_values):
    super()._ApplyFlags(config_values, flag_values)
    if flag_values['agent_sandbox_runtime_class'].present:
      config_values['runtime_class'] = flag_values.agent_sandbox_runtime_class


class SandboxWarmPoolSpec(spec.BaseSpec):
  """Config for the SandboxWarmPool (models SandboxWarmPoolSpec)."""

  def __init__(self, *args, **kwargs):
    self.replicas: int
    super().__init__(*args, **kwargs)

  @classmethod
  def _GetOptionDecoderConstructions(cls):
    result = super()._GetOptionDecoderConstructions()
    result.update({
        'replicas': (option_decoders.IntDecoder, {'default': 0, 'min': 0}),
    })
    return result

  @classmethod
  def _ApplyFlags(cls, config_values, flag_values):
    super()._ApplyFlags(config_values, flag_values)
    if flag_values['agent_sandbox_warmpool_replicas'].present:
      config_values['replicas'] = flag_values.agent_sandbox_warmpool_replicas


class _ControllerDecoder(option_decoders.TypeVerifier):
  """Decodes the controller config block into a ControllerSpec."""

  def Decode(self, value, component_full_name, flag_values):
    super().Decode(value, component_full_name, flag_values)
    return ControllerSpec(
        self._GetOptionFullName(component_full_name),
        flag_values=flag_values, **value)


class _SandboxTemplateDecoder(option_decoders.TypeVerifier):
  """Decodes the sandbox_template config block into a SandboxTemplateSpec."""

  def Decode(self, value, component_full_name, flag_values):
    super().Decode(value, component_full_name, flag_values)
    return SandboxTemplateSpec(
        self._GetOptionFullName(component_full_name),
        flag_values=flag_values, **value)


class _SandboxWarmPoolDecoder(option_decoders.TypeVerifier):
  """Decodes the sandbox_warmpool config block into a SandboxWarmPoolSpec."""

  def Decode(self, value, component_full_name, flag_values):
    super().Decode(value, component_full_name, flag_values)
    return SandboxWarmPoolSpec(
        self._GetOptionFullName(component_full_name),
        flag_values=flag_values, **value)


class K8sAgentSandboxConfigSpec(agent_sandbox_spec.BaseAgentSandboxConfigSpec):
  """Config spec for the Kubernetes agent sandbox."""

  SANDBOX_TYPE = agent_sandbox_spec.DEFAULT_SANDBOX_TYPE

  def __init__(self, component_full_name, flag_values=None, **kwargs):
    self.manifest_ref: str
    self.namespace: str
    self.controller: ControllerSpec
    self.sandbox_template: SandboxTemplateSpec
    self.sandbox_warmpool: SandboxWarmPoolSpec
    super().__init__(component_full_name, flag_values=flag_values, **kwargs)
    # The sub-blocks are optional in config; build defaults when omitted so
    # _Create always has values. Flags still apply via each sub-spec __init__.
    if self.controller is None:
      self.controller = ControllerSpec(
          '{}.controller'.format(component_full_name), flag_values=flag_values)
    if self.sandbox_template is None:
      self.sandbox_template = SandboxTemplateSpec(
          '{}.sandbox_template'.format(component_full_name),
          flag_values=flag_values)
    if self.sandbox_warmpool is None:
      self.sandbox_warmpool = SandboxWarmPoolSpec(
          '{}.sandbox_warmpool'.format(component_full_name),
          flag_values=flag_values)

  @classmethod
  def _GetOptionDecoderConstructions(cls):
    result = super()._GetOptionDecoderConstructions()
    result.update({
        'manifest_ref': (
            option_decoders.StringDecoder, {'default': _DEFAULT_MANIFEST_REF}),
        'namespace': (option_decoders.StringDecoder, {'default': 'default'}),
        'controller': (_ControllerDecoder, {'default': None, 'none_ok': True}),
        'sandbox_template': (
            _SandboxTemplateDecoder, {'default': None, 'none_ok': True}),
        'sandbox_warmpool': (
            _SandboxWarmPoolDecoder, {'default': None, 'none_ok': True}),
    })
    return result

  @classmethod
  def _ApplyFlags(cls, config_values, flag_values):
    super()._ApplyFlags(config_values, flag_values)
    if flag_values['agent_sandbox_manifest_ref'].present:
      config_values['manifest_ref'] = flag_values.agent_sandbox_manifest_ref
    if flag_values['agent_sandbox_namespace'].present:
      config_values['namespace'] = flag_values.agent_sandbox_namespace
```

Note: the `_ApplyFlags` methods reference flags defined in Task 3. Until Task 3 runs, the `flag_values['agent_sandbox_*']` lookups will `KeyError` only if those flags are accessed; the decode test in this task does NOT set any agent_sandbox flag, but `_ApplyFlags` still calls `flag_values['name'].present`, which requires the flag to be defined. **Therefore Task 3 (flag definitions) must be importable before this test passes.** To keep the TDD loop honest, add the flag definitions (Task 3, Step 3) into this same module now, at the top, and treat Task 3 as the test for them. (Practically: do Task 2 Step 3 and Task 3 Step 3 together, commit once per task with the right files.)

- [ ] **Step 4: Run the decode test to verify it passes**

Run: `python -m pytest tests/resources/kubernetes/k8s_agent_sandbox_test.py -k Spec -v`
Expected: PASS (`testDefaults`, `testNestedOverrides`).

- [ ] **Step 5: Commit**

```bash
git add perfkitbenchmarker/resources/kubernetes/k8s_agent_sandbox_spec.py tests/resources/kubernetes/k8s_agent_sandbox_test.py
git commit -m "agent_sandbox: config-driven K8sAgentSandboxConfigSpec with nested sub-specs"
```

---

## Task 3: Flag definitions and the `manifest_ref` rename

**Files:**
- Modify: `perfkitbenchmarker/resources/kubernetes/k8s_agent_sandbox_spec.py` (add flag defs at top, below imports)
- Test: `tests/resources/kubernetes/k8s_agent_sandbox_test.py`

- [ ] **Step 1: Write the failing flag-override test**

Add to `K8sAgentSandboxSpecTest`:

```python
  @flagsaver.flagsaver(
      agent_sandbox_manifest_ref='deadbeef',
      agent_sandbox_runtime_class='gvisor',
      agent_sandbox_warmpool_replicas=7,
      agent_sandbox_controller_claim_workers=12,
      agent_sandbox_controller_leader_elect=True,
  )
  def testFlagsOverrideConfig(self):
    spec = self._Decode()  # no config-level overrides
    self.assertEqual(spec.manifest_ref, 'deadbeef')
    self.assertEqual(spec.sandbox_template.runtime_class, 'gvisor')
    self.assertEqual(spec.sandbox_warmpool.replicas, 7)
    self.assertEqual(spec.controller.claim_workers, 12)
    self.assertTrue(spec.controller.leader_elect)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/resources/kubernetes/k8s_agent_sandbox_test.py -k testFlagsOverrideConfig -v`
Expected: FAIL (`UnrecognizedFlagError: agent_sandbox_manifest_ref`).

- [ ] **Step 3: Define the flags at the top of `k8s_agent_sandbox_spec.py`** (immediately after the imports, before the default constants)

```python
from absl import flags

flags.DEFINE_string(
    'agent_sandbox_manifest_ref',
    None,
    'agent-sandbox release ref (tag or SHA) for CRD, RBAC, and controller '
    'manifests.',
)
flags.DEFINE_string(
    'agent_sandbox_namespace',
    None,
    'Namespace in which SandboxClaims are created.',
)
flags.DEFINE_string(
    'agent_sandbox_runtime_class',
    None,
    'RuntimeClass for sandbox pods.',
)
flags.DEFINE_integer(
    'agent_sandbox_warmpool_replicas',
    None,
    'SandboxWarmPool size to provision in Prepare.',
)
flags.DEFINE_string(
    'agent_sandbox_controller_image',
    None,
    'Controller container image.',
)
flags.DEFINE_integer(
    'agent_sandbox_controller_claim_workers', None,
    'Controller --sandbox-claim-concurrent-workers value.')
flags.DEFINE_integer(
    'agent_sandbox_controller_sandbox_workers', None,
    'Controller --sandbox-concurrent-workers value.')
flags.DEFINE_integer(
    'agent_sandbox_controller_warmpool_workers', None,
    'Controller --sandbox-warm-pool-concurrent-workers value.')
flags.DEFINE_integer(
    'agent_sandbox_controller_warmpool_max_batch_size', None,
    'Controller --sandbox-warm-pool-max-batch-size value.')
flags.DEFINE_integer(
    'agent_sandbox_controller_kube_api_burst', None,
    'Controller --kube-api-burst value.')
flags.DEFINE_integer(
    'agent_sandbox_controller_kube_api_qps', None,
    'Controller --kube-api-qps value.')
flags.DEFINE_boolean(
    'agent_sandbox_controller_enable_tracing', False,
    'Enable controller OpenTelemetry tracing.')
flags.DEFINE_string(
    'agent_sandbox_controller_otel_endpoint', None,
    'OTLP exporter endpoint when tracing is enabled.')
flags.DEFINE_boolean(
    'agent_sandbox_controller_leader_elect', False,
    'Whether the controller runs with leader election enabled.')
```

Notes:
- Flag defaults are `None` (except the two booleans) so that `_ApplyFlags` only overrides config when a flag is explicitly `present`. This is the rename of the old `agent_sandbox_controller_ref` to `agent_sandbox_manifest_ref`, and it drops the old `agent_sandbox_template` and `agent_sandbox_warmpool` name flags.
- These flags previously lived in `agent_sandbox_benchmark.py`. The PR 2 benchmark stub (Task 6) must NOT redefine them.

- [ ] **Step 4: Run the flag-override test to verify it passes**

Run: `python -m pytest tests/resources/kubernetes/k8s_agent_sandbox_test.py -v`
Expected: PASS (all three spec tests).

- [ ] **Step 5: Commit**

```bash
git add perfkitbenchmarker/resources/kubernetes/k8s_agent_sandbox_spec.py
git commit -m "agent_sandbox: define stack/controller flags, rename controller_ref to manifest_ref"
```

---

## Task 4: Port the controller-manifest configuration helper (pure logic, unit-tested)

**Files:**
- Modify: `perfkitbenchmarker/resources/kubernetes/k8s_agent_sandbox.py`
- Test: `tests/resources/kubernetes/k8s_agent_sandbox_test.py`

`_configure_controller_manifest(manifest, controller_image, tuning)` is the highest-value pure logic. Port it verbatim from `geojaz/agent-sandbox-1:perfkitbenchmarker/linux_packages/agent_sandbox.py` as a MODULE-LEVEL function (not a method) so it stays trivially testable. Bring along the module constants it depends on: `_TUNING_ARG_MAP`, `_DEFAULT_CPU_REQUEST`, `_DEFAULT_CPU_LIMIT`, `_DEFAULT_MEMORY_REQUEST`, `_DEFAULT_MEMORY_LIMIT`.

- [ ] **Step 1: Write the failing test for manifest injection**

Add to the test file (new test class):

```python
class ConfigureControllerManifestTest(pkb_common_test_case.PkbCommonTestCase):

  def _Manifest(self):
    # Minimal controller Deployment matching the upstream extensions.controller
    # shape: spec.template.spec.containers[0] with args and resources.
    return {
        'kind': 'Deployment',
        'spec': {'template': {'spec': {'containers': [{
            'name': 'manager',
            'image': 'placeholder',
            'args': ['--existing-arg'],
            'resources': {},
        }]}}},
    }

  def testImageAndTuningInjected(self):
    out = k8s_agent_sandbox._configure_controller_manifest(
        self._Manifest(),
        controller_image='my/image:tag',
        tuning={'claim_workers': 8, 'kube_api_qps': 50, 'leader_elect': True},
    )
    container = out['spec']['template']['spec']['containers'][0]
    self.assertEqual(container['image'], 'my/image:tag')
    self.assertIn('--sandbox-claim-concurrent-workers=8', container['args'])
    self.assertIn('--kube-api-qps=50', container['args'])

  def testResourceDefaultsApplied(self):
    out = k8s_agent_sandbox._configure_controller_manifest(
        self._Manifest(), controller_image='img', tuning={})
    res = out['spec']['template']['spec']['containers'][0]['resources']
    self.assertEqual(res['requests']['cpu'], k8s_agent_sandbox._DEFAULT_CPU_REQUEST)
    self.assertEqual(res['limits']['memory'], k8s_agent_sandbox._DEFAULT_MEMORY_LIMIT)
```

If the ported function signature takes a YAML string instead of a dict (confirm against the source — the old `_configure_controller_manifest` operates on parsed YAML), adapt the test to pass/parse accordingly. Read the source function first and match the test to its real input/output contract.

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/resources/kubernetes/k8s_agent_sandbox_test.py -k ConfigureControllerManifest -v`
Expected: FAIL (`module 'k8s_agent_sandbox' has no attribute '_configure_controller_manifest'`).

- [ ] **Step 3: Port the function and its constants into `k8s_agent_sandbox.py`**

Copy verbatim from the source, as module-level definitions: `_TUNING_ARG_MAP`, the four `_DEFAULT_*` resource constants, and `_configure_controller_manifest`. Do not change its logic. Add the imports it needs (`yaml` if it parses YAML; the constants). Keep the function module-level.

Source: `git show geojaz/agent-sandbox-1:perfkitbenchmarker/linux_packages/agent_sandbox.py` — copy the `_TUNING_ARG_MAP` tuple, `_DEFAULT_CPU_REQUEST`/`_DEFAULT_CPU_LIMIT`/`_DEFAULT_MEMORY_REQUEST`/`_DEFAULT_MEMORY_LIMIT`, and the full body of `_configure_controller_manifest` unchanged.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/resources/kubernetes/k8s_agent_sandbox_test.py -k ConfigureControllerManifest -v`
Expected: PASS. (Adjust the test's manifest shape if the source function expects slightly different keys; the source is the contract.)

- [ ] **Step 5: Commit**

```bash
git add perfkitbenchmarker/resources/kubernetes/k8s_agent_sandbox.py tests/resources/kubernetes/k8s_agent_sandbox_test.py
git commit -m "agent_sandbox: port controller-manifest configuration helper with tests"
```

---

## Task 5: Implement `K8sAgentSandbox._Create` orchestration and `_Delete`

**Files:**
- Modify: `perfkitbenchmarker/resources/kubernetes/k8s_agent_sandbox.py`
- Test: `tests/resources/kubernetes/k8s_agent_sandbox_test.py`

Port the remaining helpers from the source file as MODULE-LEVEL functions (they are stateless and take explicit args): `_crd_name`, `_url`, `_apply_url`, `_apply_yaml`, `_create_installer_configmap`, `_wait_warmpool_ready`, `install_gvisor`, `install_controller`, `apply_template`, `install_warmpool`. Also bring the path/release constants: `_GVISOR_DAEMONSET`, `_GVISOR_RUNTIMECLASS`, `_GVISOR_INSTALLER_SCRIPT`, `_GVISOR_CONFIGMAP_NAME`, `_TEMPLATE_MANIFEST`, `_WARMPOOL_MANIFEST`, `_RELEASE_BASE`, `_CRD_FILES`, `_RBAC_FILES`, `_CORE_FILE`, `_CONTROLLER_FILE`. Add `_SANDBOX_NAME = 'agent-sandbox'`.

Then implement `_Create` as orchestration that reads `self.spec` and calls these helpers as private methods.

**Argument mapping (old install_stack call -> new spec-driven calls):**

| Old install_stack arg | New source |
|-----------------------|------------|
| `controller_ref` | `self.spec.manifest_ref` |
| `controller_image` | `self.spec.controller.image` |
| `runtime_class` | `self.spec.sandbox_template.runtime_class` |
| `template_name` | `_SANDBOX_NAME` |
| `warmpool_name` | `_SANDBOX_NAME` |
| `warmpool_replicas` | `self.spec.sandbox_warmpool.replicas` |
| `controller_tuning` (dict) | built from `self.spec.controller.*` (see `_BuildTuning` below) |

`apply_template` previously took `(template_name, runtime_class)` and rendered the j2 with those two vars. It must now also pass the new template vars (`image`, `cpu_request`, `cpu_limit`, `memory_request`, `memory_limit`, `labels`). Update `apply_template`'s signature to accept the `SandboxTemplateSpec` (or the individual values) and pass them all to `kubernetes_commands.ApplyManifest`. Default `labels` to `{'sandbox': 'python-sandbox-bench'}` when `spec.labels` is None.

- [ ] **Step 1: Write the failing `_Create` orchestration test**

Add (new test class). It mocks every outbound Kubernetes call and asserts the orchestration runs in order with spec-derived values.

```python
class K8sAgentSandboxCreateTest(pkb_common_test_case.PkbCommonTestCase):

  def _Sandbox(self, **template_overrides):
    sandbox_spec = k8s_agent_sandbox_spec.K8sAgentSandboxConfigSpec(
        _COMPONENT, flag_values=self.flags,
        type='Kubernetes', manifest_ref='ref123',
        sandbox_warmpool={'replicas': 3},
        sandbox_template=template_overrides or {'runtime_class': 'runsc'},
    )
    cluster = mock.Mock()
    return k8s_agent_sandbox.K8sAgentSandbox(sandbox_spec, cluster)

  @mock.patch.object(k8s_agent_sandbox, 'install_warmpool')
  @mock.patch.object(k8s_agent_sandbox, 'apply_template')
  @mock.patch.object(k8s_agent_sandbox, 'install_controller')
  @mock.patch.object(k8s_agent_sandbox, 'install_gvisor')
  def testCreateOrchestration(
      self, mock_gvisor, mock_controller, mock_template, mock_warmpool):
    sandbox = self._Sandbox()
    sandbox._Create()
    mock_gvisor.assert_called_once()
    # Controller installed with the configured ref + image.
    _, kwargs = mock_controller.call_args
    self.assertEqual(kwargs.get('controller_ref', None) or mock_controller.call_args[0][0], 'ref123')
    mock_template.assert_called_once()
    mock_warmpool.assert_called_once()

  def testDeleteIsNoOp(self):
    sandbox = self._Sandbox()
    self.assertIsNone(sandbox._Delete())  # does not raise
```

Adjust the controller-call assertion to match whether you call `install_controller` positionally or by keyword in the implementation; keep the implementation and the assertion consistent.

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/resources/kubernetes/k8s_agent_sandbox_test.py -k K8sAgentSandboxCreate -v`
Expected: FAIL (`_Create` raises `NotImplementedError`).

- [ ] **Step 3: Implement `_Create`, `_Delete`, and the private orchestration methods**

In `k8s_agent_sandbox.py`, replace the `_Create`/`_Delete` stubs:

```python
  def _Create(self):
    """Installs the kubernetes-sigs/agent-sandbox stack onto the cluster."""
    self._InstallGvisor()
    self._InstallController()
    self._ApplyTemplate()
    self._InstallWarmpool()

  def _Delete(self):
    """No-op: the ephemeral cluster teardown reclaims the sandbox stack."""
    pass

  def _InstallGvisor(self):
    install_gvisor()

  def _InstallController(self):
    install_controller(
        controller_ref=self.spec.manifest_ref,
        controller_image=self.spec.controller.image,
        controller_tuning=self._BuildTuning(),
    )

  def _ApplyTemplate(self):
    apply_template(_SANDBOX_NAME, self.spec.sandbox_template)

  def _InstallWarmpool(self):
    install_warmpool(
        _SANDBOX_NAME, _SANDBOX_NAME, self.spec.sandbox_warmpool.replicas)

  def _BuildTuning(self):
    """Builds the controller_tuning dict from the controller sub-spec."""
    c = self.spec.controller
    tuning = {
        'enable_tracing': c.enable_tracing,
        'leader_elect': c.leader_elect,
        'cpu_request': c.cpu_request,
        'cpu_limit': c.cpu_limit,
        'memory_request': c.memory_request,
        'memory_limit': c.memory_limit,
    }
    for key in (
        'claim_workers', 'sandbox_workers', 'warmpool_workers',
        'warmpool_max_batch_size', 'kube_api_burst', 'kube_api_qps',
        'otel_endpoint',
    ):
      value = getattr(c, key)
      if value is not None:
        tuning[key] = value
    return tuning
```

Then update the ported `apply_template` to accept the template spec and pass all jinja vars. Its body should call `kubernetes_commands.ApplyManifest` (as the old `apply_template` did) with the manifest path `_TEMPLATE_MANIFEST` and the template vars:

```python
def apply_template(template_name, template_spec):
  """Applies the SandboxTemplate rendered from the template spec."""
  labels = template_spec.labels or {'sandbox': 'python-sandbox-bench'}
  kubernetes_commands.ApplyManifest(
      _TEMPLATE_MANIFEST,
      template_name=template_name,
      runtime_class=template_spec.runtime_class,
      image=template_spec.image,
      cpu_request=template_spec.cpu_request,
      cpu_limit=template_spec.cpu_limit,
      memory_request=template_spec.memory_request,
      memory_limit=template_spec.memory_limit,
      labels=labels,
  )
```

Confirm `kubernetes_commands.ApplyManifest` forwards arbitrary kwargs as jinja template variables (the old `apply_template` already relied on this for `template_name`/`runtime_class`); match its real signature.

Keep `install_gvisor`, `install_controller`, `install_warmpool`, and the helpers as the verbatim port (only `apply_template` changes signature). `install_controller` keyword args (`controller_ref`, `controller_image`, `controller_tuning`) match the old function; verify against source.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/resources/kubernetes/k8s_agent_sandbox_test.py -v`
Expected: PASS (all classes).

- [ ] **Step 5: Commit**

```bash
git add perfkitbenchmarker/resources/kubernetes/k8s_agent_sandbox.py tests/resources/kubernetes/k8s_agent_sandbox_test.py
git commit -m "agent_sandbox: implement K8sAgentSandbox._Create orchestration and no-op _Delete"
```

---

## Task 6: Minimal benchmark stub

**Files:**
- Create: `perfkitbenchmarker/linux_benchmarks/agent_sandbox_benchmark.py`
- Test: `tests/resources/kubernetes/k8s_agent_sandbox_test.py` (add a config-construction test)

- [ ] **Step 1: Write the failing benchmark-config test**

Add a test that loads the benchmark config and confirms the cluster spec produces a `K8sAgentSandbox`:

```python
class AgentSandboxBenchmarkConfigTest(pkb_common_test_case.PkbCommonTestCase):

  def testConfigBuildsK8sAgentSandbox(self):
    from perfkitbenchmarker import configs
    from perfkitbenchmarker.linux_benchmarks import agent_sandbox_benchmark
    config = configs.LoadConfig(
        agent_sandbox_benchmark.BENCHMARK_CONFIG, {},
        agent_sandbox_benchmark.BENCHMARK_NAME)
    cluster_spec = configs.benchmark_config_spec.BenchmarkConfigSpec(
        agent_sandbox_benchmark.BENCHMARK_NAME, flag_values=self.flags,
        **config).container_cluster
    self.assertIsNotNone(cluster_spec.agent_sandbox)
    sandbox = agent_sandbox.GetAgentSandbox(cluster_spec.agent_sandbox, mock.Mock())
    self.assertIsInstance(sandbox, k8s_agent_sandbox.K8sAgentSandbox)
```

If `BenchmarkConfigSpec` construction is heavy in tests, simplify to: decode the `agent_sandbox` sub-dict directly through `agent_sandbox_spec.AgentSandboxConfigDecoder().Decode(...)` and assert `GetAgentSandbox` returns a `K8sAgentSandbox`. Pick whichever matches existing benchmark-config tests in `tests/`.

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/resources/kubernetes/k8s_agent_sandbox_test.py -k Benchmark -v`
Expected: FAIL (`No module named ...agent_sandbox_benchmark`).

- [ ] **Step 3: Create the stub benchmark**

```python
# (license header)
"""Stub benchmark that provisions the Kubernetes agent sandbox resource.

The agent sandbox is installed via the container_cluster.agent_sandbox config
block (cluster construction calls K8sAgentSandbox.Create()). The load
generator and metrics land in a follow-up change; Run currently returns no
samples.
"""

from perfkitbenchmarker import configs

BENCHMARK_NAME = 'agent_sandbox'
BENCHMARK_CONFIG = """
agent_sandbox:
  description: >
    Provision the agent-sandbox stack on a Kubernetes cluster. Load generation
    and metrics are added in a follow-up change.
  container_cluster:
    cloud: GCP
    type: Kubernetes
    vm_count: 1
    vm_spec:
      GCP:
        machine_type: c4-standard-4
        zone: us-central1-a
      AWS:
        machine_type: m8i.xlarge
        zone: us-east-1a
    nodepools:
      sandbox:
        vm_count: 4
        vm_spec:
          GCP:
            machine_type: c4-standard-16
            zone: us-central1-a
          AWS:
            machine_type: m8i.4xlarge
            zone: us-east-1a
        node_labels:
          sandbox.gke.io/runtime: runsc
        node_taints:
          - sandbox.gke.io/runtime=runsc:NoSchedule
    agent_sandbox:
      type: Kubernetes
"""


def GetConfig(user_config):
  """Loads the benchmark config and merges user overrides."""
  config = configs.LoadConfig(BENCHMARK_CONFIG, user_config, BENCHMARK_NAME)
  config['container_cluster']['cloud'] = FLAGS.cloud
  return config


def Prepare(benchmark_spec):
  """No-op: the agent sandbox is installed during cluster construction."""
  del benchmark_spec


def Run(benchmark_spec):
  """Returns no samples yet. Load generation lands in a follow-up change."""
  del benchmark_spec
  return []  # TODO(PR3): run the load generator and return samples.


def Cleanup(benchmark_spec):
  """No-op: cluster teardown reclaims the agent sandbox stack."""
  del benchmark_spec
```

Add the `from absl import flags` import and `FLAGS = flags.FLAGS` at top (GetConfig references `FLAGS.cloud`). Do NOT define any `agent_sandbox_*` flag here (they live in `k8s_agent_sandbox_spec.py`). Include the standard license header.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/resources/kubernetes/k8s_agent_sandbox_test.py -v`
Expected: PASS (all classes, including the benchmark config test).

- [ ] **Step 5: Run the full new-test module and a quick import smoke**

Run:
```bash
python -m pytest tests/resources/kubernetes/k8s_agent_sandbox_test.py -v
python -c "from perfkitbenchmarker.linux_benchmarks import agent_sandbox_benchmark; from perfkitbenchmarker.resources.kubernetes import k8s_agent_sandbox, k8s_agent_sandbox_spec; print('import OK')"
```
Expected: all tests PASS; import prints `import OK`.

- [ ] **Step 6: Commit**

```bash
git add perfkitbenchmarker/linux_benchmarks/agent_sandbox_benchmark.py tests/resources/kubernetes/k8s_agent_sandbox_test.py
git commit -m "agent_sandbox: add stub benchmark that provisions the resource"
```

---

## Final verification

- [ ] Run the full new test module once more: `python -m pytest tests/resources/kubernetes/k8s_agent_sandbox_test.py -v` — all pass.
- [ ] Confirm no `agent_sandbox_*` flag is defined twice (grep both the spec module and the benchmark): `grep -rn "DEFINE_" perfkitbenchmarker/resources/kubernetes/k8s_agent_sandbox_spec.py perfkitbenchmarker/linux_benchmarks/agent_sandbox_benchmark.py`.
- [ ] Confirm the diff vs `geojaz/agent-sandbox-skeleton` contains only the seven files in the file-structure table.
- [ ] Push branch `geojaz/agent-sandbox-resource` to `onix` and open PR 2 against `GoogleCloudPlatform/PerfKitBenchmarker` (base `master`) once #6730 has merged; until then, keep it stacked and note the dependency in the PR body.

---

## Self-review notes (author)

- **Spec coverage vs design doc:** config-driven spec (Tasks 2–3), `_Delete` no-op (Task 5), benchmark stub (Task 6), focused unit tests (decode + flag override + manifest injection + create orchestration, Tasks 2–6), install helpers as private methods with pure logic at module level (Tasks 4–5), data files incl. extended j2 (Task 1), `manifest_ref` rename (Task 3), shared internal name for template/warmpool (Task 5). All covered.
- **Known verification points for the implementer** (flagged inline, not placeholders): exact input contract of `_configure_controller_manifest` (dict vs YAML string), `kubernetes_commands.ApplyManifest` kwarg-forwarding signature, `PkbCommonTestCase` flag attribute name (`self.flags`), and `install_controller` arg style. Each names the source of truth to check.
- **Stubbed `sandbox_template` fields** (`command`, `args`, `env`, `service_account`, `annotations`, `network_policy_management`, `env_vars_injection_policy`, `service`) are intentionally accepted/validated but not rendered; this is the agreed "at least stubs" scope.
- **Namespace** is carried as a spec field but not consumed by `_Create` (the old install path did not namespace resources); it is wired into the load generator in PR 3.

**Next step:** execute task-by-task via superpowers:subagent-driven-development (recommended) or superpowers:executing-plans.
