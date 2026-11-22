#!/usr/bin/env python3
"""Offline Chore Time snapshot validation and synthetic replay drill.

This tool never contacts a household service. ``manifest`` reads an explicit
data directory and emits only paths, sizes, hashes, counts and validation
results. ``drill`` runs the isolated failover_test regression in temporary
directories. ``compare`` proves an encrypted restore reproduced a capture.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
CONTRACT = "chore-failover-state-v1"
LEDGER_SCHEMA = "chore-ledger-jsonl-v1"

REQUIRED = {"chores.json", "house.json", "ledger.jsonl"}
AUTHORITATIVE_OPTIONAL = {
    "adult_requests.json", "chore_override.json", "chore_pause.json", "chore_time_until.json",
    "envelope-load.jpg", "envelopes_printed.json", "grocy_sync.json", "home_arrivals.json",
    "home_days.json", "house_tasks.json", "hub_placement.json", "packing_game.json",
    "paper_scans.json", "peer_pin_resets.json", "photos/robot_sensors.jpg",
    "robot-camera-sun_room.json", "robot-location-training.jsonl", "robot_map.json",
    "room-eye-capture.token", "room_eye/state.json", "signal_bulbs.json",
    "sun-room-robot-floor.npz", "vacuum-button.token", "vacuum_auto.json",
    "vacuum_auto_vacuum_upstairs.json", "vacuum_help.json", "whatsnew.json",
    "keep-house-jobs/browser.json", "keep-house-jobs/state.json",
}
REBUILDABLE = {
    "brightened.json", "interactions.jsonl", "kid-display-calendar.ics", "kid_calendar.json",
    "kid_open.json", "news_seen.json", "keep-house-jobs/.lock", "room_eye/manual-samples.lock",
}
LOCAL_FENCE = {"failover-authority.json", "failover-watermark.json", "failover-probe.token"}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def revision():
    try:
        return subprocess.check_output(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def classify(relative):
    if relative in REQUIRED:
        return "required"
    if relative in AUTHORITATIVE_OPTIONAL:
        return "authoritative_optional"
    if relative.startswith(("room_eye/labels/", "room_eye/samples/", "photos/")):
        return "authoritative_optional"
    if relative.startswith("robot-camera-") and relative.endswith(".json"):
        return "authoritative_optional"
    if relative.startswith("vacuum_auto") and relative.endswith(".json"):
        return "authoritative_optional"
    if relative in REBUILDABLE:
        return "rebuildable"
    if relative in LOCAL_FENCE:
        return "node_local_fence"
    if relative.startswith("backups/") or relative.startswith(("chores.pre-", "house-pre-")):
        return "historical_archive"
    return "unclassified"


def ledger_check(path):
    result = {"schema": LEDGER_SCHEMA, "event_count": 0, "tail_complete": True,
              "json_valid": True, "unique_event_ids": True, "unique_idempotency_keys": True}
    if not path.exists():
        result.update(json_valid=False, tail_complete=False, error="ledger.jsonl is missing")
        return result
    raw = path.read_bytes()
    result["tail_complete"] = not raw or raw.endswith(b"\n")
    ids, request_ids = set(), set()
    try:
        for number, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("line %d is not an object" % number)
            result["event_count"] += 1
            event_id = row.get("id")
            if not event_id or event_id in ids:
                result["unique_event_ids"] = False
            ids.add(event_id)
            request_id = row.get("_idempotency_key")
            if request_id:
                if request_id in request_ids:
                    result["unique_idempotency_keys"] = False
                request_ids.add(request_id)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        result["json_valid"] = False
        result["error"] = str(error)
    return result


def build_manifest(data, app_revision=None):
    data = Path(data).resolve()
    files = {}
    for path in sorted(p for p in data.rglob("*") if p.is_file()):
        relative = path.relative_to(data).as_posix()
        files[relative] = {"class": classify(relative), "size": path.stat().st_size,
                           "sha256": sha256(path)}
    missing = sorted(REQUIRED - set(files))
    unclassified = sorted(name for name, item in files.items() if item["class"] == "unclassified")
    ledger = ledger_check(data / "ledger.jsonl")
    valid_json = {}
    for name in ("chores.json", "house.json"):
        try:
            valid_json[name] = isinstance(json.loads((data / name).read_text()), dict)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            valid_json[name] = False
    ok = (not missing and not unclassified and all(valid_json.values()) and ledger["tail_complete"]
          and ledger["json_valid"] and ledger["unique_event_ids"] and ledger["unique_idempotency_keys"])
    return {"contract": CONTRACT, "app_revision": app_revision or revision(), "ok": ok,
            "root_label": data.name, "missing_required": missing, "unclassified": unclassified,
            "config_json_valid": valid_json, "ledger": ledger, "files": files}


def compare(before, after):
    first = json.loads(Path(before).read_text())
    second = json.loads(Path(after).read_text())
    keys = sorted(set(first.get("files", {})) | set(second.get("files", {})))
    required_classes = {"required", "authoritative_optional"}
    differences = []
    for key in keys:
        a, b = first.get("files", {}).get(key), second.get("files", {}).get(key)
        if not a or not b:
            item = a or b or {}
            if item.get("class") in required_classes:
                differences.append({"path": key, "reason": "missing from one manifest"})
        elif a.get("class") in required_classes and a.get("sha256") != b.get("sha256"):
            differences.append({"path": key, "reason": "hash differs"})
    same_revision = first.get("app_revision") == second.get("app_revision")
    return {"contract": CONTRACT, "ok": bool(first.get("ok") and second.get("ok")
                                                and same_revision and not differences),
            "same_app_revision": same_revision, "differences": differences,
            "before_events": first.get("ledger", {}).get("event_count"),
            "after_events": second.get("ledger", {}).get("event_count")}


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    manifest = commands.add_parser("manifest")
    manifest.add_argument("--data", required=True)
    manifest.add_argument("--output")
    manifest.add_argument("--revision")
    manifests = commands.add_parser("compare")
    manifests.add_argument("before")
    manifests.add_argument("after")
    commands.add_parser("drill")
    args = parser.parse_args()

    if args.command == "manifest":
        result = build_manifest(args.data, args.revision)
        rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output:
            Path(args.output).write_text(rendered)
        else:
            sys.stdout.write(rendered)
    elif args.command == "compare":
        result = compare(args.before, args.after)
        sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    else:
        completed = subprocess.run([sys.executable, "-W", "ignore", "-m", "unittest",
                                    "tests.test_failover.RehearsalServerTest"], cwd=REPO)
        result = {"contract": CONTRACT, "ok": completed.returncode == 0,
                  "profile": "failover_test", "external_effects": "suppressed",
                  "live_household_data": "not_used"}
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
