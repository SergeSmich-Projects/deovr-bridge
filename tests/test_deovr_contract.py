# -*- coding: utf-8 -*-
"""Regression tests for DeoVR, PLAY'A API v2 and SQLite soft migrations."""

import os
import sqlite3
import sys
import tempfile
import unittest


PROJECT_MODULES = ["app", "config", "scraper", "store", "torrserve"]


def _drop_project_modules():
    for name in PROJECT_MODULES:
        sys.modules.pop(name, None)


def _load_app_for_db(db_path: str, hidden_sites: str = "xxxclub"):
    os.environ["DB_PATH"] = db_path
    os.environ["REFRESH_INTERVAL_SEC"] = "0"
    os.environ["HIDDEN_LIBRARY_SITE_IDS"] = hidden_sites
    _drop_project_modules()
    import app  # noqa: PLC0415
    import store  # noqa: PLC0415

    return app, store


def _temp_db(testcase: unittest.TestCase) -> str:
    fd, db_path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    testcase.addCleanup(lambda: os.path.exists(db_path) and os.remove(db_path))
    return db_path


def _sample_item(**overrides):
    item = {
        "id": "custom_42",
        "topic_id": "42",
        "title": "Visible sample",
        "tags": ["Collection", "VR"],
        "thumbnail_url": "https://example.invalid/thumb.jpg",
        "source_type": "magnet",
        "source": "magnet:?xt=urn:btih:deadbeef",
        "screen_type": "dome",
        "stereo_mode": "sbs",
        "video_length": 120,
        "video_height": 3072,
        "site": "pornolab",
        "source_rank": 0,
        "seeders": 7,
        "leechers": 2,
        "studio": ["Studio A"],
        "performers": ["Actor One"],
    }
    item.update(overrides)
    return item


class DeoVRContractTests(unittest.TestCase):
    def tearDown(self):
        _drop_project_modules()

    def test_legacy_database_is_migrated_before_deovr_reads_it(self):
        db_path = _temp_db(self)

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
        db_path = _temp_db(self)
        app_mod, store_mod = _load_app_for_db(db_path)
        store_mod.upsert_item(_sample_item())

        client = app_mod.app.test_client()
        root_response = client.post("/deovr", headers={"Host": "192.168.1.2:9999"})
        self.assertEqual(root_response.status_code, 200)
        root = root_response.get_json()
        cards = [card for scene in root["scenes"] for card in scene["list"]]
        self.assertGreaterEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(
            set(card),
            {"title", "videoLength", "thumbnailUrl", "video_url"},
            "short DeoVR cards must not expose string ids such as custom_42",
        )
        self.assertEqual(card["video_url"], "http://192.168.1.2:9999/video/custom_42")

        detail_response = client.get("/video/custom_42", headers={"Host": "192.168.1.2:9999"})
        self.assertEqual(detail_response.status_code, 200)
        self.assertTrue(detail_response.content_type.startswith("application/json"))
        detail = detail_response.get_json()
        self.assertEqual(detail["authorized"], 0)
        self.assertIsInstance(detail["id"], int)
        source = detail["encodings"][0]["videoSources"][0]
        self.assertEqual(source["resolution"], 3072)
        self.assertEqual(source["height"], 3072)
        self.assertEqual(source["url"], "http://192.168.1.2:9999/stream/custom_42")

    def test_xxxclub_is_hidden_from_player_outputs_by_default(self):
        db_path = _temp_db(self)
        app_mod, store_mod = _load_app_for_db(db_path)
        store_mod.upsert_item(_sample_item(id="pl_1", title="Pornolab visible", site="pornolab"))
        store_mod.upsert_item(_sample_item(id="xc_1", title="XXXClub hidden", site="xxxclub"))

        client = app_mod.app.test_client()
        deovr = client.get("/deovr", headers={"Host": "192.168.1.2:9999"}).get_json()
        titles = [card["title"] for scene in deovr["scenes"] for card in scene["list"]]
        self.assertTrue(any("Pornolab visible" in title for title in titles))
        self.assertFalse(any("XXXClub hidden" in title for title in titles))
        self.assertEqual(client.get("/video/xc_1", headers={"Host": "192.168.1.2:9999"}).status_code, 404)

        playa = client.get(
            "/api/playa/v2/videos?page-index=0&page-size=50",
            headers={"Host": "192.168.1.2:9999"},
        ).get_json()
        self.assertEqual(playa["status"]["code"], 1)
        self.assertEqual(playa["data"]["item_total"], 1)
        self.assertEqual(playa["data"]["content"][0]["id"], "pl_1")

    def test_playa_initial_flow_and_video_detail(self):
        db_path = _temp_db(self)
        app_mod, store_mod = _load_app_for_db(db_path)
        store_mod.upsert_item(_sample_item(id="pl_42", title="PLAYA sample"))

        client = app_mod.app.test_client()
        version = client.get("/api/playa/v2/version", headers={"Host": "192.168.1.2:9999"}).get_json()
        self.assertEqual(version["status"]["code"], 1)
        self.assertEqual(version["data"], "1.10.1")

        config = client.get("/api/playa/v2/config", headers={"Host": "192.168.1.2:9999"}).get_json()
        self.assertEqual(config["status"]["code"], 1)
        self.assertFalse(config["data"]["auth"])
        self.assertTrue(config["data"]["actors"])
        self.assertTrue(config["data"]["studios"])

        videos = client.get(
            "/api/playa/v2/videos?page-index=0&page-size=10&title=sample",
            headers={"Host": "192.168.1.2:9999"},
        ).get_json()
        self.assertEqual(videos["status"]["code"], 1)
        self.assertEqual(videos["data"]["item_total"], 1)
        self.assertEqual(videos["data"]["content"][0]["status"], "S:7 L:2")

        detail = client.get("/api/playa/v2/video/pl_42", headers={"Host": "192.168.1.2:9999"}).get_json()
        self.assertEqual(detail["status"]["code"], 1)
        data = detail["data"]
        self.assertEqual(data["id"], "pl_42")
        self.assertEqual(data["studio"][0]["title"], "Studio A")
        self.assertEqual(data["actors"][0]["title"], "Actor One")
        link = data["details"][0]["links"][0]
        self.assertEqual(link["url"], "http://192.168.1.2:9999/stream/pl_42")
        self.assertEqual(link["projection"], "180")
        self.assertEqual(link["stereo"], "LR")
        self.assertEqual(link["quality_name"], "3072p")

        actors = client.get("/api/playa/v2/actors", headers={"Host": "192.168.1.2:9999"}).get_json()
        self.assertEqual(actors["data"]["content"][0]["title"], "Actor One")
        studios = client.get("/api/playa/v2/studios", headers={"Host": "192.168.1.2:9999"}).get_json()
        self.assertEqual(studios["data"]["content"][0]["title"], "Studio A")
        categories = client.get("/api/playa/v2/categories", headers={"Host": "192.168.1.2:9999"}).get_json()
        self.assertEqual(categories["status"]["code"], 1)
        self.assertTrue(any(category["title"] == "VR" for category in categories["data"]))


if __name__ == "__main__":
    unittest.main()
