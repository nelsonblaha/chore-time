import unittest
from datetime import datetime, timezone

import mail_delivery


NOW = datetime(2026, 11, 18, 19, 30, tzinfo=timezone.utc).timestamp()


def event(event_id, at="2026-11-18T19:20:00Z", caption="A mail carrier delivers mail to the front door", description="USPS delivery"):
    return {"event_id": event_id, "timestamp": at, "caption": caption, "description": description}


class MailDeliveryTest(unittest.TestCase):
    def harness(self, events, ledger=None, last_clean=0):
        out, logs, changes = [], [], []
        g = {
            "ha_get": lambda entity, token: {"attributes": {"events": events}},
            "ha_token": lambda: "token",
            "read_events": lambda: list(ledger or []),
            "derive": lambda: {"board": {"mail": {"last_clean": last_clean}}},
            "append": lambda row: out.append(row) or row,
            "ilog": logs.append,
            "after_change": lambda: changes.append(True),
        }
        return g, out, logs, changes

    def test_postal_caption_reopens_and_records_source(self):
        g, out, logs, changes = self.harness([event("mail-1")])
        self.assertEqual(mail_delivery.check(g, {"entity": "sensor.porch"}, NOW), 1)
        self.assertEqual(out[0]["type"], "reopen")
        self.assertEqual(out[0]["chore"], "mail")
        self.assertEqual(out[0]["source_event"], "mail-1")
        self.assertIn("Mail was delivered", out[0]["note"])
        self.assertEqual(len(logs), 1)
        self.assertEqual(changes, [True])

    def test_ignores_packages_and_people_without_postal_evidence(self):
        events = [
            event("amazon", caption="Amazon delivery person drops off a package", description="A package is delivered"),
            event("person", caption="A person enters the house", description=""),
        ]
        g, out, _, changes = self.harness(events)
        self.assertEqual(mail_delivery.check(g, {"entity": "sensor.porch"}, NOW), 0)
        self.assertEqual(out, [])
        self.assertEqual(changes, [])

    def test_deduplicates_and_ignores_delivery_older_than_last_sort(self):
        old = event("old", at="2026-11-18T18:00:00Z")
        seen = event("seen", at="2026-11-18T19:20:00Z")
        ledger = [{"type": "reopen", "source": "mail_delivery", "source_event": "seen"}]
        g, out, _, _ = self.harness([old, seen], ledger, datetime(2026, 11, 18, 18, 30, tzinfo=timezone.utc).timestamp())
        self.assertEqual(mail_delivery.check(g, {"entity": "sensor.porch"}, NOW), 0)
        self.assertEqual(out, [])

    def test_rejects_stale_backfill(self):
        g, out, _, _ = self.harness([event("stale", at="2026-11-14T19:20:00Z")])
        self.assertEqual(mail_delivery.check(g, {"entity": "sensor.porch", "max_age_h": 72}, NOW), 0)
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
