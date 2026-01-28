#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_TARGET_SECONDS = 90 * 60


def now_epoch() -> int:
    return int(time.time())


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def default_log_path() -> Path:
    return Path(__file__).resolve().parents[1] / ".timed_audit_clock.json"


def _locked_open_rw(path: Path):
    # Cross-process lock to avoid corruption if multiple agents/tools touch the file.
    # Linux-only (ok for this environment).
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open("a+", encoding="utf-8")
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
    f.seek(0)
    return f


def load_state(path: Path) -> dict[str, Any]:
    f = _locked_open_rw(path)
    try:
        raw = f.read()
        if not raw.strip():
            return {
                "version": 1,
                "target_seconds_per_section": DEFAULT_TARGET_SECONDS,
                "active": None,
                "sections": {},
            }
        return json.loads(raw)
    finally:
        f.close()


def save_state(path: Path, state: dict[str, Any]) -> None:
    f = _locked_open_rw(path)
    try:
        f.seek(0)
        f.truncate()
        f.write(json.dumps(state, indent=2, sort_keys=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    finally:
        f.close()


def ensure_section(state: dict[str, Any], section: str) -> dict[str, Any]:
    sections = state.setdefault("sections", {})
    if section not in sections:
        sections[section] = {
            "target_seconds": int(state.get("target_seconds_per_section") or DEFAULT_TARGET_SECONDS),
            "elapsed_seconds": 0,
            "segments": [],
        }
    if "target_seconds" not in sections[section]:
        sections[section]["target_seconds"] = int(state.get("target_seconds_per_section") or DEFAULT_TARGET_SECONDS)
    if "elapsed_seconds" not in sections[section]:
        sections[section]["elapsed_seconds"] = 0
    if "segments" not in sections[section]:
        sections[section]["segments"] = []
    return sections[section]


def active_effective_elapsed(state: dict[str, Any], now: int) -> int:
    active = state.get("active")
    if not active:
        return 0
    start = int(active.get("started_epoch") or now)
    last_tick = int(active.get("last_tick_epoch") or start)
    if last_tick < start:
        return 0
    # "Effective" elapsed is time we have explicitly marked as active via tick.
    # This prevents counting idle time if a turn was interrupted.
    return last_tick - start


def cmd_start(state: dict[str, Any], section: str, note: str | None, now: int) -> None:
    active = state.get("active")
    if active is not None:
        raise SystemExit(
            f"error: timer already running for section '{active.get('section')}'. "
            f"Run 'pause' first (or 'status' to inspect)."
        )
    ensure_section(state, section)
    state["active"] = {
        "section": section,
        "started_epoch": now,
        "started_at": now_iso(),
        "last_tick_epoch": now,
        "last_tick_at": now_iso(),
        "note": note or "",
    }


def cmd_tick(state: dict[str, Any], now: int) -> None:
    active = state.get("active")
    if not active:
        raise SystemExit("error: no active timer. Run 'start <section>' first.")
    active["last_tick_epoch"] = now
    active["last_tick_at"] = now_iso()


def cmd_pause(state: dict[str, Any], reason: str | None, now: int) -> None:
    active = state.get("active")
    if not active:
        raise SystemExit("error: no active timer to pause.")
    section = str(active.get("section"))
    sec = ensure_section(state, section)

    start_epoch = int(active.get("started_epoch") or now)
    # Default: close the segment at last_tick to avoid counting idle time if we were interrupted.
    end_epoch = int(active.get("last_tick_epoch") or now)
    if end_epoch < start_epoch:
        end_epoch = start_epoch

    seg = {
        "start_epoch": start_epoch,
        "start_at": active.get("started_at") or "",
        "end_epoch": end_epoch,
        "end_at": active.get("last_tick_at") or now_iso(),
        "note": active.get("note") or "",
        "reason": reason or "",
    }
    sec["segments"].append(seg)
    sec["elapsed_seconds"] = int(sec.get("elapsed_seconds") or 0) + (end_epoch - start_epoch)

    state["active"] = None


def fmt_hhmmss(total_seconds: int) -> str:
    s = max(0, int(total_seconds))
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:02d}"


def cmd_status(state: dict[str, Any], now: int) -> int:
    active = state.get("active")
    active_section = active.get("section") if active else None
    active_start_epoch = int(active.get("started_epoch") or now) if active else None
    active_last_tick_epoch = int(active.get("last_tick_epoch") or active_start_epoch) if active else None
    print(f"log: {state.get('_log_path', '(unknown)')}")
    print(f"now: {now_iso()} (epoch {now})")
    if active:
        section = active.get("section")
        start_epoch = int(active.get("started_epoch") or now)
        last_tick = int(active.get("last_tick_epoch") or start_epoch)
        wall = now - start_epoch
        effective = last_tick - start_epoch
        print(f"active: {section} (started {active.get('started_at')})")
        print(f"  wall_since_start: {fmt_hhmmss(wall)}")
        print(f"  effective_since_start (ticked): {fmt_hhmmss(effective)}")
        if active.get("note"):
            print(f"  note: {active.get('note')}")
    else:
        print("active: (none)")

    sections: dict[str, Any] = state.get("sections") or {}
    if not sections:
        print("sections: (none)")
        return 0

    print("\nsections:")
    exit_code = 0
    for name in sorted(sections.keys()):
        sec = sections[name]
        target = int(sec.get("target_seconds") or DEFAULT_TARGET_SECONDS)
        committed = int(sec.get("elapsed_seconds") or 0)
        active_effective = 0
        if active_section == name and active_start_epoch is not None and active_last_tick_epoch is not None:
            active_effective = max(0, active_last_tick_epoch - active_start_epoch)
        elapsed = committed + active_effective
        remaining = max(0, target - elapsed)
        pct = 0.0 if target <= 0 else (elapsed / target) * 100.0
        done = elapsed >= target
        marker = "DONE" if done else "IN-PROGRESS"
        extra = ""
        if active_effective:
            extra = f" (committed {fmt_hhmmss(committed)} + active {fmt_hhmmss(active_effective)})"
        print(
            f"- {name}: {marker} elapsed={fmt_hhmmss(elapsed)} target={fmt_hhmmss(target)} "
            f"remaining={fmt_hhmmss(remaining)} ({pct:.1f}%){extra}"
        )
        if not done:
            exit_code = 2

    return exit_code


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Persistent time tracker for the timed senior engineering audit."
    )
    parser.add_argument(
        "--log",
        default=str(default_log_path()),
        help="Path to the timer log JSON (default: repo/.timed_audit_clock.json)",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    start = sub.add_parser("start", help="Start timing a section (must not already be running).")
    start.add_argument("section", help="Section identifier (e.g., atc-drone).")
    start.add_argument("--note", default=None, help="Optional note for this segment.")

    tick = sub.add_parser("tick", help="Update last_activity (use after work chunks).")

    pause = sub.add_parser(
        "pause",
        help="Pause the active section. By default, closes at last tick to avoid counting idle time.",
    )
    pause.add_argument("--reason", default=None, help="Why we paused (e.g., awaiting user).")

    status = sub.add_parser("status", help="Show total elapsed/remaining by section.")

    reset = sub.add_parser("reset", help="Danger: reset the log (clears all sections and active).")
    reset.add_argument("--yes", action="store_true", help="Confirm reset.")

    return parser.parse_args(argv)


def cmd_reset(state: dict[str, Any], yes: bool) -> dict[str, Any]:
    if not yes:
        raise SystemExit("error: refusing to reset without --yes")
    return {
        "version": 1,
        "target_seconds_per_section": int(state.get("target_seconds_per_section") or DEFAULT_TARGET_SECONDS),
        "active": None,
        "sections": {},
    }


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    log_path = Path(args.log).resolve()
    now = now_epoch()

    state = load_state(log_path)
    state["_log_path"] = str(log_path)

    if args.cmd == "start":
        cmd_start(state, args.section, args.note, now)
        save_state(log_path, {k: v for k, v in state.items() if k != "_log_path"})
        return cmd_status(state, now)

    if args.cmd == "tick":
        cmd_tick(state, now)
        save_state(log_path, {k: v for k, v in state.items() if k != "_log_path"})
        return cmd_status(state, now)

    if args.cmd == "pause":
        cmd_pause(state, args.reason, now)
        save_state(log_path, {k: v for k, v in state.items() if k != "_log_path"})
        return cmd_status(state, now)

    if args.cmd == "status":
        return cmd_status(state, now)

    if args.cmd == "reset":
        state = cmd_reset(state, args.yes)
        save_state(log_path, state)
        state["_log_path"] = str(log_path)
        return cmd_status(state, now)

    raise SystemExit(f"error: unknown cmd {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
