#!/usr/bin/env python3
# Copyright 2025 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Load runner for Agent Sandbox benchmark iteration step 1.

Generates claims at a steady rate (default 10/sec), measures per-claim
startup/exec/lifecycle metrics, and outputs JSONL + summary statistics.

Connection modes:
- tunnel: Uses kubectl port-forward (default). At 200 concurrent, may hit
  file descriptor or kubectl-process limits. Good for local dev.
- direct: Talks directly to sandbox-router-svc:8080. More robust at scale
  but requires running from inside the cluster (or exposed service).
- gateway: Uses Gateway resource discovery.

Metrics captured (spec §3.1):
- startup_time_s: claim_requested → sandbox ready
- time_to_first_exec_s: SDK overhead after ready, before commands.run()
- exec_duration_s: exec started → exec completed
- total_lifecycle_s: claim_requested → released
- create_to_schedule_s: pod created → PodScheduled condition (server-side)
- schedule_to_containers_ready_s: PodScheduled → ContainersReady (server-side)
- pod_startup_s: pod created → Ready condition (server-side)
"""

import argparse
import json
import logging
import shlex
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import threading
from threading import Lock
from typing import Optional

import kubernetes
from tqdm import tqdm

from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import (
    SandboxDirectConnectionConfig,
    SandboxGatewayConnectionConfig,
    SandboxLocalTunnelConnectionConfig,
    SandboxTracerConfig,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)

_POD_NAME_ANNOTATION = "agents.x-k8s.io/pod-name"


def _configure_k8s_pool(pool_maxsize: int):
    """Force urllib3 pool size on the kubernetes client across SDK init.

    The SDK's K8sHelper.__init__ calls kubernetes.config.load_incluster_config()
    which replaces the default Configuration with a fresh one — so simply
    pre-setting the default here gets clobbered before CoreV1Api/CustomObjectsApi
    are constructed. We wrap both config loaders so they re-apply pool_maxsize
    on the new default after they run, before the SDK constructs its API clients.
    """
    import kubernetes.config as _k8s_config

    def _apply_pool():
        cfg = kubernetes.client.Configuration.get_default_copy()
        cfg.connection_pool_maxsize = pool_maxsize
        # kubernetes>=36 stores the in-cluster token under api_key['authorization']
        # but auth_settings() looks for api_key['BearerToken']. Copy it over so
        # the generated API clients (CustomObjectsApi etc.) include the Bearer header.
        if 'authorization' in cfg.api_key and 'BearerToken' not in cfg.api_key:
            cfg.api_key['BearerToken'] = cfg.api_key['authorization']
        kubernetes.client.Configuration.set_default(cfg)

    for fn_name in ("load_incluster_config", "load_kube_config"):
        orig = getattr(_k8s_config, fn_name)
        if getattr(orig, "_pool_patched", False):
            continue

        def _wrapped(*a, _orig=orig, **kw):
            _orig(*a, **kw)
            _apply_pool()

        _wrapped._pool_patched = True
        setattr(_k8s_config, fn_name, _wrapped)

    _apply_pool()


@dataclass
class ClaimRecord:
    """Per-claim lifecycle measurement."""

    claim_name: str
    claim_requested_at: float
    claim_ready_at: Optional[float] = None
    exec_started_at: Optional[float] = None
    exec_completed_at: Optional[float] = None
    released_at: Optional[float] = None
    error: Optional[str] = None

    # UTC wall-clock at the moment claim_ready_at monotonic was captured.
    claim_ready_at_utc: Optional[float] = None

    # K8s server-side Pod identifiers.
    pod_name: Optional[str] = None
    pod_node_name: Optional[str] = None

    # K8s server-side timestamps (seconds since epoch, UTC).
    pod_created_at_utc: Optional[float] = None
    pod_scheduled_at_utc: Optional[float] = None
    pod_initialized_at_utc: Optional[float] = None
    pod_containers_ready_at_utc: Optional[float] = None
    pod_ready_at_utc: Optional[float] = None

    # Seconds between pod creation and claim ready — positive means warm pool reuse.
    pod_age_at_bind_s: Optional[float] = None

    @property
    def startup_time_s(self) -> Optional[float]:
        if self.claim_ready_at is None:
            return None
        return self.claim_ready_at - self.claim_requested_at

    @property
    def time_to_first_exec_s(self) -> Optional[float]:
        """SDK setup time between sandbox-ready and the first commands.run() call."""
        if self.exec_started_at is None:
            return None
        return self.exec_started_at - self.claim_requested_at

    @property
    def exec_duration_s(self) -> Optional[float]:
        if self.exec_started_at is None or self.exec_completed_at is None:
            return None
        return self.exec_completed_at - self.exec_started_at

    @property
    def total_lifecycle_s(self) -> Optional[float]:
        if self.released_at is None:
            return None
        return self.released_at - self.claim_requested_at

    @property
    def create_to_schedule_s(self) -> Optional[float]:
        if self.pod_created_at_utc is None or self.pod_scheduled_at_utc is None:
            return None
        return self.pod_scheduled_at_utc - self.pod_created_at_utc

    @property
    def schedule_to_containers_ready_s(self) -> Optional[float]:
        if (
            self.pod_scheduled_at_utc is None
            or self.pod_containers_ready_at_utc is None
        ):
            return None
        return self.pod_containers_ready_at_utc - self.pod_scheduled_at_utc

    @property
    def pod_startup_s(self) -> Optional[float]:
        if self.pod_created_at_utc is None or self.pod_ready_at_utc is None:
            return None
        return self.pod_ready_at_utc - self.pod_created_at_utc


def _ts(dt) -> Optional[float]:
    """Convert a kubernetes datetime object or ISO string to epoch float (UTC)."""
    if dt is None:
        return None
    if hasattr(dt, "timestamp"):
        return (
            dt.replace(tzinfo=timezone.utc).timestamp()
            if dt.tzinfo is None
            else dt.timestamp()
        )
    try:
        from datetime import datetime as _dt

        parsed = _dt.fromisoformat(str(dt).replace("Z", "+00:00"))
        return parsed.timestamp()
    except Exception:
        return None


def _collect_pod_metrics(sandbox, namespace: str, record: ClaimRecord):
    """Fetch Pod condition timestamps from the K8s API and populate record in-place.

    Uses sandbox.k8s_helper (already authenticated) to GET the Sandbox CR,
    reads the agents.x-k8s.io/pod-name annotation (sandbox.py:94), then
    calls core_v1_api.read_namespaced_pod() for condition timestamps.

    Fails silently: on any error, leaves new fields as None.
    """
    try:
        sandbox_obj = (
            sandbox.k8s_helper.get_sandbox(sandbox.sandbox_id, namespace) or {}
        )
        annotations = (sandbox_obj.get("metadata") or {}).get("annotations") or {}
        pod_name = annotations.get(_POD_NAME_ANNOTATION) or sandbox.sandbox_id
        record.pod_name = pod_name

        pod = sandbox.k8s_helper.core_v1_api.read_namespaced_pod(
            name=pod_name, namespace=namespace
        )
        record.pod_node_name = pod.spec.node_name
        record.pod_created_at_utc = _ts(pod.metadata.creation_timestamp)

        condition_map = {}
        for cond in pod.status.conditions or []:
            if cond.status == "True":
                condition_map[cond.type] = _ts(cond.last_transition_time)

        record.pod_scheduled_at_utc = condition_map.get("PodScheduled")
        record.pod_initialized_at_utc = condition_map.get("Initialized")
        record.pod_containers_ready_at_utc = condition_map.get("ContainersReady")
        record.pod_ready_at_utc = condition_map.get("Ready")

        if (
            record.pod_created_at_utc is not None
            and record.claim_ready_at_utc is not None
        ):
            record.pod_age_at_bind_s = (
                record.claim_ready_at_utc - record.pod_created_at_utc
            )

    except Exception as exc:
        logger.warning(f"Pod metrics collection failed for {record.claim_name}: {exc}")


class LoadRunner:
    def __init__(
        self,
        template_name: str,
        namespace: str,
        qps: float,
        total: int,
        duration: float,
        max_concurrent: int,
        workload_duration: int,
        connection_mode: str,
        router_url: str,
        output_path: Path,
        dry_run: bool,
        sandbox_ready_timeout: int,
        collect_pod_metrics: bool,
        progress_interval: float = 5.0,
    ):
        self.template_name = template_name
        self.namespace = namespace
        self.qps = qps
        self.total = total
        self.duration = duration
        self.max_concurrent = max_concurrent
        self.workload_duration = workload_duration
        self.connection_mode = connection_mode
        self.router_url = router_url
        self.output_path = output_path
        self.dry_run = dry_run
        self.sandbox_ready_timeout = sandbox_ready_timeout
        self.collect_pod_metrics = collect_pod_metrics
        self.progress_interval = progress_interval

        self.records: list[ClaimRecord] = []
        self.records_lock = Lock()
        self.in_flight = 0
        self.submitted = 0
        self.in_flight_lock = Lock()
        self.peak_concurrent = 0

    def _create_client(self) -> SandboxClient:
        """Create SandboxClient with appropriate connection config."""
        if self.connection_mode == "tunnel":
            connection_config = SandboxLocalTunnelConnectionConfig(server_port=8888)
        elif self.connection_mode == "direct":
            connection_config = SandboxDirectConnectionConfig(
                api_url=self.router_url,
                server_port=8888,
            )
        elif self.connection_mode == "gateway":
            connection_config = SandboxGatewayConnectionConfig(
                gateway_name="sandbox-gateway",
                server_port=8888,
            )
        else:
            raise ValueError(f"Unknown connection mode: {self.connection_mode}")

        tracer_config = SandboxTracerConfig(
            enable_tracing=False,
            trace_service_name="load-runner-step1",
        )

        return SandboxClient(
            connection_config=connection_config,
            tracer_config=tracer_config,
            cleanup=False,
        )

    def _workload_code(self) -> str:
        """Generate the Python busy-loop code to run in each sandbox."""
        inner_code = f"""
