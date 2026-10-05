# -*- coding: utf-8 -*-
"""Regression tests for the DeoVR JSON contract and SQLite soft migrations."""

import os
import sqlite3
import sys
import tempfile
import unittest


PROJECT_MODULES = ["app", "config", "scraper", "store", "torrserve"]


def _drop_project_modules():
    for name in PROJECT_MODULES:
        sys.modules.pop(name, None)


def _load_app_for_db(db_path: str):
    os.environ["DB_PATH"] = db_path
    os.environ["REFRESH_INTERVAL_SEC"] = "0"
    _drop_project_modules()
    import app  # noqa: PLC0415
    import store  # noqa: PLC0415

    return app, store


class DeoVRContractTests(unittest.TestCase):
    def tearDown(self):
        _drop_project_modules()

    def test_legacy_database_is_migrated_before_deovr_reads_it(self):
        fd, db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(db_path) and os.remove(db_path))

        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE items (
                id TEXT PRIMARY KEY,
                topic_id TEXT,
                title TEXT,
                tags TEXT,
                thumbnail_url TEXT,
                source_type TEXT,
                source TEXT,
                screen_type TEXT,
                stereo_mode TEXT,
                video_length INTEGER DEFAULT 0,
                updated_at INTEGER,
                stream_url TEXT,
                stream_built_at INTEGER
            )
            """
        )
        conn.execute(
            """
            INSERT INTO items (
                id, topic_id, title, tags, thumbnail_url, source_type, source,
                screen_type, stereo_mode, video_length, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "123",
                "123",
                "Legacy scene",
                '["tag"]',
                "https://example.invalid/thumb.jpg",
                "magnet",
                "magnet:?xt=urn:btih:deadbeef",
                "dome",
                "sbs",
                60,
                1,
            ),
        )
        conn.commit()
        conn.close()

        app_mod, store_mod = _load_app_for_db(db_path)

        columns = {
            row[1]
            for row in sqlite3.connect(db_path).execute("PRAGMA table_info(items)").fetchall()
        }
        self.assertIn("source_rank", columns)
        self.assertIn("site", columns)
        self.assertIn("video_height", columns)

        items = store_mod.all_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["site"], "pornolab")
        self.assertEqual(items[0]["source_rank"], 999999999)
        self.assertIsNone(items[0]["video_height"])

        response = app_mod.app.test_client().get("/deovr", headers={"Host": "192.168.1.2:9999"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content_type.startswith("application/json"))
        payload = response.get_json()
        self.assertEqual(payload["authorized"], 0)
        self.assertIn("scenes", payload)
        self.assertGreaterEqual(len(payload["scenes"]), 1)

    def test_deovr_short_list_uses_strict_short_format_and_detail_uses_video_height(self):
        fd, db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(db_path) and os.remove(db_path))

        app_mod, store_mod = _load_app_for_db(db_path)
        store_mod.upsert_item(
            {
                "id": "xc_42",
                "topic_id": "42",
                "title": "XXXClub sample",
                "tags": ["Collection"],
                "thumbnail_url": "https://example.invalid/thumb.jpg",
                "source_type": "magnet",
                "source": "magnet:?xt=urn:btih:deadbeef",
                "screen_type": "dome",
                "stereo_mode": "sbs",
                "video_length": 120,
                "video_height": 3072,
                "site": "xxxclub",
                "source_rank": 0,
            }
        )

        client = app_mod.app.test_client()
        root_response = client.post("/deovr", headers={"Host": "192.168.1.2:9999"})
        self.assertEqual(root_response.status_code, 200)
        root = root_response.get_json()
        cards = [card for scene in root["scenes"] for card in scene["list"]]
        self.assertEqual(len(cards), 2)  # "XXXClub: Всё" + tag scene
        card = cards[0]
        self.assertEqual(
            set(card),
            {"title", "videoLength", "thumbnailUrl", "video_url"},
            "short DeoVR cards must not expose string ids such as xc_42",
        )
        self.assertEqual(card["video_url"], "http://192.168.1.2:9999/video/xc_42")

        detail_response = client.get("/video/xc_42", headers={"Host": "192.168.1.2:9999"})
        self.assertEqual(detail_response.status_code, 200)
        self.assertTrue(detail_response.content_type.startswith("application/json"))
        detail = detail_response.get_json()
        self.assertEqual(detail["authorized"], 0)
        self.assertIsInstance(detail["id"], int)
        source = detail["encodings"][0]["videoSources"][0]
        self.assertEqual(source["resolution"], 3072)
        self.assertEqual(source["height"], 3072)
        self.assertEqual(source["url"], "http://192.168.1.2:9999/stream/xc_42")


if __name__ == "__main__":
    unittest.main()
