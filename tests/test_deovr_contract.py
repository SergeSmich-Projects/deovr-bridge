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
    os.environ["ENABLE_EXTERNAL_ENTITY_META"] = "0"
    os.environ["ENABLE_EXTERNAL_TRAILERS"] = "0"
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
        card = videos["data"]["content"][0]
        self.assertEqual(card["status"], "")
        self.assertIn("Studio A", card["title"])
        self.assertIn("Actor One", card["subtitle"])
        self.assertNotIn("S7", card["subtitle"])
        self.assertIn("S7", card["tooltip"])
        self.assertIn("P2", card["tooltip"])
        self.assertIn("3072p", card["tooltip"])

        detail = client.get("/api/playa/v2/video/pl_42", headers={"Host": "192.168.1.2:9999"}).get_json()
        self.assertEqual(detail["status"]["code"], 1)
        data = detail["data"]
        self.assertEqual(data["id"], "pl_42")
        self.assertEqual(data["studio"]["title"], "Studio A")
        self.assertEqual(data["actors"][0]["title"], "Actor One")
        link = data["details"][0]["links"][0]
        self.assertEqual(link["url"], "http://192.168.1.2:9999/stream/pl_42")
        self.assertEqual(link["projection"], "180")
        self.assertEqual(link["stereo"], "LR")
        self.assertTrue(link["is_stream"])
        self.assertTrue(link["is_download"])
        self.assertEqual(link["quality_name"], "3072p")

        # PLAY'A in the wild may send literal &amp; separators. Support those too.
        studio_id = data["studio"]["id"]
        filtered = client.get(
            f"/api/playa/v2/videos?page-index=0&amp;page-size=10&amp;studio={studio_id}",
            headers={"Host": "192.168.1.2:9999"},
        ).get_json()
        self.assertEqual(filtered["data"]["item_total"], 1)

        actors = client.get("/api/playa/v2/actors", headers={"Host": "192.168.1.2:9999"}).get_json()
        self.assertEqual(actors["data"]["content"][0]["title"], "Actor One")
        studios = client.get("/api/playa/v2/studios", headers={"Host": "192.168.1.2:9999"}).get_json()
        self.assertEqual(studios["data"]["content"][0]["title"], "Studio A")
        categories = client.get("/api/playa/v2/categories", headers={"Host": "192.168.1.2:9999"}).get_json()
        self.assertEqual(categories["status"]["code"], 1)
        self.assertTrue(any(category["title"] == "VR" for category in categories["data"]))

        groups = client.get("/api/playa/v2/categories-groups", headers={"Host": "192.168.1.2:9999"}).get_json()
        self.assertEqual([g["id"] for g in groups["data"]], ["tags"])
        statuses = client.get("/api/playa/v2/video-statuses", headers={"Host": "192.168.1.2:9999"}).get_json()
        self.assertEqual(statuses["data"], [])

    def test_playa_ignores_stale_entity_meta_cache(self):
        db_path = _temp_db(self)
        app_mod, store_mod = _load_app_for_db(db_path)
        store_mod.upsert_item(_sample_item(id="meta_1", title="Metadata sample"))

        actor_id = app_mod._playa_entity_id("actor", "Actor One")
        studio_id = app_mod._playa_entity_id("studio", "Studio A")
        store_mod.upsert_entity_meta(
            "actor",
            actor_id,
            "Actor One",
            preview_url="https://images.povr.com/headshots/same-for-everyone/crop/300.jpg",
            source_url="https://povr.com/pornstars/actor-one",
            trailer_url="https://example.invalid/trailer.mp4",
        )
        store_mod.upsert_entity_meta(
            "studio",
            studio_id,
            "Studio A",
            preview_url="https://images.povr.com/assets/logos/channels/0/2/2627/t760povr.png",
            source_url="https://povr.com/studios/studio-a",
        )

        client = app_mod.app.test_client()
        actors = client.get("/api/playa/v2/actors", headers={"Host": "192.168.1.2:9999"}).get_json()
        self.assertIn("/entity-placeholder/actor/", actors["data"]["content"][0]["preview"])
        self.assertIn(".png?", actors["data"]["content"][0]["preview"])
        self.assertNotIn("images.povr.com", actors["data"]["content"][0]["preview"])

        actor_detail = client.get(f"/api/playa/v2/actor/{actor_id}", headers={"Host": "192.168.1.2:9999"}).get_json()
        prop_names = [prop["name"] for prop in actor_detail["data"]["properties"]]
        self.assertEqual(prop_names, ["Videos", "pornolab", "Local list"])

        studios = client.get("/api/playa/v2/studios", headers={"Host": "192.168.1.2:9999"}).get_json()
        self.assertIn("/entity-placeholder/studio/", studios["data"]["content"][0]["preview"])
        self.assertIn(".png?", studios["data"]["content"][0]["preview"])
        self.assertNotIn("images.povr.com", studios["data"]["content"][0]["preview"])

    def test_actor_and_studio_local_entity_list(self):
        db_path = _temp_db(self)
        app_mod, store_mod = _load_app_for_db(db_path)
        store_mod.upsert_item(_sample_item(id="list_1", title="List sample"))

        actor_id = app_mod._playa_entity_id("actor", "Actor One")
        studio_id = app_mod._playa_entity_id("studio", "Studio A")
        client = app_mod.app.test_client()

        actor_toggle = client.get(
            f"/entity-list/actor/{actor_id}/toggle?title=Actor%20One",
            headers={"Host": "192.168.1.2:9999", "Accept": "application/json"},
        ).get_json()
        self.assertTrue(actor_toggle["listed"])
        studio_toggle = client.post(
            f"/entity-list/studio/{studio_id}",
            data={"title": "Studio A"},
            headers={"Host": "192.168.1.2:9999", "Accept": "application/json"},
        ).get_json()
        self.assertTrue(studio_toggle["listed"])

        actors = client.get("/api/playa/v2/actors", headers={"Host": "192.168.1.2:9999"}).get_json()
        self.assertEqual(actors["data"]["content"][0]["title"], "★ Actor One")
        studios = client.get("/api/playa/v2/studios", headers={"Host": "192.168.1.2:9999"}).get_json()
        self.assertEqual(studios["data"]["content"][0]["title"], "★ Studio A")

        lists = client.get("/entity-lists.json", headers={"Host": "192.168.1.2:9999"}).get_json()
        listed = {(row["kind"], row["entity_id"]) for row in lists["items"]}
        self.assertIn(("actor", actor_id), listed)
        self.assertIn(("studio", studio_id), listed)

        client.delete(
            f"/entity-list/actor/{actor_id}",
            headers={"Host": "192.168.1.2:9999", "Accept": "application/json"},
        )
        actors = client.get("/api/playa/v2/actors", headers={"Host": "192.168.1.2:9999"}).get_json()
        self.assertEqual(actors["data"]["content"][0]["title"], "Actor One")

    def test_actor_matching_aggregates_hidden_tracker_and_aliases(self):
        db_path = _temp_db(self)
        app_mod, store_mod = _load_app_for_db(db_path, hidden_sites="xxxclub")
        store_mod.upsert_item(_sample_item(
            id="pl_1", topic_id="1", title="Visible Blake scene",
            performers=["Blake Blossom"], site="pornolab", source_rank=0,
        ))
        store_mod.upsert_item(_sample_item(
            id="xc_1", topic_id="1", title="Hidden Blake scene",
            performers=["BlakeBlossom"], site="xxxclub", source_rank=1,
        ))
        store_mod.upsert_item(_sample_item(
            id="xc_2", topic_id="2", title="Hidden alias scene",
            performers=["Blake B."], site="xxxclub", source_rank=2,
        ))
        client = app_mod.app.test_client()
        alias_response = client.post("/catalog/aliases", json={
            "kind": "actor", "alias": "Blake B.", "canonical_title": "Blake Blossom",
        })
        self.assertEqual(alias_response.status_code, 200)

        common_headers = {"Host": "192.168.1.2:9999"}
        general = client.get("/api/playa/v2/videos?page-size=20", headers=common_headers).get_json()
        self.assertEqual([row["id"] for row in general["data"]["content"]], ["pl_1"])

        actors = client.get("/api/playa/v2/actors?page-size=20", headers=common_headers).get_json()
        matches = [row for row in actors["data"]["content"] if row["title"] == "Blake Blossom"]
        self.assertEqual(len(matches), 1)
        actor_id = matches[0]["id"]
        actor_detail = client.get(f"/api/playa/v2/actor/{actor_id}", headers=common_headers).get_json()
        properties = {row["name"]: row["value"] for row in actor_detail["data"]["properties"]}
        self.assertEqual(properties["Videos"], "3")
        self.assertEqual(properties["XXXClub"], "2")

        actor_videos = client.get(
            f"/api/playa/v2/videos?actor={actor_id}&page-size=20", headers=common_headers,
        ).get_json()
        self.assertEqual(
            {row["id"] for row in actor_videos["data"]["content"]},
            {"pl_1", "xc_1", "xc_2"},
        )
        hidden_detail = client.get("/api/playa/v2/video/xc_1", headers=common_headers).get_json()
        self.assertEqual(hidden_detail["status"]["code"], 1)

    def test_external_actor_trailers_are_listed_and_playable(self):
        db_path = _temp_db(self)
        app_mod, store_mod = _load_app_for_db(db_path)
        store_mod.upsert_item(_sample_item(performers=["Blake Blossom"]))
        app_mod.config.ENABLE_EXTERNAL_TRAILERS = True

        def fake_get_json(url, headers=None):
            if "/actors?" in url:
                return {"status": {"code": 1}, "data": {"content": [
                    {"id": "blake-blossom", "title": "Blake Blossom", "preview": "https://cdn/actor.jpg"}
                ]}}
            if "/videos?" in url:
                return {"status": {"code": 1}, "data": {
                    "page_total": 1,
                    "content": [{
                        "id": "8544", "title": "Trailer scene", "subtitle": "RealJamVR",
                        "preview_image": "https://cdn/poster.jpg", "release_date": 1600000000,
                        "details": [{"type": "trailer", "duration_seconds": 73}, {"type": "full", "duration_seconds": 3200}],
                    }],
                }}
            if "/video/8544" in url:
                return {"status": {"code": 1}, "data": {
                    "id": "8544", "title": "Trailer scene", "preview_image": "https://cdn/poster.jpg",
                    "details": [
                        {"type": "trailer", "duration_seconds": 73, "links": [
                            {"is_stream": True, "url": "https://cdn/trailer.mp4", "projection": "180", "stereo": "LR"}
                        ]},
                        {"type": "full", "duration_seconds": 3200, "links": [
                            {"is_stream": True, "url": "https://cdn/premium.mp4"}
                        ]},
                    ],
                    "actors": [{"id": "blake-blossom", "title": "Blake Blossom"}],
                }}
            raise AssertionError(url)

        app_mod._external_get_json = fake_get_json
        client = app_mod.app.test_client()
        headers = {"Host": "192.168.1.2:9999"}
        actors = client.get("/api/playa/v2/actors", headers=headers).get_json()
        actor_id = actors["data"]["content"][0]["id"]
        videos = client.get(f"/api/playa/v2/videos?actor={actor_id}&page-size=20", headers=headers).get_json()
        trailer = next(row for row in videos["data"]["content"] if row["id"] == "trailer__vrporn__8544")
        self.assertEqual(trailer["details"][0]["duration_seconds"], 73)

        detail = client.get("/api/playa/v2/video/trailer__vrporn__8544", headers=headers).get_json()["data"]
        self.assertEqual([row["type"] for row in detail["details"]], ["trailer"])
        self.assertEqual(detail["details"][0]["links"][0]["url"], "https://cdn/trailer.mp4")

    def test_vrporn_playa_metadata_provider_uses_playa_preview_fields(self):
        db_path = _temp_db(self)
        app_mod, _store_mod = _load_app_for_db(db_path)
        calls = []

        def fake_get_json(url, headers=None):
            calls.append(url)
            if "/actors" in url:
                return {
                    "status": {"code": 1},
                    "data": {"content": [{"id": "blake-blossom", "title": "Blake Blossom", "preview": "https://cdn.vrporn.com/models/blake-blossom/profile.jpg"}]},
                }
            if "/actor/blake-blossom" in url:
                return {"status": {"code": 1}, "data": {"preview": "https://cdn.vrporn.com/models/blake-blossom/detail.jpg", "banner": None}}
            if "/studios" in url:
                return {
                    "status": {"code": 1},
                    "data": {"content": [{"id": "vrbangers", "title": "VR Bangers", "preview": "https://cdn.vrporn.com/studios/vrbangers/logo.jpg"}]},
                }
            if "/studio/vrbangers" in url:
                return {"status": {"code": 1}, "data": {"preview": "https://cdn.vrporn.com/studios/vrbangers/detail-logo.jpg", "description": "Studio description"}}
            raise AssertionError(url)

        app_mod._external_get_json = fake_get_json
        app_mod._ENTITY_DIRECTORY_CACHE.clear()
        actor = app_mod._fetch_vrporn_playa_entity_meta("actor", "Blake Blossom")
        self.assertEqual(actor["preview_url"], "https://cdn.vrporn.com/models/blake-blossom/profile.jpg")
        self.assertIn("/api/playa/v2/actor/blake-blossom", actor["source_url"])
        studio = app_mod._fetch_vrporn_playa_entity_meta("studio", "VR Bangers")
        self.assertEqual(studio["preview_url"], "https://cdn.vrporn.com/studios/vrbangers/logo.jpg")
        self.assertEqual(studio["description"], "")
        self.assertTrue(any("title=Blake+Blossom" in url for url in calls))
        self.assertTrue(any("/api/playa/v2/studios?page-size=1000" in url for url in calls))
        self.assertFalse(any("/api/playa/v2/studio/vrbangers" in url for url in calls))


if __name__ == "__main__":
    unittest.main()
