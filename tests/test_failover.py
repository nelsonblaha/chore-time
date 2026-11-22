"""Failover fencing tests. Every profile and data directory is synthetic."""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from failover import Authority, FencedError


REPO = Path(__file__).resolve().parents[1]


class AuthorityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="chore-failover-test-"))
        self.now = [2_000_000_000.0]
        self.cfg = {"enabled": True, "node_id": "nelnet", "max_lease_seconds": 60,
                    "allow_probe": True, "rehearsal": True}
        (self.tmp / "failover-probe.token").write_text("probe-secret\n")
        (self.tmp / "failover-probe.token").chmod(0o600)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def lease(self, epoch=7, node="nelnet", issued=None, expires=None):
        issued = self.now[0] - 1 if issued is None else issued
        expires = self.now[0] + 30 if expires is None else expires
        value = {"schema_version": 1, "node_id": node, "epoch": epoch, "lease_id": "lease-%s" % epoch,
                 "issued_at": issued, "not_before": issued, "expires_at": expires, "mode": "primary"}
        (self.tmp / "failover-authority.json").write_text(json.dumps(value))
        return value

    def authority(self):
        return Authority(self.cfg, self.tmp, now=lambda: self.now[0])

    def test_disabled_is_backwards_compatible(self):
        a = Authority({}, self.tmp, now=lambda: self.now[0])
        self.assertTrue(a.status()["writable"])
        self.assertTrue(a.status()["effects_allowed"])
        self.assertEqual(a.event_fields(), {})

    def test_valid_lease_records_durable_epoch_and_fences_rollback(self):
        self.lease(epoch=9)
        a = self.authority()
        fields = a.event_fields()
        self.assertEqual(fields, {"_fence_epoch": 9, "_fence_node": "nelnet", "_fence_lease": "lease-9"})
        self.assertEqual(json.loads((self.tmp / "failover-watermark.json").read_text())["highest_epoch"], 9)
        self.lease(epoch=8)
        restarted = self.authority()
        with self.assertRaises(FencedError):
            restarted.require_mutation()
        self.assertIn("older", restarted.status()["reason"])

    def test_partition_expiry_and_wrong_node_fail_closed(self):
        self.lease()
        a = self.authority()
        self.assertTrue(a.status()["writable"])
        self.now[0] += 31
        self.assertFalse(a.status()["writable"])
        self.assertIn("expired", a.status()["reason"])
        self.now[0] -= 31
        self.lease(epoch=8, node="paul")
        self.assertFalse(a.status()["writable"])
        self.assertIn("another node", a.status()["reason"])

    def test_oversized_lease_is_rejected(self):
        self.lease(issued=self.now[0] - 1, expires=self.now[0] + 61)
        self.assertIn("duration", self.authority().status()["reason"])

    def test_probe_validation_is_rehearsal_only_and_profile_scoped(self):
        self.lease()
        a = self.authority()
        body = {"transaction_id": "drill-transaction-0001", "profile": "failover_test", "points": 7}
        self.assertEqual(a.validate_probe(body), body)
        with self.assertRaises(ValueError):
            a.validate_probe(dict(body, transaction_id="drill-transaction-0002", profile="ava"))
        a.config["rehearsal"] = False
        a.rehearsal = False
        with self.assertRaises(ValueError):
            a.validate_probe(body)
        self.assertFalse((self.tmp / "ledger.jsonl").exists())


class RehearsalServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="chore-failover-http-"))
        shutil.copy(REPO / "example-config" / "chores.json", self.tmp / "chores.json")
        config = json.loads((self.tmp / "chores.json").read_text())
        config["kids"] = [{"id": "failover_test", "name": "Failover test", "icon": "1f9ea",
                           "color": "#455a64"}]
        config["inspectors"] = [{"id": "test_operator", "name": "Test operator", "icon": "1f9ea"}]
        (self.tmp / "chores.json").write_text(json.dumps(config))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        (self.tmp / "failover-probe.token").write_text("http-probe-secret\n")
        (self.tmp / "failover-probe.token").chmod(0o600)
        (self.tmp / "house.json").write_text(json.dumps({
            "port": self.port,
            "failover": {"enabled": True, "node_id": "nelnet-test", "rehearsal": True,
                         "allow_probe": True, "max_lease_seconds": 60,
                         "allow_absent_empty_ledger": True},
        }))
        self.base = "http://127.0.0.1:%d" % self.port
        self.proc = None
        self.restart_as("nelnet-test", 42)

    def restart_as(self, node, epoch):
        self.stop_server()
        now = time.time()
        (self.tmp / "failover-authority.json").write_text(json.dumps({
            "schema_version": 1, "node_id": node, "epoch": epoch, "lease_id": "drill-%s" % epoch,
            "issued_at": now - 1, "not_before": now - 1, "expires_at": now + 45, "mode": "primary",
        }))
        env = {**os.environ, "CHORE_DATA": str(self.tmp), "CHORE_PORT": str(self.port),
               "CHORE_NODE_ID": node, "PYTHONPATH": str(REPO)}
        self.proc = subprocess.Popen([sys.executable, str(REPO / "server.py")], cwd=REPO, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(100):
            try:
                if self.get("/health")[0] == 200:
                    break
            except OSError:
                pass
            if self.proc.poll() is not None:
                self.fail("server exited during startup: " + (self.proc.stdout.read() if self.proc.stdout else ""))
            time.sleep(.03)
        else:
            self.fail("server did not start")

    def stop_server(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
            if self.proc.stdout:
                self.proc.stdout.close()
            self.proc = None

    def tearDown(self):
        self.stop_server()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=3) as response:
                raw = response.read()
                return response.status, json.loads(raw) if response.headers.get_content_type() == "application/json" else raw
        except urllib.error.HTTPError as ex:
            return ex.code, json.loads(ex.read())

    def post(self, path, body, token=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Chore-Failover-Probe"] = token
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as ex:
            return ex.code, json.loads(ex.read())

    def test_routes_probe_and_real_profile_suppression(self):
        status, public = self.get("/api/failover/status")
        self.assertEqual(status, 200)
        self.assertEqual(public["role"], "primary")
        self.assertTrue(public["effects_suppressed"])
        health = self.get("/health")[1]
        self.assertEqual(health["checkpoint"]["contract"], "chore-core-checkpoint-v2")
        self.assertTrue(health["checkpoint"]["ready"])
        self.assertFalse(health["checkpoint"]["ledger"]["present"])
        self.assertTrue(health["checkpoint"]["ledger"]["read_ok"])
        self.assertEqual(health["checkpoint"]["ledger"]["event_count"], 0)
        self.assertIsNone(health["checkpoint"]["ledger"]["tail_complete"])
        self.assertEqual(len(health["checkpoint"]["core_sha256"]), 64)
        self.assertEqual(self.get("/kids")[0], 200)

        status, rejected = self.post("/api/start", {"kid": "ava", "chore": "kitchen"})
        self.assertEqual(status, 503)
        self.assertIn("synthetic", rejected["error"])
        self.assertFalse((self.tmp / "ledger.jsonl").exists())

        body = {"transaction_id": "http-drill-transaction-1", "profile": "failover_test", "points": 3}
        self.assertEqual(self.post("/api/failover/probe", body, "wrong")[0], 403)
        first = self.post("/api/failover/probe", body, "http-probe-secret")
        second = self.post("/api/failover/probe", body, "http-probe-secret")
        self.assertEqual((first[0], second[0]), (200, 200))
        self.assertEqual(first[1]["receipt"]["balance"], 3)
        self.assertTrue(first[1]["receipt"]["created"])
        self.assertFalse(second[1]["receipt"]["created"])
        self.assertEqual(first[1]["receipt"]["event_id"], second[1]["receipt"]["event_id"])
        self.assertTrue(first[1]["effects_suppressed"])
        rows = [json.loads(line) for line in (self.tmp / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["kid"], rows[0]["points"]), ("failover_test", 3))
        conflict = self.post("/api/failover/probe", dict(body, points=4), "http-probe-secret")
        self.assertEqual(conflict[0], 409)

        # A higher-epoch safe return replays the lost response without a
        # second ledger action; an older authority can never come back later.
        self.restart_as("paul-return", 43)
        returned = self.post("/api/failover/probe", body, "http-probe-secret")
        self.assertEqual(returned[0], 200)
        self.assertFalse(returned[1]["receipt"]["created"])
        self.assertEqual(returned[1]["receipt"]["event_id"], first[1]["receipt"]["event_id"])
        rows = [json.loads(line) for line in (self.tmp / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.get("/api/state")[1]["balance"]["failover_test"], 3)

        self.restart_as("nelnet-test", 42)
        self.assertFalse(self.get("/health")[1]["failover"]["writable"])
        self.assertEqual(self.post("/api/failover/probe", body, "http-probe-secret")[0], 503)

    def test_health_fails_closed_when_a_required_checkpoint_file_cannot_be_read(self):
        house = self.tmp / "house.json"
        house.unlink()
        house.mkdir()
        status, health = self.get("/health")
        self.assertEqual(status, 503)
        self.assertFalse(health["ok"])
        self.assertFalse(health["checkpoint"]["ready"])
        self.assertTrue(health["checkpoint"]["house"]["present"])
        self.assertFalse(health["checkpoint"]["house"]["read_ok"])
        self.assertIsNone(health["checkpoint"]["core_sha256"])


class ReplayManifestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="chore-failover-manifest-"))
        (self.tmp / "chores.json").write_text("{}\n")
        (self.tmp / "house.json").write_text("{}\n")
        (self.tmp / "ledger.jsonl").write_text(json.dumps({
            "id": "fixture-event", "ts": 1, "type": "adjust", "kid": "failover_test", "points": 1,
        }) + "\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def manifest(self):
        completed = subprocess.run([sys.executable, str(REPO / "tools" / "failover_replay.py"),
                                    "manifest", "--data", str(self.tmp), "--revision", "fixture-revision"],
                                   cwd=REPO, text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_manifest_is_payload_free_and_fails_closed_on_new_or_incomplete_state(self):
        valid = self.manifest()
        self.assertTrue(valid["ok"])
        self.assertEqual(valid["ledger"]["event_count"], 1)
        self.assertNotIn("failover_test", json.dumps(valid))

        (self.tmp / "new-state.json").write_text("{}\n")
        unknown = self.manifest()
        self.assertFalse(unknown["ok"])
        self.assertEqual(unknown["unclassified"], ["new-state.json"])
        (self.tmp / "new-state.json").unlink()

        (self.tmp / "ledger.jsonl").write_text('{"id":"incomplete"')
        broken = self.manifest()
        self.assertFalse(broken["ok"])
        self.assertFalse(broken["ledger"]["tail_complete"])
        self.assertFalse(broken["ledger"]["json_valid"])

    def test_manifest_accepts_current_dynamic_authoritative_state(self):
        for name in ("packing_progress.json", "envelope_grocy_sync.json", "ha-token"):
            (self.tmp / name).write_text("fixture\n")
        current = self.manifest()
        self.assertTrue(current["ok"])
        for name in ("packing_progress.json", "envelope_grocy_sync.json", "ha-token"):
            self.assertEqual(current["files"][name]["class"], "authoritative_optional")


if __name__ == "__main__":
    unittest.main()