import time
start = time.monotonic()
count = 0
while time.monotonic() - start < {self.workload_duration}:
    result = (count * 2) + (count ** 2)
    count += 1
print(f"iterations={{count}}")
""".strip()
        return f"python3 -c {shlex.quote(inner_code)}"

    def _run_claim(
        self, client: SandboxClient, claim_idx: int, requested_at: float
    ) -> ClaimRecord:
        """Run full lifecycle for one claim."""
        record = ClaimRecord(
            claim_name=f"claim-{claim_idx}",
            claim_requested_at=requested_at,
        )

        sandbox = None
        try:
            with self.in_flight_lock:
                self.in_flight += 1
                self.peak_concurrent = max(self.peak_concurrent, self.in_flight)

            sandbox = client.create_sandbox(
                self.template_name,
                namespace=self.namespace,
                sandbox_ready_timeout=self.sandbox_ready_timeout,
            )
            record.claim_name = sandbox.claim_name

            # sandbox.status() re-queries the cluster on every call (sandbox.py:137).
            # We call it here to confirm readiness; claim_ready_at captures both
            # the wait-for-ready time and this one extra GET.
            status, msg = sandbox.status()
            if status != "SandboxReady":
                raise RuntimeError(f"Sandbox not ready: {status} - {msg}")
            record.claim_ready_at = time.monotonic()
            record.claim_ready_at_utc = datetime.now(timezone.utc).timestamp()

            if self.collect_pod_metrics:
                _collect_pod_metrics(sandbox, self.namespace, record)

            workload_cmd = self._workload_code()
            # exec_started_at is set here — after pod-metrics fetch and workload
            # code generation — to isolate the actual commands.run() round-trip.
            record.exec_started_at = time.monotonic()
            result = sandbox.commands.run(workload_cmd)
            record.exec_completed_at = time.monotonic()

            if result.exit_code != 0:
                raise RuntimeError(
                    f"Workload failed (exit {result.exit_code}): {result.stderr}"
                )

            sandbox.terminate()
            record.released_at = time.monotonic()

        except Exception as e:
            record.error = f"{type(e).__name__}: {str(e)}"
            logger.warning(f"Claim {record.claim_name} failed: {record.error}")
        finally:
            if sandbox is not None and record.released_at is None:
                try:
                    sandbox.terminate()
                except Exception as cleanup_exc:
                    logger.warning(
                        f"Cleanup terminate failed for {record.claim_name}: {cleanup_exc}"
                    )
            with self.in_flight_lock:
                self.in_flight -= 1

        self._write_record(record)
        return record

    def _write_record(self, record: ClaimRecord):
        """Append JSONL record to output file."""
        with self.records_lock:
            self.records.append(record)
            if not self.dry_run:
                with open(self.output_path, "a") as f:
                    f.write(json.dumps(asdict(record)) + "\n")

    def run(self):
        """Execute the load test."""
        if self.dry_run:
            logger.info("=== DRY RUN ===")
            logger.info(f"Would create {self.total} claims at {self.qps} claims/sec")
            logger.info(f"Creation window: {self.duration}s")
            logger.info(f"Max concurrent: {self.max_concurrent}")
            logger.info(f"Per-claim workload: {self.workload_duration}s busy-loop")
            logger.info(f"Connection mode: {self.connection_mode}")
            if self.connection_mode == "direct":
                logger.info(f"Router URL: {self.router_url}")
            logger.info(f"Template: {self.template_name} (namespace: {self.namespace})")
            logger.info(f"Sandbox-ready timeout: {self.sandbox_ready_timeout}s")
            logger.info(f"Collect pod metrics: {self.collect_pod_metrics}")
            logger.info(f"Output: {self.output_path}")
            return

        logger.info(f"Starting load test: {self.total} claims @ {self.qps} qps")
        logger.info(f"Template: {self.template_name}, namespace: {self.namespace}")
        logger.info(f"Connection mode: {self.connection_mode}")
        logger.info(f"Collect pod metrics: {self.collect_pod_metrics}")
        logger.info(f"Output: {self.output_path}")

        client = self._create_client()
        inter_claim_delay = 1.0 / self.qps

        stop_ticker = threading.Event()
        run_start = time.monotonic()

        def _ticker():
            phase = "submit"
            while not stop_ticker.wait(self.progress_interval):
                now = time.monotonic()
                elapsed = now - run_start

                with self.records_lock:
                    records_copy = list(self.records)
                with self.in_flight_lock:
                    in_flight = self.in_flight
                    submitted = self.submitted

                completed = len(records_copy)
                errors = sum(1 for r in records_copy if r.error is not None)

                if completed > 0:
                    first_req = records_copy[0].claim_requested_at
                    qps_val = (
                        completed / (now - first_req) if (now - first_req) > 0 else 0.0
                    )
                else:
                    qps_val = 0.0

                success_records = [r for r in records_copy if r.error is None]
                startup_vals = sorted(
                    r.startup_time_s
                    for r in success_records
                    if r.startup_time_s is not None
                )
                lifecycle_vals = sorted(
                    r.total_lifecycle_s
                    for r in success_records
                    if r.total_lifecycle_s is not None
                )

                def pct(vals, p):
                    if not vals:
                        return 0.0
                    return vals[int(len(vals) * p)]

                logger.info(
                    f"[t={elapsed:.0f}s] phase={phase}"
                    f"  submitted={submitted}/{self.total}"
                    f"  completed={completed}"
                    f"  in_flight={in_flight}"
                    f"  errors={errors}"
                    f"  qps={qps_val:.1f}"
                    f"  startup_p50={pct(startup_vals, 0.50):.2f}s"
                    f"  startup_p95={pct(startup_vals, 0.95):.2f}s"
                    f"  lifecycle_p50={pct(lifecycle_vals, 0.50):.2f}s"
                )

        ticker_thread = threading.Thread(target=_ticker, daemon=True)

        start_time = time.monotonic()
        ticker_thread.start()
        try:
            with ThreadPoolExecutor(max_workers=self.max_concurrent + 10) as executor:
                futures = []
                phase = "submit"
                for i in range(self.total):
                    next_claim_time = start_time + (i * inter_claim_delay)
                    now = time.monotonic()
                    sleep_time = next_claim_time - now
                    if sleep_time > 0:
                        time.sleep(sleep_time)

                    requested_at = time.monotonic()
                    future = executor.submit(self._run_claim, client, i, requested_at)
                    futures.append(future)

                    with self.in_flight_lock:
                        self.submitted += 1

                    if (i + 1) % 50 == 0:
                        logger.info(f"Submitted {i + 1}/{self.total} claims")

                phase = "drain"
                logger.info(
                    f"All {self.total} claims submitted. Waiting for completion..."
                )

                bar = tqdm(
                    as_completed(futures),
                    total=self.total,
                    desc="claims",
                    unit="claim",
                    file=sys.stderr,
                    disable=not sys.stderr.isatty(),
                )
                for future in bar:
                    record = future.result()
                    with self.records_lock:
                        completed = len(self.records)
                        errors = sum(1 for r in self.records if r.error is not None)
                    with self.in_flight_lock:
                        in_flight = self.in_flight
                    bar.set_postfix(
                        in_flight=in_flight, errors=errors, ok=completed - errors
                    )
        finally:
            stop_ticker.set()

        logger.info("All claims completed. Generating summary...")
        self._print_summary()

    def _print_summary(self):
        """Print summary statistics table."""
        success = [r for r in self.records if r.error is None]
        failures = [r for r in self.records if r.error is not None]

        logger.info("\n" + "=" * 80)
        logger.info(f"Total claims: {len(self.records)}")
        logger.info(f"Success: {len(success)}")
        logger.info(f"Failures: {len(failures)}")
        logger.info(f"Peak concurrent: {self.peak_concurrent}")

        if self.records:
            first_request = min(r.claim_requested_at for r in self.records)
            last_request = max(r.claim_requested_at for r in self.records)
            actual_duration = last_request - first_request
            achieved_qps = (
                len(self.records) / actual_duration if actual_duration > 0 else 0
            )
            logger.info(f"Achieved QPS: {achieved_qps:.2f}")

        if failures:
            logger.info("\nFailures by error type:")
            error_counts = Counter(r.error for r in failures)
            for error, count in error_counts.most_common():
                logger.info(f"  {error}: {count}")

        if not success:
            logger.warning("No successful claims to summarize.")
            return

        metrics = {
            "startup_time_s": [
                r.startup_time_s for r in success if r.startup_time_s is not None
            ],
            "time_to_first_exec_s": [
                r.time_to_first_exec_s
                for r in success
                if r.time_to_first_exec_s is not None
            ],
            "exec_duration_s": [
                r.exec_duration_s for r in success if r.exec_duration_s is not None
            ],
            "total_lifecycle_s": [
                r.total_lifecycle_s for r in success if r.total_lifecycle_s is not None
            ],
            "pod_startup_s": [
                r.pod_startup_s for r in success if r.pod_startup_s is not None
            ],
            "create_to_schedule_s": [
                r.create_to_schedule_s
                for r in success
                if r.create_to_schedule_s is not None
            ],
            "schedule_to_containers_ready_s": [
                r.schedule_to_containers_ready_s
                for r in success
                if r.schedule_to_containers_ready_s is not None
            ],
            "pod_age_at_bind_s": [
                r.pod_age_at_bind_s for r in success if r.pod_age_at_bind_s is not None
            ],
        }

        logger.info("\nMetric Summary:")
        logger.info(
            f"{'metric':<32} {'N':>6} {'p50':>8} {'p90':>8} {'p95':>8} {'p99':>8} {'max':>8}"
        )
        logger.info("-" * 88)

        for metric_name, values in metrics.items():
            if not values:
                continue
            values_sorted = sorted(values)
            n = len(values_sorted)
            p50 = values_sorted[int(n * 0.50)] if n > 0 else 0
            p90 = values_sorted[int(n * 0.90)] if n > 0 else 0
            p95 = values_sorted[int(n * 0.95)] if n > 0 else 0
            p99 = values_sorted[int(n * 0.99)] if n > 0 else 0
            max_val = values_sorted[-1] if n > 0 else 0

            logger.info(
                f"{metric_name:<32} {n:>6} {p50:>8.3f} {p90:>8.3f} {p95:>8.3f} {p99:>8.3f} {max_val:>8.3f}"
            )

        logger.info("=" * 88)


def main():
    parser = argparse.ArgumentParser(
        description="Load runner for Agent Sandbox benchmark step 1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--template-name",
        default="python-runtime",
        help="SandboxTemplate name (default: python-runtime-)",
    )
    parser.add_argument(
        "--namespace",
        default="default",
        help="Kubernetes namespace (default: default)",
    )
    parser.add_argument(
        "--qps",
        type=float,
        default=10.0,
        help="Claims per second (default: 10)",
    )
    parser.add_argument(
        "--total",
        type=int,
        default=600,
        help="Total claims to create (default: 600)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=60.0,
        help="Creation window duration in seconds (default: 60)",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=200,
        help="Max concurrent claims (default: 200)",
    )
    parser.add_argument(
        "--workload-duration",
        type=int,
        default=20,
        help="Per-sandbox workload duration in seconds (default: 20)",
    )
    parser.add_argument(
        "--connection-mode",
        choices=["tunnel", "direct", "gateway"],
        default="tunnel",
        help="Connection mode (default: tunnel)",
    )
    parser.add_argument(
        "--router-url",
        default="http://sandbox-router-svc:8080",
        help="Sandbox router URL for direct mode (default: http://sandbox-router-svc:8080)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output JSONL path (default: ./run-step1-<timestamp>.jsonl)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan without executing",
    )
    parser.add_argument(
        "--sandbox-ready-timeout",
        type=int,
        default=180,
        help="SDK sandbox-ready watch timeout in seconds (default: 180, matches SDK default)",
    )
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=5.0,
        help="Seconds between progress log lines (default: 5.0)",
    )
    parser.add_argument(
        "--collect-pod-metrics",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="Fetch K8s Pod condition timestamps after sandbox ready (default: True)",
    )

    args = parser.parse_args()

    if args.output is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        args.output = Path(f"./run-step1-{timestamp}.jsonl")

    _configure_k8s_pool(max(args.max_concurrent + 50, 250))

    runner = LoadRunner(
        template_name=args.template_name,
        namespace=args.namespace,
        qps=args.qps,
        total=args.total,
        duration=args.duration,
        max_concurrent=args.max_concurrent,
        workload_duration=args.workload_duration,
        connection_mode=args.connection_mode,
        router_url=args.router_url,
        output_path=args.output,
        dry_run=args.dry_run,
        sandbox_ready_timeout=args.sandbox_ready_timeout,
        collect_pod_metrics=args.collect_pod_metrics,
        progress_interval=args.progress_interval,
    )

    runner.run()


if __name__ == "__main__":
    main()
