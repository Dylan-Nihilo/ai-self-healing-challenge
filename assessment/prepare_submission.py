"""Prepare an evaluation copy using trusted infrastructure and allowed solution files only."""
import argparse
import json
import secrets
import shutil
from pathlib import Path

from assessment.check_scope import MANIFEST, check, files, mutable


def prepare(baseline: Path, candidate: Path, destination: Path):
    if destination.is_relative_to(baseline) or destination.is_relative_to(candidate):
        raise ValueError("Evaluation copy must be outside the baseline and submission")
    baseline_check = check(baseline, baseline / MANIFEST)
    if not baseline_check["ok"]:
        raise ValueError("Trusted baseline no longer matches its manifest")
    result = check(candidate, baseline / MANIFEST)
    if not result["ok"]:
        return result
    if destination.exists():
        raise ValueError("Destination must not already exist")
    manifest = json.loads((baseline / MANIFEST).read_text())
    baseline_files, baseline_links = files(baseline)
    if baseline_links:
        raise ValueError("Trusted baseline contains symlinks")
    destination.mkdir(parents=True)
    for name in baseline_files:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(baseline / name, target)
    # Allowed deletions must also be reflected in the evaluation copy.
    for name in baseline_files:
        if mutable(name, manifest["mutable"]):
            (destination / name).unlink()
    candidate_files, _ = files(candidate)
    for name in candidate_files:
        if mutable(name, manifest["mutable"]):
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(candidate / name, target)
    config = destination / ".env"
    config.write_text("WORKBENCH_TOKEN=" + secrets.token_urlsafe(32) + "\n", encoding="utf-8")
    config.chmod(0o600)
    (destination / "compose.eval.yaml").write_text("services:\n  core:\n    ports: !reset []\n  frontend:\n    profiles: [review-ui]\n    ports: !reset []\n", encoding="utf-8")
    return {"ok": True, "prepared": str(destination), "note": "Use the trusted baseline and evaluation override; no model credentials were copied."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    result = prepare(args.baseline.resolve(), args.candidate.resolve(), args.destination.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
