"""PKB benchmark for agent-sandbox v0.4.5 SandboxClaim lifecycle latency.

Provisions a GKE cluster, installs the agent-sandbox controller (core +
extensions), deploys the sandbox router and warmpool, then runs load_runner.py
as a Kubernetes Job. Parses the JSONL output into PKB Sample objects with
p50/p90/p95/p99 aggregates for each timing metric.

Metrics (all in seconds):
  startup_time_s, time_to_first_exec_s, exec_duration_s, total_lifecycle_s,
  pod_startup_s, create_to_schedule_s, schedule_to_containers_ready_s,
  pod_age_at_bind_s

Usage:
  python pkb.py --project=<gcp_project> --benchmarks=agent_sandbox \
    --zone=us-central1-a --owner=<name> --gke_enable_private_nodes=true \
    --accept_licenses
"""

import collections
import json
import logging
import textwrap

from absl import flags
from perfkitbenchmarker import configs
from perfkitbenchmarker import data
from perfkitbenchmarker import errors
from perfkitbenchmarker import sample
from perfkitbenchmarker import vm_util
from perfkitbenchmarker.resources.container_service import kubectl
from perfkitbenchmarker.resources.container_service import kubernetes_commands

BENCHMARK_NAME = 'agent_sandbox'
BENCHMARK_CONFIG = """
agent_sandbox:
  description: >
    Measures SandboxClaim lifecycle latency (startup_time_s,
    time_to_first_exec_s, total_lifecycle_s, etc.) using agent-sandbox v0.4.5.
    Provisions a GKE cluster, installs agent-sandbox controller + warmpool,
    runs load_runner.py in-cluster, and returns timing Samples.
  container_cluster:
    cloud: GCP
    type: Kubernetes
    vm_count: 3
    vm_spec:
      GCP:
        machine_type: n2-standard-4
        zone: us-central1-a
"""

FLAGS = flags.FLAGS

_NAMESPACE = 'agent-sandbox-bench'
_TEMPLATE_NAME = 'python-runtime-template'
_LOAD_RUNNER_JOB = 'agent-sandbox-load-runner'
_LOAD_RUNNER_CONFIGMAP = 'load-runner-script'

flags.DEFINE_float(
    'agent_sandbox_qps',
    1.0,
    'Target QPS (claims per second) for the load runner.',
)
flags.DEFINE_integer(
    'agent_sandbox_total',
    30,
    'Total number of SandboxClaims to create.',
)
flags.DEFINE_integer(
    'agent_sandbox_warmpool_size',
    5,
    'Number of pre-warmed pods in the SandboxWarmPool.',
)
flags.DEFINE_integer(
    'agent_sandbox_max_concurrent',
    50,
    'Maximum number of concurrent in-flight claims.',
)
flags.DEFINE_integer(
    'agent_sandbox_workload_duration',
    20,
    'Duration in seconds of the per-sandbox Python busy-loop workload.',
)
flags.DEFINE_boolean(
    'agent_sandbox_use_gvisor',
    False,
    'If True, configure SandboxTemplate to use the gVisor runtime class. '
    'Requires nodes pre-labeled and tainted for gVisor.',
)


def GetConfig(user_config):
  return configs.LoadConfig(BENCHMARK_CONFIG, user_config, BENCHMARK_NAME)


def CheckPrerequisites(benchmark_config):
  if FLAGS.agent_sandbox_qps <= 0:
    raise errors.Config.InvalidValue(
        f'agent_sandbox_qps must be > 0, got {FLAGS.agent_sandbox_qps}'
    )
  if FLAGS.agent_sandbox_total <= 0:
    raise errors.Config.InvalidValue(
        f'agent_sandbox_total must be > 0, got {FLAGS.agent_sandbox_total}'
    )
  if FLAGS.agent_sandbox_warmpool_size < 0:
    raise errors.Config.InvalidValue(
        f'agent_sandbox_warmpool_size must be >= 0, '
        f'got {FLAGS.agent_sandbox_warmpool_size}'
    )


def _WaitForDeployment(name: str, namespace: str, timeout: int = 600) -> None:
  """Waits for a Deployment to become Available; logs pod state on timeout."""
  try:
    kubernetes_commands.WaitForResource(
        f'deployment/{name}',
        'Available',
        namespace=namespace,
        timeout=timeout,
    )
  except Exception:
    # Log pod state to help diagnose failures before re-raising.
    stdout, _, _ = kubectl.RunKubectlCommand(
        ['get', 'pods', '-n', namespace, '-o', 'wide'],
        raise_on_failure=False,
        timeout=30,
    )
    logging.error(
        'Deployment %s/%s not available after %ds. Pod state:\n%s',
        namespace, name, timeout, stdout,
    )
    raise


