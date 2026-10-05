
# -*- coding: utf-8 -*-
"""
Наполняет базу РЕАЛЬНЫМИ карточками, спарсенными с pornolab.net (без логина - значит,
поле source пустое, "скачать" по-настоящему нельзя). Это только для проверки, что
теги/превью/длительность корректно уезжают в формат DeoVR.

Когда в config.py/переменных окружения будут заданы FORUM_USERNAME/FORUM_PASSWORD,
обычный scraper.refresh_all() будет находить и реальную ссылку на .torrent.
"""

import scraper
import store

store.init_db()

SAMPLE_TOPICS = [
    ("3313474", "https://pornolab.net/forum/viewtopic.php?t=3313474",
     "[VRPornJack / VRPorn.com] Suzzi Eis (Lights Out Lay) [2026 г., Hardcore, All Sex, VR, 60 FPS, 180°, 8K, 4096p] [Oculus Rift / Vive]"),
    ("3313365", "https://pornolab.net/forum/viewtopic.php?t=3313365",
     "[VRedging] Lexi Montana - Don't Get Too Excited for Lexi [2026, Blonde, Blowjob, Close Up, Handjob, Lingerie, POV, Piercing, Shaved, Kissing, SideBySide, 3072p, SiteRip]"),
]

for topic_id, topic_url, title in SAMPLE_TOPICS:
    item = scraper.parse_topic(topic_id, topic_url, title)
    if not item["source"]:
        item["source_type"] = "magnet"
        item["source"] = f"magnet:?xt=urn:btih:DEMO{topic_id}0000000000000000000000000&dn=demo"
    store.upsert_item(item)
    print("ok:", item["title"][:70], "| tags:", item["tags"])
