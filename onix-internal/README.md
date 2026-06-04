# agent_sandbox benchmark: team cheat sheet

How to run the v2 `agent_sandbox` PKB benchmark against a GKE cluster, using the
`onix-internal/asb-bench.sh` wrapper. This directory (`onix-internal/`) is internal tooling: it is not
meant to be PR'd upstream, just shared across the team.

The wrapper stands up ONE cluster per `RUN_URI`, runs MANY tests against it, and
tears down once (mirrors the v1 `run.sh` ergonomics). Each `test` writes its own
output dir so runs never overwrite each other, and auto-generates a v1-style
`summary.md`.

---

## TL;DR

```bash
cd <repo-root>            # the dir with pkb.py; onix-internal/ lives under it
. .venv/bin/activate      # Python 3.12 venv (see Setup)

# stand up a fresh cluster + install the controller/warmpool (once)
RUN_URI=run1 ./onix-internal/asb-bench.sh up

# run a load test (workload-hold mode is the default; see Modes)
RUN_URI=run1 QPS=10 TOTAL=600 ./onix-internal/asb-bench.sh test

# read the result
cat ~/asb-runs/run1/<TEST_ID>/summary.md

# tear the cluster down when done
RUN_URI=run1 ./onix-internal/asb-bench.sh down
```

---

## Setup

Requirements on whatever host runs the wrapper:

- The repo, with a **Python 3.12** venv at `<repo-root>/.venv`.
  (3.11 fails: a module the benchmark imports uses 3.12-only f-string syntax, so
  every `pkb.py` invocation SyntaxErrors at import on 3.11.)

  ```bash
  uv python install 3.12
  uv venv --python 3.12 .venv      # or: python3.12 -m venv .venv
  . .venv/bin/activate
  pip install -r requirements.txt
  ```

- `kubectl` and `gke-gcloud-auth-plugin` on PATH.
- `gcloud` authenticated with an identity (user or service account) that has
  `roles/container.developer` on the project, so it can `get-credentials` and
  create/watch/delete SandboxClaims.

### Where to run it (this matters for the numbers)

In v2 the load generator runs **in the `pkb.py` process on the host** and talks
to the GKE API server directly (it is NOT an in-cluster Job like v1). So the
host's network path to the API server is part of what you measure.

Run it on a host with a fast, stable path to the control plane:

- **Best:** a small GCE VM in the same project/region as the cluster (in-VPC).
  A laptop on a flaky connection adds jitter to `submit_qps` / `startup_time`.
- The cluster's DNS control-plane endpoint must be reachable
  (`allowExternalTraffic: true`, master-authorized-networks empty or including
  the host). The wrapper fetches credentials with `--dns-endpoint` by default.

---

## Prepping a fresh cluster: warm the control plane first

A brand-new GKE cluster starts with a small control plane. GKE autoscales the
API server based on load, so if you run the benchmark immediately the first runs
hit an under-provisioned, still-scaling control plane and the numbers are not
representative.

The most reliable warmup is to run the benchmark itself a bunch of times with
`WORKLOAD_DURATION=0` (pure provisioning, no exec hold — each run takes about
a minute). After roughly 15 runs GKE has seen enough traffic to scale the
control plane and the numbers stabilize.

### Run it (from the VM)

```bash
cd ~/PerfKitBenchmarker-internal
for i in $(seq 1 15); do
  RUN_URI=<RUN_URI> STATIC_CLUSTER=pkb-<RUN_URI> \
    WORKLOAD_DURATION=0 QPS=10 TOTAL=600 \
    ./onix-internal/asb-bench.sh test
done
```

Each iteration runs ~60s of submissions plus drain time. The full loop takes
roughly 15-20 minutes. You can run fewer iterations and watch the metric below
to decide when to stop.

### Watch for the control plane to scale

