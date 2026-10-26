#!/usr/bin/env python3
"""Flatten Harbor job output into one trace file per (backend, task).

    traces/
      summary.csv                     one row per task: reward per backend
      browserbase/<task>.json         best valid trace for that task
      kernel/<task>.json
      anchor/<task>.json
      browserbase/screenshots/<task>/ only with --screenshots

Each exported JSON holds the Harbor reward, the judge verdict, run metadata and
the full Stagehand trajectory. A trace is valid when the agent took at least one
step and consumed tokens; otherwise (for example when the model API refused every
call) it is skipped. When a task has several valid traces for a backend, the most
recent one wins. The jobs/ directory is only read, never modified.

Usage: python scripts/export_traces.py [--jobs jobs] [--out traces] [--screenshots]
                                       [--model glm-5.3-flash]
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import shutil
from pathlib import Path


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _backend(job_config: str) -> str:
    if "KernelEnvironment" in job_config:
        return "kernel"
    if "AnchorEnvironment" in job_config:
        return "anchor"
    return "browserbase"


def _model(job: dict) -> str:
    agents = job.get("agents") or [{}]
    return str(agents[0].get("model_name") or "")


def _is_valid(trajectory: dict) -> bool:
    usage = trajectory.get("usage") or {}
    tokens = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
    return bool(trajectory.get("steps")) and tokens > 0


def collect(jobs_dir: Path, model_filter: str = "") -> dict[tuple[str, str], dict]:
    best: dict[tuple[str, str], dict] = {}
    for job_dir in sorted(p for p in jobs_dir.iterdir() if p.is_dir()):
        config_path = job_dir / "config.json"
        if not config_path.exists():
            continue
        config_text = config_path.read_text(encoding="utf-8")
        backend = _backend(config_text)
        model = _model(json.loads(config_text))
        if model_filter and model_filter not in model:
            continue
        for trial_dir in sorted(p for p in job_dir.iterdir() if p.is_dir()):
            result = _load(trial_dir / "result.json") or {}
            for traj_path in trial_dir.rglob("trajectory.json"):
                trajectory = _load(traj_path)
                if not trajectory or not _is_valid(trajectory):
                    continue
                task = str(result.get("task_name") or trial_dir.name.split("__")[0])
                task = task.split("/")[-1]
                run_dir = traj_path.parent
                rewards = (result.get("verifier_result") or {}).get("rewards") or {}
                record = {
                    "task": task,
                    "backend": backend,
                    "model": model,
                    "reward": rewards.get("reward"),
                    "error": (result.get("exception_info") or {}).get("exception_type"),
                    "job": job_dir.name,
                    "trial": trial_dir.name,
                    "started_at": result.get("started_at"),
                    "judge": _load(run_dir / "scores" / "result.json"),
                    "trajectory": trajectory,
                    "_run_dir": run_dir,
                }
                key = (backend, task)
                current = best.get(key)
                if current is None or str(record["started_at"]) > str(
                    current["started_at"]
                ):
                    best[key] = record
    return best


def export(best: dict[tuple[str, str], dict], out_dir: Path, screenshots: bool) -> None:
    # Clear files rather than the directories themselves: OneDrive and open
    # editors on Windows hold directory handles that make rmtree fail.
    if out_dir.exists():
        for path in sorted(out_dir.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif not any(path.iterdir()):
                with contextlib.suppress(OSError):
                    path.rmdir()
    for (backend, task), record in sorted(best.items()):
        backend_dir = out_dir / backend
        backend_dir.mkdir(parents=True, exist_ok=True)
        run_dir = record.pop("_run_dir")
        (backend_dir / f"{task}.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        if screenshots and (run_dir / "screenshots").is_dir():
            shutil.copytree(
                run_dir / "screenshots", backend_dir / "screenshots" / task
            )

    backends = sorted({backend for backend, _ in best})
    tasks = sorted({task for _, task in best})
    with open(out_dir / "summary.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["task"]
            + [f"{b}_reward" for b in backends]
            + [f"{b}_model" for b in backends]
        )
        for task in tasks:
            rows = [best.get((b, task)) for b in backends]
            writer.writerow(
                [task]
                + ["" if r is None else r["reward"] for r in rows]
                + ["" if r is None else r["model"] for r in rows]
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=Path, default=Path("jobs"))
    parser.add_argument("--out", type=Path, default=Path("traces"))
    parser.add_argument("--screenshots", action="store_true")
    parser.add_argument(
        "--model", default="", help="only use jobs whose model contains this text"
    )
    args = parser.parse_args()

    best = collect(args.jobs, args.model)
    export(best, args.out, args.screenshots)
    for backend in sorted({b for b, _ in best}):
        count = sum(1 for b, _ in best if b == backend)
        print(f"{backend}: {count} tasks")
    print(f"wrote {args.out / 'summary.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
