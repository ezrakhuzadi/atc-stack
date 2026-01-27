#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


EXCLUDED_DIR_NAMES = {
    ".git",
    "__pycache__",
    "node_modules",
    "target",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
}

FINDING_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("insecure_tls", re.compile(r"\bverify\s*=\s*False\b")),
    ("rust_unwrap", re.compile(r"\.(?:unwrap|expect)\(")),
    ("eval_exec", re.compile(r"\b(?:eval|exec)\s*\(")),
    ("todo", re.compile(r"\b(?:TODO|FIXME)\b")),
]

TODO_EXTS = {
    ".cjs",
    ".go",
    ".js",
    ".mjs",
    ".py",
    ".rs",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".yml",
    ".yaml",
}

EVAL_EXEC_EXTS = {".cjs", ".js", ".mjs", ".py", ".ts", ".tsx"}
INSECURE_TLS_EXTS = {".py"}
RUST_UNWRAP_EXTS = {".rs"}


@dataclass(frozen=True)
class Finding:
    kind: str
    pattern: str
    file: str
    line: int
    snippet: str


def sha256_hex(data: bytes) -> str:
    h = hashlib.sha256()
    h.update(data)
    return h.hexdigest()


def looks_binary(data: bytes) -> bool:
    return b"\0" in data


def iter_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIR_NAMES]
        for filename in filenames:
            yield Path(dirpath) / filename


def file_ext_key(path: Path) -> str:
    if path.name == "Dockerfile":
        return "Dockerfile"
    return path.suffix


def scan_file(path: Path) -> tuple[dict, list[Finding]]:
    data = path.read_bytes()
    is_binary = looks_binary(data)
    file_record = {
        "path": str(path),
        "size": len(data),
        "sha256": sha256_hex(data),
        "binary": is_binary,
    }

    findings: list[Finding] = []
    if is_binary:
        return file_record, findings

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return file_record, findings

    ext = path.suffix.lower()

    for line_idx, line in enumerate(text.splitlines(), start=1):
        for pattern_name, pattern in FINDING_PATTERNS:
            if pattern_name == "rust_unwrap" and ext not in RUST_UNWRAP_EXTS:
                continue
            if pattern_name == "eval_exec" and ext not in EVAL_EXEC_EXTS:
                continue
            if pattern_name == "insecure_tls" and ext not in INSECURE_TLS_EXTS:
                continue
            if pattern_name == "todo" and ext not in TODO_EXTS:
                continue
            if not pattern.search(line):
                continue
            kind = "secret" if pattern_name == "private_key" else "risk"
            findings.append(
                Finding(
                    kind=kind,
                    pattern=pattern_name,
                    file=str(path),
                    line=line_idx,
                    snippet=line.strip()[:500],
                )
            )

    return file_record, findings


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Full-file ship-readiness scan (reads every file).")
    parser.add_argument(
        "root",
        nargs="?",
        default=str(Path(__file__).resolve().parents[1]),
        help="Root directory to scan (default: repo root)",
    )
    parser.add_argument(
        "--out",
        default="ship_audit_scan.json",
        help="Output JSON path (default: ship_audit_scan.json in CWD)",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    out_path = Path(args.out).resolve()

    files: list[dict] = []
    findings: list[Finding] = []
    ext_counts: dict[str, int] = {}
    total_bytes = 0

    for path in sorted(iter_files(root)):
        if not path.is_file():
            continue
        if path.resolve() == out_path:
            continue
        record, file_findings = scan_file(path)
        files.append(record)
        total_bytes += record["size"]
        key = file_ext_key(path)
        ext_counts[key] = ext_counts.get(key, 0) + 1
        findings.extend(file_findings)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "roots": [str(root)],
        "total_files": len(files),
        "total_bytes": total_bytes,
        "ext_counts": dict(sorted(ext_counts.items(), key=lambda kv: (kv[0] == "", kv[0]))),
        "files": files,
        "findings": [f.__dict__ for f in findings],
    }

    out_path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"ok: wrote {out_path} ({len(files)} files, {total_bytes} bytes, {len(findings)} findings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