def Prepare(benchmark_spec):
  """Installs agent-sandbox components and prepares the warmpool."""
  del benchmark_spec  # cluster already provisioned by PKB

  # 1. Create benchmark namespace.
  kubernetes_commands.ApplyManifest('container/agent_sandbox/namespace.yaml')

  # 2. Install agent-sandbox core controller + Sandbox CRD.
  kubernetes_commands.ApplyManifest('container/agent_sandbox/install_core.yaml')
  _WaitForDeployment('agent-sandbox-controller', 'agent-sandbox-system')

  # 3. Install extensions (SandboxClaim/SandboxTemplate/WarmPool CRDs).
  # Extensions update the existing agent-sandbox-controller deployment; no
  # separate deployment is created so we wait for the existing one to settle.
  kubernetes_commands.ApplyManifest(
      'container/agent_sandbox/install_extensions.yaml'
  )
  _WaitForDeployment('agent-sandbox-controller', 'agent-sandbox-system')

  # 4. Deploy sandbox router (required for direct-mode load runner).
  kubernetes_commands.ApplyManifest('container/agent_sandbox/sandbox_router.yaml')
  kubernetes_commands.WaitForRollout(
      'deployment/sandbox-router',
      timeout=180,
      namespace=_NAMESPACE,
  )

  # 5. Apply SandboxTemplate CR.
  kubernetes_commands.ApplyManifest(
      'container/agent_sandbox/sandbox_template.yaml.j2',
      Namespace=_NAMESPACE,
      TemplateName=_TEMPLATE_NAME,
      UseGvisor=FLAGS.agent_sandbox_use_gvisor,
  )

  # 6. Apply SandboxWarmPool CR.
  kubernetes_commands.ApplyManifest(
      'container/agent_sandbox/warmpool.yaml.j2',
      Namespace=_NAMESPACE,
      TemplateName=_TEMPLATE_NAME,
      WarmPoolSize=FLAGS.agent_sandbox_warmpool_size,
  )

  # 7. Create load-runner-script ConfigMap by rendering YAML to a temp file.
  load_runner_src = data.ResourcePath('container/agent_sandbox/load_runner.py')
  with open(load_runner_src) as f:
    script_content = f.read()

  configmap_yaml = '\n'.join([
      'apiVersion: v1',
      'kind: ConfigMap',
      'metadata:',
      f'  name: {_LOAD_RUNNER_CONFIGMAP}',
      f'  namespace: {_NAMESPACE}',
      'data:',
      '  load_runner.py: |',
      textwrap.indent(script_content, '    '),
  ])
  with vm_util.NamedTemporaryFile(mode='w', suffix='.yaml') as tf:
    tf.write(configmap_yaml)
    tf.close()
    kubectl.RunKubectlCommand(['apply', '-f', tf.name], timeout=30)

  # 8. Apply load-runner RBAC (ServiceAccount + Role + RoleBinding).
  kubernetes_commands.ApplyManifest('container/agent_sandbox/load_runner_rbac.yaml')


def Run(benchmark_spec):
  """Runs load_runner.py as a Kubernetes Job and returns SandboxClaim Samples."""
  del benchmark_spec

  metadata = {
      'qps': FLAGS.agent_sandbox_qps,
      'total': FLAGS.agent_sandbox_total,
      'warmpool_size': FLAGS.agent_sandbox_warmpool_size,
      'max_concurrent': FLAGS.agent_sandbox_max_concurrent,
      'workload_duration_s': FLAGS.agent_sandbox_workload_duration,
      'use_gvisor': FLAGS.agent_sandbox_use_gvisor,
  }

  # Apply the Kubernetes Job manifest.
  kubernetes_commands.ApplyManifest(
      'container/agent_sandbox/load_runner_job.yaml.j2',
      Namespace=_NAMESPACE,
      TemplateName=_TEMPLATE_NAME,
      QPS=FLAGS.agent_sandbox_qps,
      Total=FLAGS.agent_sandbox_total,
      MaxConcurrent=FLAGS.agent_sandbox_max_concurrent,
      WorkloadDuration=FLAGS.agent_sandbox_workload_duration,
  )

  # Wait for the Job to succeed. Timeout: submission window + workload + buffer.
  job_timeout = int(
      FLAGS.agent_sandbox_total / FLAGS.agent_sandbox_qps
      + FLAGS.agent_sandbox_workload_duration
      + 600
  )
  kubectl.RunKubectlCommand(
      [
          'wait',
          '--for=condition=Complete',
          f'job/{_LOAD_RUNNER_JOB}',
          f'--timeout={job_timeout}s',
          '-n', _NAMESPACE,
      ],
      timeout=job_timeout + 30,
  )

  # Get the pod name for the completed job.
  pod_stdout, _, _ = kubectl.RunKubectlCommand(
      [
          'get', 'pods',
          '-l', f'job-name={_LOAD_RUNNER_JOB}',
          '-n', _NAMESPACE,
          '-o', 'jsonpath={.items[0].metadata.name}',
      ],
      timeout=30,
  )
  pod_name = pod_stdout.strip()
  if not pod_name:
    raise errors.Benchmarks.RunError(
        f'No pod found for job {_LOAD_RUNNER_JOB} in namespace {_NAMESPACE}'
    )

  # Retrieve results via logs: the job cats run.jsonl to stdout after '---RESULTS---'.
  # kubectl cp cannot exec into a completed (Succeeded) pod.
  logs_stdout, _, _ = kubectl.RunKubectlCommand(
      ['logs', pod_name, '-n', _NAMESPACE],
      timeout=60,
  )
  # Extract only the lines after the sentinel so progress output is excluded.
  sentinel = '---RESULTS---'
  if sentinel in logs_stdout:
    jsonl_content = logs_stdout[logs_stdout.index(sentinel) + len(sentinel):]
  else:
    jsonl_content = logs_stdout

  return _ParseLoadRunnerOutput(jsonl_content, metadata)


