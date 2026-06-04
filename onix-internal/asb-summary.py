#!/usr/bin/env python3
"""asb-summary.py — write a v1-style summary.md for an asb-bench test run.

Reads the PKB results.json (NDJSON) that asb-bench.sh writes and renders the
same summary.md layout the v1 run.sh produced: the invocation knobs plus the
human-readable "All claims completed ... Metric Summary (client-side)" block.

v2's load runner does not print that block itself, so we reconstruct it from
the published percentile/scalar samples. The table formatting (column widths,
the 80-char banner and 88-char rules) is copied from the v1 load runner so the
output is byte-for-byte the same shape.

Usage:
  asb-summary.py <test-dir|results.json>   # one test (writes summary.md alongside)
  asb-summary.py --all <run-dir>           # backfill every test dir under run-dir
"""

import argparse
import json
import sys
from pathlib import Path

_PCTLS = ("p50", "p90", "p95", "p99", "max")

# v1-ish row ordering; any other percentile families sort after these.
_ORDER = [
    "startup_time", "queue_wait", "provision_time",
    "time_to_first_exec", "exec_duration", "total_lifecycle",
]


def _load(results_path):
    samples = []
    with open(results_path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                samples.append(json.loads(line))
    return samples


def _labels(label_str):
    """PKB labels are |k:v|,|k:v|,... -> dict. Values may themselves contain ':'."""
    out = {}
    for chunk in (label_str or "").split("|,|"):
        chunk = chunk.strip("|")
        if ":" in chunk:
            k, v = chunk.split(":", 1)
            out[k] = v
    return out


def _by_metric(samples):
    return {s["metric"]: s for s in samples}


def _val(m, name):
    s = m.get(name)
    return s["value"] if s else None


def _pct_families(samples):
    """Group startup_time_p50/.../max style metrics into {base: {pctl: val, "unit": u}}."""
    fams = {}
    for s in samples:
        name = s["metric"]
        for p in _PCTLS:
            if name.endswith("_" + p):
                base = name[: -(len(p) + 1)]
                fam = fams.setdefault(base, {"unit": s.get("unit", "")})
                fam[p] = s["value"]
    return fams


def _row_label(base, unit):
    if unit == "seconds" and not base.endswith("_s"):
        return base + "_s"
    return base


def _percentile_table(samples):
    fams = _pct_families(samples)
    m = _by_metric(samples)
    succ = _val(m, "success_count")
    n = int(succ) if succ is not None else 0

    header = (
        f"{'metric':<32} {'N':>6} {'p50':>8} {'p90':>8} "
        f"{'p95':>8} {'p99':>8} {'max':>8}"
    )
    lines = [header, "-" * 88]
    bases = sorted(
        fams, key=lambda b: (_ORDER.index(b) if b in _ORDER else len(_ORDER), b)
    )
    for base in bases:
        fam = fams[base]
        label = _row_label(base, fam.get("unit", ""))
        cells = " ".join(f"{fam.get(p, 0.0):>8.3f}" for p in _PCTLS)
        lines.append(f"{label:<32} {n:>6} {cells}")
    lines.append("=" * 88)
    return "\n".join(lines)


def _results_block(samples):
    m = _by_metric(samples)
    succ = _val(m, "success_count")
    err = _val(m, "error_count")
    succ_i = int(succ) if succ is not None else 0
    fail_i = int(err) if err is not None else 0

    out = ["All claims completed. Generating summary...", "", "=" * 80]
    out.append(f"Total claims: {succ_i + fail_i}")
    out.append(f"Succeeded: {succ_i}")
    out.append(f"Failed: {fail_i}")
    peak = _val(m, "peak_concurrency")
    if peak is not None:
        out.append(f"Peak concurrent: {int(peak)}")
    sq = _val(m, "submit_qps")
    if sq is not None:
        out.append(f"submit_qps: {sq:.2f}")
    cq = _val(m, "completion_qps")
    if cq is not None:
        out.append(f"completion_qps: {cq:.2f}")

    # v1 prints a per-error-type breakdown only when there are failures.
    err_types = [
        (s["metric"][len("error_count_"):], int(s["value"]))
        for s in samples
        if s["metric"].startswith("error_count_") and s["value"]
    ]
    if err_types:
        out.append("")
        out.append("Failures by error type:")
        for name, count in sorted(err_types, key=lambda x: -x[1]):
            out.append(f"  {name}: {count}")

    out.append("")
    out.append("Metric Summary (client-side):")
    out.append(_percentile_table(samples))
    return "\n".join(out)


def _derive_invocation(labels):
    """Best-effort re-run block for old runs that have no captured invocation.sh."""
    candidates = [
        ("PROJECT", labels.get("project")),
        ("ZONE", labels.get("zone")),
        ("TEMPLATE", labels.get("template")),
        ("WARMPOOL_REPLICAS", labels.get("warmpool_replicas")),
        ("QPS", labels.get("target_qps")),
        ("TOTAL", labels.get("total_claims")),
    ]
    lines = ["# reconstructed from results labels (controller/cluster knobs not recorded)"]
    for k, v in candidates:
        if v and v != "None":
            lines.append(f"{k}={v} \\")
    lines.append("./asb-bench.sh test")
    return "\n".join(lines)


def summarize(target):
    target = Path(target)
    if target.is_dir():
        results, out_dir = target / "results.json", target
    else:
        results, out_dir = target, target.parent
    if not results.exists():
        print(f"skip {out_dir}: no results.json", file=sys.stderr)
        return None
    samples = _load(results)
    if not samples:
        print(f"skip {out_dir}: empty results.json", file=sys.stderr)
        return None

    labels = _labels(samples[0].get("labels", ""))
    benchmark = samples[0].get("test", "agent_sandbox")
    run_uri = samples[0].get("run_uri", "")
    test_id = labels.get("test_id", "") or out_dir.name

    inv_file = out_dir / "invocation.sh"
    if inv_file.exists():
        invocation = inv_file.read_text().rstrip("\n")
    else:
        invocation = _derive_invocation(labels)

    md = (
        f"# Run summary: {benchmark}\n\n"
        f"- test_id: {test_id}\n"
        f"- run_uri (cluster): {run_uri}\n\n"
        f"## Invocation\n\n```bash\n{invocation}\n```\n\n"
        f"## Results\n\n```\n{_results_block(samples)}\n```\n"
    )
    out_path = out_dir / "summary.md"
    out_path.write_text(md)
    return out_path


def main():
    ap = argparse.ArgumentParser(
        description="Write v1-style summary.md from asb-bench results.json"
    )
    ap.add_argument("path", help="test dir / results.json, or run dir with --all")
    ap.add_argument(
        "--all", action="store_true",
        help="treat path as a run dir; summarize every test dir under it",
    )
    args = ap.parse_args()

    if args.all:
        root = Path(args.path)
        wrote = False
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            out = summarize(d)
            if out:
                print(f"wrote {out}")
                wrote = True
        if not wrote:
            print(f"no summaries written under {root}", file=sys.stderr)
            sys.exit(1)
    else:
        out = summarize(args.path)
        if out:
            print(f"wrote {out}")
        else:
            sys.exit(1)


if __name__ == "__main__":
    main()
