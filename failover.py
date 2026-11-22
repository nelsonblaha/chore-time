"""Small, coordinator-fed authority fence for Chore Time.

This module deliberately does not elect a primary.  A separate, trusted
coordinator writes a short-lived lease containing a monotonically increasing
epoch.  Chore Time validates that lease before accepting mutations or causing
external effects and remembers the highest epoch it has accepted across
restarts.

The feature is disabled unless house.json contains ``failover.enabled=true``;
existing single-node installations therefore keep their current behaviour.
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
from pathlib import Path


class FencedError(RuntimeError):
    """The node is not the current write/effect authority."""

    def __init__(self, status):
        super().__init__(status.get("reason") or "node is fenced")
        self.status = status


def _atomic_json(path, value, mode=0o600):
    """Write a small authority journal durably (file and containing directory)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, mode)
    try:
        dfd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:  # some filesystems do not support directory fsync
        pass


class Authority:
    """Validate coordinator leases and maintain the local epoch high-water mark."""

    def __init__(self, config, data_dir, now=None):
        self.config = dict(config or {})
        self.data_dir = Path(data_dir)
        self.enabled = self.config.get("enabled") is True
        self.node_id = str(os.environ.get("CHORE_NODE_ID") or self.config.get("node_id") or "").strip()
        self.rehearsal = self.config.get("rehearsal") is True or os.environ.get("CHORE_FAILOVER_REHEARSAL") == "1"
        self.allow_probe = self.config.get("allow_probe") is True
        self.max_lease_seconds = float(self.config.get("max_lease_seconds") or 120)
        self.clock_skew_seconds = float(self.config.get("clock_skew_seconds") or 5)
        self.authority_file = self._path(self.config.get("authority_file"), "failover-authority.json")
        self.watermark_file = self._path(self.config.get("watermark_file"), "failover-watermark.json")
        self.probe_token_file = self._path(self.config.get("probe_token_file"), "failover-probe.token")
        self.now = now or time.time
        self.lock = threading.RLock()

    def _path(self, configured, fallback):
        return Path(str(configured)).expanduser() if configured else self.data_dir / fallback

    @staticmethod
    def _read_json(path, default=None):
        try:
            value = json.loads(Path(path).read_text())
            return value if isinstance(value, dict) else default
        except (OSError, ValueError):
            return default

    def _watermark(self):
        value = self._read_json(self.watermark_file, {}) or {}
        try:
            return max(0, int(value.get("highest_epoch") or 0))
        except (TypeError, ValueError):
            return 0

    def _safe(self, **updates):
        out = {
            "enabled": self.enabled,
            "node_id": self.node_id or None,
            "role": "single" if not self.enabled else "fenced",
            "writable": not self.enabled,
            "effects_allowed": not self.enabled,
            "effects_suppressed": False,
            "rehearsal": self.rehearsal,
            "epoch": None,
            "lease_expires_at": None,
            "reason": "failover is disabled" if not self.enabled else "no valid authority lease",
        }
        out.update(updates)
        return out

    def status(self, record=True):
        if not self.enabled:
            return self._safe()
        if not self.node_id:
            return self._safe(reason="failover node_id is not configured", effects_suppressed=True)
        lease = self._read_json(self.authority_file)
        if not lease:
            return self._safe(reason="authority lease is missing or unreadable", effects_suppressed=True)
        try:
            schema = int(lease.get("schema_version"))
            epoch = int(lease.get("epoch"))
            issued = float(lease.get("issued_at"))
            not_before = float(lease.get("not_before", issued))
            expires = float(lease.get("expires_at"))
        except (TypeError, ValueError):
            return self._safe(reason="authority lease fields are invalid", effects_suppressed=True)
        base = {"epoch": epoch, "lease_expires_at": expires, "effects_suppressed": True}
        now = float(self.now())
        if schema != 1 or epoch < 1 or lease.get("mode") != "primary" or not str(lease.get("lease_id") or ""):
            return self._safe(reason="authority lease shape is invalid", **base)
        if str(lease.get("node_id") or "") != self.node_id:
            return self._safe(reason="authority lease belongs to another node", **base)
        if issued > now + self.clock_skew_seconds or not_before > now + self.clock_skew_seconds:
            return self._safe(reason="authority lease is not active yet", **base)
        if expires <= now:
            return self._safe(reason="authority lease expired", **base)
        if expires <= issued or expires - issued > self.max_lease_seconds:
            return self._safe(reason="authority lease duration exceeds the safety limit", **base)
        with self.lock:
            high = self._watermark()
            if epoch < high:
                return self._safe(reason="authority epoch is older than the durable watermark", **base)
            if record and epoch > high:
                _atomic_json(self.watermark_file, {"schema_version": 1, "highest_epoch": epoch,
                                                   "recorded_at": now, "node_id": self.node_id})
        effects = not self.rehearsal
        return self._safe(role="primary", writable=True, effects_allowed=effects,
                          effects_suppressed=not effects, rehearsal=self.rehearsal,
                          epoch=epoch, lease_expires_at=expires,
                          reason="rehearsal suppresses external effects" if self.rehearsal else "valid primary lease")

    def require_mutation(self):
        status = self.status()
        if not status["writable"]:
            raise FencedError(status)
        return status

    def require_effect(self):
        status = self.status()
        if not status["effects_allowed"]:
            raise FencedError(status)
        return status

    def event_fields(self):
        if not self.enabled:
            return {}
        status = self.require_mutation()
        lease = self._read_json(self.authority_file, {}) or {}
        return {"_fence_epoch": status["epoch"], "_fence_node": self.node_id,
                "_fence_lease": str(lease.get("lease_id") or "")}

    def check_probe_token(self, supplied):
        try:
            if stat.S_IMODE(self.probe_token_file.stat().st_mode) != 0o600:
                return False
            expected = self.probe_token_file.read_text().strip()
        except OSError:
            return False
        import hmac
        return bool(expected and hmac.compare_digest(str(supplied or ""), expected))

    def validate_probe(self, body):
        """Validate the isolated synthetic ledger transaction request."""
        if not self.enabled or not self.allow_probe:
            raise ValueError("failover probe is not enabled")
        if not self.rehearsal:
            raise ValueError("failover probe requires rehearsal mode")
        self.require_mutation()
        txid = str(body.get("transaction_id") or "")
        profile = str(body.get("profile") or "")
        if profile != "failover_test":
            raise ValueError("only the synthetic failover_test profile is allowed")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,79}", txid):
            raise ValueError("invalid transaction_id")
        try:
            points = int(body.get("points"))
        except (TypeError, ValueError):
            raise ValueError("points must be an integer") from None
        if points < -25 or points > 25:
            raise ValueError("synthetic points must be between -25 and 25")
        return {"transaction_id": txid, "profile": profile, "points": points}

    def public_status(self):
        status = self.status(record=False)
        status["routes"] = {
            "kids": "/kids", "parent": "/inspect", "state": "/api/state", "health": "/health",
            "ha_kids_asset": "/local/chore-kids.html", "ha_state_sensor": "sensor.chore_state",
        }
        status["dependencies"] = [
            "coordinator-issued short lease with monotonically increasing epoch",
            "single-writer, ordered replication of CHORE_DATA",
            "stable VIP/DNS/reverse-proxy route for Hub URLs",
            "Home Assistant reachability and synchronized HA kids-page asset",
        ]
        return status