def _ComputeMetrics(record: dict) -> dict:
  """Compute derived timing metrics from a raw ClaimRecord dict."""
  if record.get('error') is not None:
    return {}

  req = record.get('claim_requested_at')
  ready = record.get('claim_ready_at')
  exec_start = record.get('exec_started_at')
  exec_end = record.get('exec_completed_at')
  released = record.get('released_at')
  pod_created = record.get('pod_created_at_utc')
  pod_scheduled = record.get('pod_scheduled_at_utc')
  pod_containers_ready = record.get('pod_containers_ready_at_utc')
  pod_ready = record.get('pod_ready_at_utc')
  pod_age = record.get('pod_age_at_bind_s')

  metrics = {}
  if req is not None and ready is not None:
    metrics['startup_time_s'] = ready - req
  if req is not None and exec_start is not None:
    metrics['time_to_first_exec_s'] = exec_start - req
  if exec_start is not None and exec_end is not None:
    metrics['exec_duration_s'] = exec_end - exec_start
  if req is not None and released is not None:
    metrics['total_lifecycle_s'] = released - req
  if pod_created is not None and pod_scheduled is not None:
    metrics['create_to_schedule_s'] = pod_scheduled - pod_created
  if pod_scheduled is not None and pod_containers_ready is not None:
    metrics['schedule_to_containers_ready_s'] = (
        pod_containers_ready - pod_scheduled
    )
  if pod_created is not None and pod_ready is not None:
    metrics['pod_startup_s'] = pod_ready - pod_created
  if pod_age is not None:
    metrics['pod_age_at_bind_s'] = pod_age
  return metrics


def _ParseLoadRunnerOutput(jsonl_content: str, metadata: dict) -> list:
  """Parse JSONL output from load_runner.py into PKB Sample objects.

  Each line is a serialised ClaimRecord (raw timestamp fields). Metrics are
  computed from the timestamps, then aggregated into p50/p90/p95/p99 Samples.
  """
  all_values = collections.defaultdict(list)

  for line in jsonl_content.strip().splitlines():
    line = line.strip()
    if not line:
      continue
    try:
      record = json.loads(line)
    except json.JSONDecodeError:
      logging.warning(
          'Skipping non-JSON line from load runner: %s', line[:120]
      )
      continue
    for metric_name, value in _ComputeMetrics(record).items():
      all_values[metric_name].append(value)

  samples = []
  for metric_name, values in all_values.items():
    if not values:
      continue
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    meta = dict(metadata)
    meta['sample_count'] = n
    for pct_label, pct_frac in (
        ('p50', 0.50), ('p90', 0.90), ('p95', 0.95), ('p99', 0.99)
    ):
      idx = min(int(n * pct_frac), n - 1)
      samples.append(
          sample.Sample(
              f'{metric_name}_{pct_label}',
              sorted_vals[idx],
              'seconds',
              dict(meta),
          )
      )
  return samples


def Cleanup(benchmark_spec):
  """Removes load runner Job, WarmPool, SandboxTemplate, and sandbox router."""
  del benchmark_spec

  # Delete namespaced resources explicitly.
  for resource in (
      f'job/{_LOAD_RUNNER_JOB}',
      f'configmap/{_LOAD_RUNNER_CONFIGMAP}',
      'sandboxwarmpool/python-sandbox-warmpool',
      f'sandboxtemplate/{_TEMPLATE_NAME}',
  ):
    kubectl.RunKubectlCommand(
        ['delete', resource, '-n', _NAMESPACE, '--ignore-not-found=true'],
        raise_on_failure=False,
        timeout=60,
    )

  # Sandbox router: delete via the manifest (namespace is embedded in it).
  kubernetes_commands.DeleteFromFile(
      data.ResourcePath('container/agent_sandbox/sandbox_router.yaml')
  )
