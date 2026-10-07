# -*- coding: utf-8 -*-
"""Безопасный импорт VR magnet-ссылок из публичного Knaben API v1."""

import datetime as dt
import logging
import re
import time
import urllib.parse

import requests

import config
import store

log = logging.getLogger("knaben")

_HASH_RE = re.compile(r"^[0-9a-fA-F]{40}$|^[A-Z2-7]{32}$", re.IGNORECASE)
_VR_RE = re.compile(r"(?i)(?:\bvr\s*180\b|\bvr180\b|\bvirtual[ ._-]*reality\b|\b(?:sbs|side[ ._-]*by[ ._-]*side)\b)")
_HEIGHT_RE = re.compile(r"(?i)(?:^|\D)(2160|2560|2880|3072|3840|4096|4320|5120|5760|6144|7680|8192)p?(?:\D|$)")
_DANGEROUS_RE = re.compile(r"(?i)\.(?:exe|scr|bat|cmd|com|msi|ps1|vbs|js|jar|lnk)(?:\s|$)")
_ARCHIVE_ONLY_RE = re.compile(r"(?i)\.(?:zip|rar|7z|iso)(?:\s|$)")


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _infohash(hit: dict) -> str:
    value = str(hit.get("hash") or "").strip()
    if _HASH_RE.fullmatch(value):
        return value.lower()
    magnet = str(hit.get("magnetUrl") or "").strip()
    if not magnet.lower().startswith("magnet:?"):
        return ""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(magnet).query)
    for xt in query.get("xt", []):
        if xt.lower().startswith("urn:btih:"):
            value = xt.rsplit(":", 1)[-1].strip()
            if _HASH_RE.fullmatch(value):
                return value.lower()
    return ""


def _magnet(hit: dict, infohash: str) -> str:
    magnet = str(hit.get("magnetUrl") or "").strip()
    if magnet.lower().startswith("magnet:?"):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(magnet).query)
        magnet_hashes = [
            xt.rsplit(":", 1)[-1].strip().lower()
            for xt in query.get("xt", [])
            if xt.lower().startswith("urn:btih:")
        ]
        if infohash in magnet_hashes:
            return magnet
    title = str(hit.get("title") or "").strip()
    return f"magnet:?xt=urn:btih:{infohash}&dn={urllib.parse.quote_plus(title)}"


def _timestamp(value) -> int:
    text = str(value or "").strip()
    if not text:
        return int(time.time())
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError):
        return int(time.time())


def _video_height(title: str):
    matches = [_int(v) for v in _HEIGHT_RE.findall(title or "")]
    low = (title or "").casefold()
    # Маркетинговые 6K/8K в VR-релизах обычно описывают горизонталь SBS;
    # PLAY'A/TorrServe ожидают вертикальную высоту кадра.
    if re.search(r"(?:^|\W)8k(?:\W|$)", low):
        matches.append(4320)
    elif re.search(r"(?:^|\W)6k(?:\W|$)", low):
        matches.append(3072)
    return max(matches) if matches else None


def normalize_hit(hit: dict, rank: int = 0):
    """Преобразует ответ API в item либо отклоняет сомнительный результат."""
    title = " ".join(str(hit.get("title") or "").split())
    infohash = _infohash(hit)
    seeders = max(_int(hit.get("seeders")), 0)
    size = max(_int(hit.get("bytes")), 0)
    if not title or not infohash or not _VR_RE.search(title):
        return None
    if seeders < config.KNABEN_MIN_SEEDERS:
        return None
    if size < config.KNABEN_MIN_SIZE_BYTES or size > config.KNABEN_MAX_SIZE_BYTES:
        return None
    if _DANGEROUS_RE.search(title) or _ARCHIVE_ONLY_RE.search(title):
        return None

    tracker = " ".join(str(hit.get("tracker") or hit.get("cachedOrigin") or "").split())
    tags = ["VR", "VR180", "Knaben"]
    if tracker and tracker.casefold() != "knaben":
        tags.append(tracker)
    height = _video_height(title)
    if height:
        tags.append(f"{height}p")

    return {
        "id": f"kn_{infohash}",
        "topic_id": infohash,
        "title": title,
        "tags": tags,
        "thumbnail_url": "",
        "source_type": "magnet",
        "source": _magnet(hit, infohash),
        "screen_type": "dome",
        "stereo_mode": "sbs",
        "video_length": 0,
        "video_height": height,
        "created_at": _timestamp(hit.get("date") or hit.get("lastSeen")),
        "performers": [],
        "studio": [],
        "site": "knaben",
        "source_rank": rank,
        "seeders": seeders,
        # Knaben называет поле peers; храним его в существующей колонке,
        # которая в новых карточках PLAY'A уже отображается как P (peers).
        "leechers": max(_int(hit.get("peers")), 0),
        "screenshots": [],
    }


def search(query: str, session=None) -> list:
    query = " ".join((query or "").split())
    if not query:
        return []
    payload = {
        "search_type": "100%",
        "search_field": "title",
        "query": query,
        "order_by": "seeders",
        "order_direction": "desc",
        "from": 0,
        "size": min(max(config.KNABEN_RESULTS_PER_QUERY, 1), 300),
        "hide_unsafe": True,
        "hide_xxx": False,
    }
    if config.KNABEN_LAST_SEEN_SEC > 0:
        payload["seconds_since_last_seen"] = config.KNABEN_LAST_SEEN_SEC

    client = session or requests
    response = client.post(
        config.KNABEN_API_URL,
        json=payload,
        timeout=config.KNABEN_TIMEOUT_SEC,
        headers={"User-Agent": "deovr-bridge/1.0"},
    )
    response.raise_for_status()
    body = response.json()
    hits = body.get("hits", []) if isinstance(body, dict) else []
    return hits if isinstance(hits, list) else []


def refresh(session=None) -> int:
    """Ищет настроенные VR-запросы, удаляет дубли по infohash и пишет в БД."""
    if not config.ENABLE_KNABEN:
        return 0
    best = {}
    for query in config.KNABEN_QUERIES:
        for index, hit in enumerate(search(query, session=session)):
            item = normalize_hit(hit, rank=index)
            if not item:
                continue
            old = best.get(item["id"])
            if old is None or item["seeders"] > old["seeders"]:
                best[item["id"]] = item

    items = sorted(
        best.values(),
        key=lambda item: (-item["seeders"], -(item.get("video_height") or 0), item["title"].casefold()),
    )[: config.KNABEN_MAX_IMPORT_ITEMS]
    for rank, item in enumerate(items):
        item["source_rank"] = rank
        store.upsert_item(item)
    log.info("[knaben] Импортировано безопасных VR magnet-ссылок: %s", len(items))
    return len(items)
