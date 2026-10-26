#!/usr/bin/env python3
"""Flatten Harbor job output so trials are grouped only by backend.

Harbor writes ``jobs/<backend>/<job-name>/<task>__<id>/``. This moves every
trial folder up to ``jobs/<backend>/<task>__<id>/`` and the job's own files
(config.json, result.json, job.log, lock.json) to
``jobs/<backend>/_job_logs/<job-name>/``. Job folders sitting directly under
``jobs/`` (older runs) are sorted into their backend first. Each trial's own
config.json records its backend and model, so nothing is lost.

Run it after each ``harbor run``; it is safe to run repeatedly.

Usage: python scripts/organize_jobs.py [--jobs jobs]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
from pathlib import Path

BACKENDS = ("browserbase", "kernel", "anchor")


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _is_job_dir(path: Path) -> bool:
    return "job_name" in _load(path / "config.json")


def _backend(job_dir: Path) -> str:
    text = (job_dir / "config.json").read_text(encoding="utf-8")
    if "KernelEnvironment" in text:
        return "kernel"
    if "AnchorEnvironment" in text:
        return "anchor"
    return "browserbase"


def _flatten(job_dir: Path, backend_dir: Path) -> int:
    moved = 0
    logs_dir = backend_dir / "_job_logs" / job_dir.name
    for child in sorted(job_dir.iterdir()):
        target = backend_dir / child.name if child.is_dir() else logs_dir / child.name
        if target.exists():
            print(f"  skip {child} (already exists at {target})")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(child), str(target))
        moved += child.is_dir()
    # OneDrive can hold a handle on a just-emptied folder; leave it if so.
    with contextlib.suppress(OSError):
        if not any(job_dir.iterdir()):
            job_dir.rmdir()
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=Path, default=Path("jobs"))
    args = parser.parse_args()
    jobs_dir: Path = args.jobs

    candidates: list[tuple[Path, Path]] = []
    for path in sorted(jobs_dir.iterdir()):
        if not path.is_dir():
            continue
        if _is_job_dir(path):
            candidates.append((path, jobs_dir / _backend(path)))
        else:
            # A grouping folder (browserbase/, kernel/, _failed/, ...).
            for job_dir in sorted(p for p in path.iterdir() if p.is_dir()):
                if _is_job_dir(job_dir):
                    candidates.append((job_dir, jobs_dir / _backend(job_dir)))

    for job_dir, backend_dir in candidates:
        count = _flatten(job_dir, backend_dir)
        print(f"{job_dir} -> {backend_dir}: {count} trials")

    for leftover in sorted(jobs_dir.iterdir()):
        if leftover.is_dir() and leftover.name not in BACKENDS:
            with contextlib.suppress(OSError):
                for empty in sorted(leftover.rglob("*"), reverse=True):
                    empty.rmdir()
                leftover.rmdir()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
