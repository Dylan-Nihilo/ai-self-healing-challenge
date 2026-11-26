"""Compare a submission with an instructor-owned, frozen baseline manifest."""
import argparse
import fnmatch
import hashlib
import json
import os
from pathlib import Path


MANIFEST = "assessment/locked-files.json"
MUTABLE = ["solution/**", "tests/solution/**", "docs/DELIVERY.md", "docs/AI_COLLABORATION.md", ".env.models.example"]
IGNORED_DIRS = {".git", "node_modules", ".venv", "__pycache__", "dist", ".data", "test-results"}


def mutable(path, patterns=MUTABLE):
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def files(root):
    found, links = {}, []
    for directory, names, filenames in os.walk(root, followlinks=False):
        for name in list(names):
            path = Path(directory) / name
            if path.is_symlink():
                links.append(path.relative_to(root).as_posix()); names.remove(name)
            elif name in IGNORED_DIRS:
                names.remove(name)
        for name in filenames:
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                links.append(rel)
            elif name == ".DS_Store" or name.endswith((".pyc", ".pyo")) or (name.startswith(".env") and name not in {".env.example", ".env.models.example"}):
                continue
            else:
                found[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found, links


def check(candidate: Path, trusted_manifest: Path):
    manifest = json.loads(trusted_manifest.read_text(encoding="utf-8"))
    actual, links = files(candidate)
    problems = [{"path": name, "reason": "symlink_not_allowed"} for name in links]
    candidate_manifest = candidate / MANIFEST
    if not candidate_manifest.exists() or candidate_manifest.is_symlink() or candidate_manifest.read_bytes() != trusted_manifest.read_bytes():
        problems.append({"path": MANIFEST, "reason": "manifest_changed"})
    for name, expected in manifest["protected"].items():
        if actual.get(name) != expected:
            problems.append({"path": name, "reason": "protected_file_changed_or_missing"})
    for name in actual:
        if name != MANIFEST and name not in manifest["protected"] and not mutable(name, manifest["mutable"]):
            problems.append({"path": name, "reason": "outside_allowed_scope"})
    for name in manifest["required"]:
        if name not in actual:
            problems.append({"path": name, "reason": "required_entry_missing"})
    return {"ok": not problems, "problems": problems}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, default=Path("."))
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    result = check(args.candidate.resolve(), (args.manifest or args.candidate / MANIFEST).resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