Open Metrics Explorer with this PromQL query (this is the
[saved view](https://console.cloud.google.com/monitoring/metrics-explorer;duration=PT1H?pageState=%7B%22xyChart%22:%7B%22constantLines%22:%5B%5D,%22dataSets%22:%5B%7B%22plotType%22:%22LINE%22,%22pointConnectionMethod%22:%22GAP_DETECTION%22,%22prometheusQuery%22:%22sum(resets(apiserver_request_total%5B2m%5D))%5Cn%22,%22targetAxis%22:%22Y1%22,%22unitOverride%22:%22%22%7D%5D,%22options%22:%7B%22mode%22:%22COLOR%22%7D,%22y1Axis%22:%7B%22label%22:%22%22,%22scale%22:%22LINEAR%22%7D%7D%7D&project=ehole-benchmark-temp-z8s8)):

```promql
sum(resets(apiserver_request_total[2m]))
```

`apiserver_request_total` is a per-API-server-instance counter. When GKE resizes
the control plane it brings up new API server instances, and their counters
restart from zero. `resets()` detects those restarts, so a **blip (a spike above
zero) on this graph means the control plane just scaled**. That is your signal
that the cluster is ready to benchmark.

### Then proceed

Once you see the blip (or the loop finishes), run your actual benchmark tests
with the settings you care about:

```bash
RUN_URI=<RUN_URI> STATIC_CLUSTER=pkb-<RUN_URI> QPS=10 TOTAL=600 \
  ./onix-internal/asb-bench.sh test
```

---

## Verbs

| Verb        | What it does |
|-------------|--------------|
| `up`        | provision + prepare a new cluster (create cluster, node pools, install controller + warmpool) |
| `provision` | provision only (mint the PKB spec; no prepare) — used for adopting an existing cluster |
| `prepare`   | re-run prepare only (retune controller/warmpool knobs, no reprovision) |
| `test`      | run the load generator once → per-test output dir + `summary.md` |
| `down`      | cleanup + teardown the cluster |
| `bench`     | provision,prepare,run,cleanup,teardown in one shot |
| `claims`    | `kubectl delete sandboxclaims --all` in the namespace (clear orphans) |
| `summary`   | (re)generate `summary.md` from results.json (latest test, a `TEST_ID`, or `--all`) |

Any flag after the verb is forwarded to `pkb.py`, e.g.
`./onix-internal/asb-bench.sh test --agent_sandbox_qps=20`.

---

## Modes: workload hold vs pure provisioning

Controlled by `WORKLOAD_DURATION` (seconds), default **20**.

- **Mode A — workload hold (default, `WORKLOAD_DURATION=20`):** after each claim
  reports Ready, the driver execs a CPU busy-loop inside the sandbox pod (via
  `kubectl exec` through the API server) and holds it for the duration before
  releasing. This is what you want for a realistic run:
  - `peak_concurrency` reflects sandboxes alive at once (≈ `QPS × (startup + hold)`,
    so ~200 at QPS=10 / 20s), which exhausts the warm pool into cold provisioning.
  - `summary.md` regains the `exec_duration_s` and `total_lifecycle_s` rows
    alongside `startup_time_s` (close to the v1 table).

  ```bash
  RUN_URI=run1 QPS=10 TOTAL=600 WORKLOAD_DURATION=20 ./onix-internal/asb-bench.sh test
  ```

- **Pure provisioning (`WORKLOAD_DURATION=0`):** release each sandbox the instant
  it is Ready, no exec. Measures provisioning concurrency only; `peak_concurrency`
  is tiny (≈ `QPS × startup`, single digits). Use it to isolate control-plane
  provisioning without workload traffic.

  ```bash
  RUN_URI=run1 QPS=10 TOTAL=600 WORKLOAD_DURATION=0 ./onix-internal/asb-bench.sh test
  ```

> Measurement caveat: Mode A execs **through the API server**, so ~200 concurrent
> holds add control-plane connection load that a router-based path would not. Your
> `startup_time` (claim → Ready) is unaffected, but if you are isolating pure
> control-plane reconcile scaling, be aware of that confound.

---

## Reusing an existing cluster (adopt instead of provision)

If a cluster already exists (e.g. built by another tool, or a previous run you did
not tear down), do NOT re-provision — PKB is not idempotent and will fail trying to
recreate the cluster, node pools, and network. Instead adopt it.

The GKE cluster name is always `pkb-<RUN_URI>`. Set `STATIC_CLUSTER` to that name;
the wrapper marks the cluster (and its network/subnet) user-managed so provision
skips create/delete and just mints the spec PKB needs for the run stage.

```bash
# one-time: adopt the running cluster + mint the spec (no create, no prepare)
RUN_URI=<id> STATIC_CLUSTER=pkb-<id> ./onix-internal/asb-bench.sh provision

# then run as many tests as you like against it
RUN_URI=<id> STATIC_CLUSTER=pkb-<id> QPS=10 TOTAL=600 ./onix-internal/asb-bench.sh test
```

Notes:

- Pass `STATIC_CLUSTER` to `test` too, not just `provision`: the load generator
  reads the ambient kubeconfig, and the wrapper sets it up for the adopted cluster.
- Adopt mode does not re-prepare, so a controller + warmpool already installed on
  the cluster are left untouched.

---

## Output

Each `test` writes `~/asb-runs/<RUN_URI>/<TEST_ID>/`:

- `summary.md`   — v1-style summary that eric likes because it collects all the stuff he wants to know in one place: invocation knobs + the result block (claims,
  success/fail, peak concurrent, submit/completion qps, and the percentile table).
- `results.json` — raw PKB samples (NDJSON).
- `console.log`  — full stdout of the run.
- `pkb.log`      — verbose PKB log (copied from the run dir).
- `invocation.sh`— the exact knobs used, for reproducibility.

Regenerate a summary without re-running:

```bash
RUN_URI=run1 ./onix-internal/asb-bench.sh summary            # latest test
RUN_URI=run1 ./onix-internal/asb-bench.sh summary <TEST_ID>  # a specific test
RUN_URI=run1 ./onix-internal/asb-bench.sh summary --all      # every test under the run
```

---

## Common knobs (override via env)

| Env | Default | Meaning |
|-----|---------|---------|
| `RUN_URI` | `run1` | cluster id; GKE cluster is `pkb-<RUN_URI>` |
| `QPS` | `10` | target SandboxClaim submission rate |
| `TOTAL` | `600` | total claims to submit |
| `WORKLOAD_DURATION` | `20` | seconds to hold+exec each sandbox (0 = pure provisioning) |
| `STATIC_CLUSTER` | (unset) | adopt this existing cluster name instead of provisioning |
| `WARMPOOL_REPLICAS` | `150` | warm pool size |
| `SANDBOX_POOL_NODES` | `5` | sandbox node-pool size |
| `SANDBOX_POOL_MACHINE_TYPE` | `c4-standard-16` | sandbox node machine type |
| `CLAIM_WORKERS` / `SANDBOX_WORKERS` / `KUBE_API_BURST` | `100` / `60` / `600` | controller concurrency tuning |
| `PROJECT` / `ZONE` | `ehole-benchmark-temp-z8s8` / `us-central1-a` | GCP target |
| `NAMESPACE` | `default` | namespace for claims |

Full list is at the top of `onix-internal/asb-bench.sh`.
