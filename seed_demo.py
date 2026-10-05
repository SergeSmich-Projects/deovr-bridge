
# -*- coding: utf-8 -*-
"""Наполняет локальную базу тестовыми карточками, чтобы проверить /deovr без реального форума."""

import store

store.init_db()

demo = [
    {
        "id": "1001",
        "title": "[VR 180][SBS] Demo Scene One",
        "tags": ["180", "SBS", "Demo"],
        "thumbnail_url": "https://picsum.photos/seed/1001/400/225",
        "source_type": "magnet",
        "source": "magnet:?xt=urn:btih:DEADBEEF0000000000000000000000000000beef&dn=demo1",
        "screen_type": "dome",
        "stereo_mode": "sbs",
        "video_length": 300,
    },
    {
        "id": "1002",
        "title": "[VR 360][TB] Demo Scene Two",
        "tags": ["360", "TB", "Demo"],
        "thumbnail_url": "https://picsum.photos/seed/1002/400/225",
        "source_type": "magnet",
        "source": "magnet:?xt=urn:btih:DEADBEEF0000000000000000000000000000beef&dn=demo2",
        "screen_type": "sphere",
        "stereo_mode": "tb",
        "video_length": 420,
    },
]

for item in demo:
    store.upsert_item(item)

print("OK, добавлено:", len(demo))
