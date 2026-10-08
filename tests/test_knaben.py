# -*- coding: utf-8 -*-
"""Tests for the structured Knaben VR importer."""

import os
import sys
import tempfile
import unittest


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response({"hits": self.hits})


class KnabenTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.environ["DB_PATH"] = self.db_path
        for name in ("knaben", "store", "config"):
            sys.modules.pop(name, None)
        import config  # noqa: PLC0415
        import knaben  # noqa: PLC0415
        import store  # noqa: PLC0415

        self.config = config
        self.knaben = knaben
        self.store = store
        config.ENABLE_KNABEN = True
        config.KNABEN_QUERIES = ["VR180"]
        config.KNABEN_MIN_SEEDERS = 2
        config.KNABEN_MIN_SIZE_BYTES = 1024 ** 3
        config.KNABEN_MAX_SIZE_BYTES = 500 * 1024 ** 3
        config.KNABEN_MAX_IMPORT_ITEMS = 20
        config.KNABEN_RESULTS_PER_QUERY = 20
        config.KNABEN_LAST_SEEN_SEC = 86400
        store.init_db()

    def tearDown(self):
        for name in ("knaben", "store", "config"):
            sys.modules.pop(name, None)
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    @staticmethod
    def _hit(**overrides):
        hit = {
            "title": "Studio Scene VR180 8K SBS",
            "hash": "A" * 40,
            "magnetUrl": None,
            "bytes": 12 * 1024 ** 3,
            "seeders": 14,
            "peers": 3,
            "tracker": "1337x",
            "date": "2026-10-06T12:00:00Z",
            "lastSeen": "2026-10-07T12:00:00Z",
        }
        hit.update(overrides)
        return hit

    def test_search_uses_safe_seed_ordered_api_request(self):
        session = _Session([])
        self.assertEqual(self.knaben.search("  VR180  ", session=session), [])
        _, kwargs = session.calls[0]
        payload = kwargs["json"]
        self.assertEqual(payload["query"], "VR180")
        self.assertEqual(payload["order_by"], "seeders")
        self.assertEqual(payload["order_direction"], "desc")
        self.assertTrue(payload["hide_unsafe"])
        self.assertFalse(payload["hide_xxx"])
        self.assertEqual(payload["seconds_since_last_seen"], 86400)

    def test_normalizer_rejects_dead_small_and_dangerous_results(self):
        self.assertIsNone(self.knaben.normalize_hit(self._hit(seeders=0)))
        self.assertIsNone(self.knaben.normalize_hit(self._hit(bytes=1000)))
        self.assertIsNone(self.knaben.normalize_hit(self._hit(title="VR180 installer.exe")))
        self.assertIsNone(self.knaben.normalize_hit(self._hit(title="Ordinary flat movie")))

        item = self.knaben.normalize_hit(self._hit())
        self.assertEqual(item["id"], "kn_" + "a" * 40)
        self.assertEqual(item["site"], "knaben")
        self.assertEqual(item["source_type"], "magnet")
        self.assertIn("urn:btih:", item["source"])
        self.assertEqual(item["video_height"], 4320)
        self.assertEqual(item["seeders"], 14)
        self.assertEqual(item["leechers"], 3)

    def test_refresh_deduplicates_infohash_and_keeps_best_swarm(self):
        same_hash = "B" * 40
        session = _Session([
            self._hit(hash=same_hash, seeders=4, title="Scene VR180 4096p"),
            self._hit(hash=same_hash.lower(), seeders=18, title="Scene VR180 8192p"),
            self._hit(hash="C" * 40, seeders=1, title="Dead Scene VR180 8192p"),
        ])
        count = self.knaben.refresh(session=session)
        self.assertEqual(count, 1)
        items = self.store.all_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["seeders"], 18)
        self.assertEqual(items[0]["title"], "Scene VR180 8192p")
        self.assertEqual(items[0]["created_at"], 1791288000)


if __name__ == "__main__":
    unittest.main()
