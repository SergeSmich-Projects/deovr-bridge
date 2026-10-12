#!/usr/bin/env python3
"""
playa_matcher.py — Высокопроизводительная система индексации PLAY'A API v2
(vrporn.com, povr.com и др.) и сопоставления (matching) с раздачами Pornolab (раздел VR f=1823).

Ключевые особенности архитектуры:
1. Локальный индекс в SQLite (`playa_catalog.db`):
   - Загружает 47 500+ роликов с vrporn.com (всего ~24 запроса по page-size=2000)
     и 28 300+ роликов с povr.com (по студиям + общий список по page-size=400).
   - Учитывает различия реализаций PLAY'A API v2:
     * vrporn.com: 0-based `page-index`, хранит студию в `subtitle`, а слаг со студией и актрисами — в URL `preview_image`.
     * povr.com: 1-based `page-index`, игнорирует параметр `?title=`, оставляет `subtitle=""` пустым в общем списке,
       но отдаёт точные списки роликов по фильтру `?studio=<id>` (155 студий).
   - Автоматический троттлинг и обработка Cloudflare Rate Limit (HTTP 429 / error 1015 + заголовок `Retry-After`).
2. Парсер заголовков раздач Pornolab VR (`parse_pornolab_title`):
   - Извлекает студию, исходный сайт, список актрис/актёров, чистое название сцены,
     внешний ID ролика в скобках `(6378455)`, дату релиза (`YYYY-MM-DD`, `DD.MM.YYYY`, `YYYY`), теги и разрешение.
   - Корректно разбирает вложенные скобки вида `Kenna James (The Mask (A XXX Parody) / 01.04.2022)`.
3. Многоступенчатый движок сопоставления (`PlayaMatcher`):
   - Блокировка кандидатов (Inverted Index в памяти по ID, нормализованному названию, студии и редким токенам):
     сопоставление 35 000 раздач с 75 000 роликами API занимает ~3-7 секунд вместо часов полного перебора.
   - Нормализация римских цифр (`Part II` <-> `Pt.2`), разговорных окончаний (`Bangin` <-> `Banging`),
     встроенных в `title` имён актрис (`Ariana, Lexy Emerald - My Fairy-Tale Prince`, `King MILF - Karina King`,
     `Maddy Black in Straight into it!`, `From The Vault: ...`).
   - Опциональная дозагрузка деталей `GET /video/<id>` (`verify_details=True`) только для спорных/коротких названий
     с вечным кэшированием списка `actors` и `studio` в SQLite.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

try:
    from rapidfuzz import fuzz
    from rapidfuzz.distance import Levenshtein
    HAS_RAPIDFUZZ = True
except ImportError:
    import difflib
    HAS_RAPIDFUZZ = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("playa_matcher")


# =============================================================================
# 1. НОРМАЛИЗАЦИЯ ТЕКСТА, НАЗВАНИЙ, СТУДИЙ И ИМЁН АКТОРОВ
# =============================================================================

ROMAN_MAP = {
    "xii": "12", "xi": "11", "x": "10", "ix": "9", "viii": "8",
    "vii": "7", "vi": "6", "v": "5", "iv": "4", "iii": "3", "ii": "2", "i": "1",
}

STOPWORDS = {
    "the", "a", "an", "in", "on", "of", "at", "to", "for", "with", "and", "by",
    "vr", "xxx", "porn", "video", "virtual", "reality", "passthrough", "pmv",
}

# Частые разговорные сокращения в заголовках раздач/сцен
COLLOQUIAL_PATTERNS = [
    (re.compile(r"\bbangin\b"), "banging"),
    (re.compile(r"\bfuckin\b"), "fucking"),
    (re.compile(r"\blovin\b"), "loving"),
    (re.compile(r"\bsuckin\b"), "sucking"),
    (re.compile(r"\brickin\b"), "licking"),
    (re.compile(r"\bchillin\b"), "chilling"),
    (re.compile(r"\bplayin\b"), "playing"),
    (re.compile(r"\bridin\b"), "riding"),
    (re.compile(r"\bgettin\b"), "getting"),
    (re.compile(r"\bmakin\b"), "making"),
]

# Известные склейки/алиасы студий и слов в названиях
WORD_CANONICAL_MAP = {
    "virtualreal": "virtual real",
    "lil": "little",
}

STUDIO_ALIASES = {
    "slroriginals": "sexlikereal originals",
    "slr": "sexlikereal",
    "povcentralvr": "povcentral",
    "povcentral": "povcentral",
    "allanalvr": "allanal",
    "allanal": "allanal",
    "18vr": "18",
    "vrconk": "conk",
    "vrbangers": "bangers",
    "vrhush": "hush",
    "vrmagic": "magic",
    "vrdome": "dome",
    "vrplayful": "playful",
    "vrmassage": "massage",
    "vrmassaged": "massage",
    "littlecapricedreams": "littlecaprice",
    "littlecapricevr": "littlecaprice",
    "lewdfraggypremium": "lewdfraggy",
    "lewdfraggy": "lewdfraggy",
    "realityloverspassthrough": "realitylovers",
}


def strip_accents(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c))


def normalize_studio(name: str) -> str:
    """
    Приводит название студии к каноническому ключу без шума:
    'Lewd Fraggy Premium VR' -> 'lewdfraggy'
    'All Anal VR' / 'AllAnalVR' -> 'allanal'
    'POV Central' / 'POVcentralVR' -> 'povcentral'
    'Teddy Tarantino 8k' / 'Teddy Tarantino VR' -> 'teddytarantino'
    """
    if not name:
        return ""
    s = strip_accents(name.lower())
    s = re.sub(r"\.(com|net|org|tv|xxx|io)\b", "", s)
    # Убираем суффиксы/префиксы VR, 8k, passthrough, premium, studio, productions
    s = re.sub(r"\b(8k|6k|5k|4k|hd|vr|passthrough|ar|premium|studios?|productions?)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    # Дополнительно проверяем хвостовой или начальный 'vr' при слитном написании (AllAnalVR -> allanal)
    if len(s) > 4 and s.endswith("vr"):
        s = s[:-2]
    if len(s) > 4 and s.startswith("vr"):
        s_no_vr = s[2:]
        if f"vr{s_no_vr}" in STUDIO_ALIASES:
            return STUDIO_ALIASES[f"vr{s_no_vr}"]
        return s_no_vr
    return STUDIO_ALIASES.get(s, s)


def normalize_actor_name(name: str) -> str:
    """
    Канонизация имени актрисы/актёра:
    'Angelika Grays' / 'Angelica Grays' -> учитываем и точное, и фонетически близкое.
    """
    if not name:
        return ""
    s = strip_accents(name.lower())
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def normalize_title(title: str) -> str:
    """
    Канонизация названия сцены:
    - Приводит кавычки/тире к ASCII
    - Убирает пометки [Passthrough], [VR PMV], From The Vault:
    - Унифицирует Part II / Pt. 2 / Pt.2 -> part 2, Episode IV / Ep. 4 -> episode 4
    - Унифицирует Bangin -> Banging и VirtualReal -> Virtual Real
    """
    if not title:
        return ""
    s = strip_accents(title.lower())
    # Убираем технические префиксы/суффиксы из заголовков API
    s = re.sub(r"^\s*from the vault\s*:\s*", "", s)
    s = re.sub(r"\[(passthrough|vr pmv|pmv|bonus[^\]]*|bts)\]", " ", s)

    # Заменяем дефисы/пунктуацию на пробелы, сохраняя буквы и цифры
    s = s.replace("&", " and ")
    # Приводим pt.2 / pt 2 / part ii -> part 2
    s = re.sub(
        r"\b(?:part|pt)\.?\s*(xii|xi|x|ix|viii|vii|vi|v|iv|iii|ii|i|\d+)\b",
        lambda m: f" part {ROMAN_MAP.get(m.group(1), m.group(1))} ",
        s,
    )
    s = re.sub(
        r"\b(?:episode|ep)\.?\s*(xii|xi|x|ix|viii|vii|vi|v|iv|iii|ii|i|\d+)\b",
        lambda m: f" episode {ROMAN_MAP.get(m.group(1), m.group(1))} ",
        s,
    )

    s = re.sub(r"[^a-z0-9\s]", " ", s)
    for pat, repl in COLLOQUIAL_PATTERNS:
        s = pat.sub(repl, s)

    tokens = []
    for tok in s.split():
        canon = WORD_CANONICAL_MAP.get(tok, tok)
        tokens.extend(canon.split())

    return " ".join(tokens)


def strip_actors_from_title(norm_title: str, actor_names: Iterable[str]) -> str:
    """
    В API (особенно VRPorn и POVR) имя актрисы часто вшито прямо в `title`:
    - 'Ariana, Lexy Emerald - My Fairy-Tale Prince'
    - 'King MILF - Karina King'
    - 'Maddy Black in Straight into it!'
    - 'Barbie Sins: Lingerie Blowjob'
    Эта функция удаляет известные имена актрис (и связки 'in', 'with') из нормализованного названия.
    """
    s = norm_title
    for actor in actor_names:
        na = normalize_title(actor)
        if not na:
            continue
        # В начале: "<actor> in <title>" или "<actor> <title>"
        s = re.sub(rf"^{re.escape(na)}\s+(?:in\s+)?", "", s)
        # В конце: "<title> with <actor>" или "<title> <actor>"
        s = re.sub(rf"\s+(?:with\s+)?{re.escape(na)}$", "", s)
        # Если несколько актрис перечислялись в начале
        s = re.sub(rf"\b{re.escape(na)}\b", " ", s)
    s = re.sub(r"^\s*(?:in|with|and)\s+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if s else norm_title


def extract_slug_tokens_from_preview(url: str) -> str:
    """
    На vrporn.com URL превью часто содержит полный слаг с названием, студией и актрисой:
    https://mcdn.vrporn.com/files/20201006223649/cam-love-sexbabesvr-tiffany-tatum-vr-porn-video-vrporn-com-virtual-reality-1.jpg
    """
    if not url:
        return ""
    path = urllib.parse.urlparse(url).path
    filename = path.rsplit("/", 1)[-1].lower()
    filename = re.sub(r"\.(jpg|jpeg|png|webp)$", "", filename)
    # Убираем стандартные суффиксы vrporn
    filename = re.sub(r"-(vr-porn-video|vrporn-com|virtual-reality|poster|small|680)(-\d+)?", " ", filename)
    filename = re.sub(r"[^a-z0-9]+", " ", filename)
    return re.sub(r"\s+", " ", filename).strip()


def tokenize_significant(norm_text: str) -> List[str]:
    """Токены длиннее 1 символа кроме стоп-слов для инвертированного индекса."""
    return [t for t in norm_text.split() if len(t) >= 2 and t not in STOPWORDS]


# =============================================================================
# 2. ПАРСЕР ЗАГОЛОВКОВ РАЗДАЧ PORNOLAB (РАЗДЕЛ VR f=1823)
# =============================================================================

@dataclass
class ParsedTorrent:
    raw_line: str
    torrent_id: Optional[str] = None
    raw_studio_bracket: str = ""
    studio: str = ""
    studio_norm: str = ""
    source_site: str = ""
    actors: List[str] = field(default_factory=list)
    actors_norm: List[str] = field(default_factory=list)
    scene_title: str = ""
    scene_title_norm: str = ""
    scene_title_no_actors_norm: str = ""
    external_id: Optional[str] = None
    release_date: Optional[str] = None      # YYYY-MM-DD
    release_timestamp: Optional[int] = None # Unix timestamp (UTC полночь)
    release_year: Optional[int] = None      # YYYY
    tags: List[str] = field(default_factory=list)
    resolution: Optional[str] = None


def _parse_date_str(s: str) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    s = s.strip()
    # YYYY-MM-DD
    m = re.search(r"\b(20\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])\b", s)
    if m:
        iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        ts = int(dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=dt.timezone.utc).timestamp())
        return iso, ts, int(m.group(1))
    # DD.MM.YYYY
    m = re.search(r"\b(0[1-9]|[12]\d|3[01])\.(0[1-9]|1[0-2])\.(20\d{2})\b", s)
    if m:
        iso = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        ts = int(dt.datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=dt.timezone.utc).timestamp())
        return iso, ts, int(m.group(3))
    # YYYY
    m = re.search(r"\b(199\d|20\d{2})\b", s)
    if m:
        return None, None, int(m.group(1))
    return None, None, None


def _clean_studio_and_site(bracket_content: str) -> Tuple[str, str]:
    parts = [p.strip() for p in bracket_content.split("/")]
    if len(parts) >= 2:
        studio_part = parts[0]
        site_part = parts[-1].lower()
    else:
        single = parts[0]
        if re.search(r"\.(com|net|org|tv|xxx)$", single, re.I):
            site_part = single.lower()
            studio_part = re.sub(r"\.(com|net|org|tv|xxx)$", "", single, flags=re.I)
        else:
            studio_part = single
            site_part = ""
    return studio_part.strip(), site_part.strip()


def _extract_outer_parens_at_end(s: str) -> Optional[Tuple[str, str]]:
    """
    Корректно выделяет сбалансированные скобки в конце строки,
    поддерживая вложенные скобки:
    'Kenna James (The Mask (A XXX Parody) / 01.04.2022)'
    -> ('Kenna James', 'The Mask (A XXX Parody) / 01.04.2022')
    """
    s = s.strip()
    if not s.endswith(")"):
        return None
    depth = 0
    for i in range(len(s) - 1, -1, -1):
        if s[i] == ")":
            depth += 1
        elif s[i] == "(":
            depth -= 1
            if depth == 0:
                return s[:i].strip(), s[i + 1 : -1].strip()
    return None


def parse_pornolab_title(raw: str, torrent_id: Optional[str] = None) -> ParsedTorrent:
    """
    Разбирает строку заголовка раздачи с Pornolab (f=1823) во все структурные поля.
    """
    s = raw.strip()
    # На случай если в начале строки есть префиксы трекера вроде "√ · "
    s = re.sub(r"^[√✓•·\-\s]+", "", s)

    m_lead = re.match(r"^\[([^\]]+)\]\s*(.*)$", s)
    if m_lead:
        raw_studio_bracket = m_lead.group(1).strip()
        rest = m_lead.group(2).strip()
    else:
        raw_studio_bracket = ""
        rest = s

    studio, source_site = _clean_studio_and_site(raw_studio_bracket) if raw_studio_bracket else ("", "")

    trailing_brackets: List[str] = []
    while True:
        m_tail = re.search(r"\s*\[([^\]]+)\]\s*$", rest)
        if m_tail:
            trailing_brackets.insert(0, m_tail.group(1).strip())
            rest = rest[: m_tail.start()].strip()
        else:
            break

    rel_date, rel_ts, rel_year = None, None, None
    tags: List[str] = []
    resolution: Optional[str] = None

    if trailing_brackets:
        meta_parts = [p.strip() for p in trailing_brackets[0].split(",")]
        if meta_parts:
            d_str, ts_int, y_int = _parse_date_str(meta_parts[0])
            if y_int:
                rel_date, rel_ts, rel_year = d_str, ts_int, y_int
                meta_parts = meta_parts[1:]
            for mp in meta_parts:
                if re.match(r"^\d{3,4}p$", mp, re.I):
                    resolution = mp.lower()
                elif mp:
                    tags.append(mp)

    core = rest.strip()
    external_id: Optional[str] = None
    actors: List[str] = []
    scene_title = ""

    # Внешний числовой ID сайта в конце: (6378455) или (96899)
    m_id = re.search(r"\s*\((\d{4,10})\)\s*$", core)
    if m_id:
        external_id = m_id.group(1)
        core = core[: m_id.start()].strip()

    if " - " in core:
        parts = [p.strip() for p in core.split(" - ")]
        actor_part = parts[0]
        title_parts = parts[1:]
        # Если в конце перед (ID) повторно указана студия: "... - Teddy Tarantino"
        if len(title_parts) >= 2:
            last_norm = normalize_studio(title_parts[-1])
            st_norm = normalize_studio(studio)
            if st_norm and last_norm and (last_norm == st_norm or last_norm in st_norm or st_norm in last_norm):
                title_parts = title_parts[:-1]
        scene_title = " - ".join(title_parts).strip()
        actors = [a.strip() for a in actor_part.split(",") if a.strip()]
    else:
        paren_split = _extract_outer_parens_at_end(core)
        if paren_split:
            actor_part, inside_parens = paren_split
            m_inline_date = re.search(
                r"^(.*?)\s*/\s*(\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})\s*$",
                inside_parens,
            )
            if m_inline_date:
                scene_title = m_inline_date.group(1).strip()
                d_in, ts_in, y_in = _parse_date_str(m_inline_date.group(2))
                if d_in:
                    rel_date, rel_ts = d_in, ts_in
                if y_in and not rel_year:
                    rel_year = y_in
            else:
                scene_title = inside_parens.strip()
            actors = [a.strip() for a in actor_part.split(",") if a.strip()]
        else:
            scene_title = core

    studio_norm = normalize_studio(studio)
    # Если в скобках был только сайт типа [POVR.com], то studio=='POVR' — это агрегатор, а не конкретная студия
    if studio_norm in {"povr", "vrporn", "sexlikereal"}:
        if not source_site:
            source_site = f"{studio_norm}.com"

    actors_norm = [normalize_actor_name(a) for a in actors if normalize_actor_name(a)]
    scene_title_norm = normalize_title(scene_title)
    scene_title_no_actors_norm = strip_actors_from_title(scene_title_norm, actors)

    return ParsedTorrent(
        raw_line=raw,
        torrent_id=torrent_id,
        raw_studio_bracket=raw_studio_bracket,
        studio=studio,
        studio_norm=studio_norm,
        source_site=source_site,
        actors=actors,
        actors_norm=actors_norm,
        scene_title=scene_title,
        scene_title_norm=scene_title_norm,
        scene_title_no_actors_norm=scene_title_no_actors_norm,
        external_id=external_id,
        release_date=rel_date,
        release_timestamp=rel_ts,
        release_year=rel_year,
        tags=tags,
        resolution=resolution,
    )


# =============================================================================
# 3. КЛИЕНТ PLAY'A API v2 И ЛОКАЛЬНОЕ ХРАНИЛИЩЕ SQLITE (`PlayaCatalogDB`)
# =============================================================================

@dataclass
class ApiVideoRecord:
    site: str                  # 'vrporn.com' | 'povr.com'
    video_id: str              # ID в API
    title: str                 # Оригинальный title
    title_norm: str            # Нормализованный title
    title_clean_norm: str      # Нормализованный title без встроенных имён актрис
    studio: str                # Название студии
    studio_norm: str           # Нормализованный ключ студии
    release_date: int          # Unix timestamp
    release_year: int          # Год релиза (UTC)
    duration_seconds: int      # Длительность полной версии или трейлера
    preview_image: str         # Ссылка на обложку
    preview_slug_norm: str     # Токены из URL обложки (часто содержат актрису и студию)
    actors_json: str = "[]"    # JSON-список актрис (заполняется из слага/названия или /video/<id>)
    details_fetched: int = 0   # 1, если запрашивали /video/<id>


class PlayaCatalogDB:
    """
    Локальная SQLite-база для кэширования каталога PLAY'A API v2
    (видео, студии, актёры и состояние синхронизации).
    """

    def __init__(self, db_path: str = "playa_catalog.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # WAL mode для лучшей производительности при многопоточном доступе
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;

            CREATE TABLE IF NOT EXISTS videos (
                site TEXT NOT NULL,
                video_id TEXT NOT NULL,
                title TEXT NOT NULL,
                title_norm TEXT NOT NULL,
                title_clean_norm TEXT NOT NULL,
                studio TEXT DEFAULT '',
                studio_norm TEXT DEFAULT '',
                release_date INTEGER DEFAULT 0,
                release_year INTEGER DEFAULT 0,
                duration_seconds INTEGER DEFAULT 0,
                preview_image TEXT DEFAULT '',
                preview_slug_norm TEXT DEFAULT '',
                actors_json TEXT DEFAULT '[]',
                details_fetched INTEGER DEFAULT 0,
                PRIMARY KEY (site, video_id)
            );

            CREATE INDEX IF NOT EXISTS idx_videos_title_norm ON videos(title_norm);
            CREATE INDEX IF NOT EXISTS idx_videos_studio_norm ON videos(studio_norm);

            CREATE TABLE IF NOT EXISTS studios (
                site TEXT NOT NULL,
                studio_id TEXT NOT NULL,
                title TEXT NOT NULL,
                studio_norm TEXT NOT NULL,
                PRIMARY KEY (site, studio_id)
            );

            CREATE TABLE IF NOT EXISTS actors (
                site TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                title TEXT NOT NULL,
                actor_norm TEXT NOT NULL,
                PRIMARY KEY (site, actor_id)
            );

            CREATE TABLE IF NOT EXISTS sync_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.conn.commit()

    def upsert_videos(self, records: List[ApiVideoRecord]) -> None:
        if not records:
            return
        cur = self.conn.cursor()
        cur.executemany(
            """
            INSERT INTO videos (
                site, video_id, title, title_norm, title_clean_norm,
                studio, studio_norm, release_date, release_year,
                duration_seconds, preview_image, preview_slug_norm,
                actors_json, details_fetched
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(site, video_id) DO UPDATE SET
                title = excluded.title,
                title_norm = excluded.title_norm,
                title_clean_norm = excluded.title_clean_norm,
                studio = CASE WHEN excluded.studio != '' THEN excluded.studio ELSE videos.studio END,
                studio_norm = CASE WHEN excluded.studio_norm != '' THEN excluded.studio_norm ELSE videos.studio_norm END,
                release_date = CASE WHEN excluded.release_date > 0 THEN excluded.release_date ELSE videos.release_date END,
                release_year = CASE WHEN excluded.release_year > 0 THEN excluded.release_year ELSE videos.release_year END,
                duration_seconds = CASE WHEN excluded.duration_seconds > 0 THEN excluded.duration_seconds ELSE videos.duration_seconds END,
                preview_image = CASE WHEN excluded.preview_image != '' THEN excluded.preview_image ELSE videos.preview_image END,
                preview_slug_norm = CASE WHEN excluded.preview_slug_norm != '' THEN excluded.preview_slug_norm ELSE videos.preview_slug_norm END,
                actors_json = CASE WHEN excluded.details_fetched = 1 OR videos.actors_json = '[]' THEN excluded.actors_json ELSE videos.actors_json END,
                details_fetched = MAX(videos.details_fetched, excluded.details_fetched)
            """,
            [
                (
                    r.site,
                    str(r.video_id),
                    r.title,
                    r.title_norm,
                    r.title_clean_norm,
                    r.studio,
                    r.studio_norm,
                    r.release_date,
                    r.release_year,
                    r.duration_seconds,
                    r.preview_image,
                    r.preview_slug_norm,
                    r.actors_json,
                    r.details_fetched,
                )
                for r in records
            ],
        )
        self.conn.commit()

    def load_all_videos(self) -> List[ApiVideoRecord]:
        cur = self.conn.cursor()
        rows = cur.execute("SELECT * FROM videos").fetchall()
        return [
            ApiVideoRecord(
                site=row["site"],
                video_id=row["video_id"],
                title=row["title"],
                title_norm=row["title_norm"],
                title_clean_norm=row["title_clean_norm"],
                studio=row["studio"],
                studio_norm=row["studio_norm"],
                release_date=row["release_date"],
                release_year=row["release_year"],
                duration_seconds=row["duration_seconds"],
                preview_image=row["preview_image"],
                preview_slug_norm=row["preview_slug_norm"],
                actors_json=row["actors_json"],
                details_fetched=row["details_fetched"],
            )
            for row in rows
        ]

    def count_videos(self, site: Optional[str] = None) -> int:
        cur = self.conn.cursor()
        if site:
            return cur.execute("SELECT COUNT(*) FROM videos WHERE site = ?", (site,)).fetchone()[0]
        return cur.execute("SELECT COUNT(*) FROM videos").fetchone()[0]


class PlayaApiClient:
    """
    HTTP-клиент для PLAY'A API v2 с поддержкой троттлинга, автоматических повторов
    при 429 TooManyRequests / 503 / 504 и учётом особенностей vrporn.com и povr.com.
    """

    def __init__(self, base_domain: str, request_delay: float = 1.2, timeout: float = 25.0):
        # Приводим к виду 'vrporn.com' или 'povr.com'
        parsed = urllib.parse.urlparse(base_domain if "://" in base_domain else f"https://{base_domain}")
        self.site = parsed.netloc.lower().removeprefix("www.")
        self.base_url = f"https://{self.site}/api/playa/v2"
        self.request_delay = request_delay
        self.timeout = timeout
        self._last_req_ts = 0.0

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None, max_retries: int = 5) -> Dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            url = f"{url}?{q}"

        for attempt in range(1, max_retries + 1):
            elapsed = time.time() - self._last_req_ts
            if elapsed < self.request_delay:
                time.sleep(self.request_delay - elapsed)

            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "PlayaVR/2.0",
                    "Accept": "application/json",
                },
            )
            try:
                self._last_req_ts = time.time()
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                    payload = json.loads(raw)
                    status = payload.get("status", {})
                    if status.get("code", 1) != 1:
                        raise RuntimeError(f"API error status={status} on {url}")
                    return payload.get("data")
            except urllib.error.HTTPError as e:
                if e.code in (429, 503, 504):
                    retry_after = e.headers.get("Retry-After")
                    wait_sec = float(retry_after) + 1.0 if (retry_after and retry_after.isdigit()) else (10.0 * attempt)
                    logger.warning(
                        "[%s] HTTP %d на %s (попытка %d/%d). Ожидание %.1f сек...",
                        self.site, e.code, url, attempt, max_retries, wait_sec,
                    )
                    time.sleep(wait_sec)
                    continue
                raise
            except Exception as e:
                if attempt < max_retries:
                    wait_sec = 3.0 * attempt
                    logger.warning("[%s] Ошибка %s на %s. Повтор через %.1f с...", self.site, e, url, wait_sec)
                    time.sleep(wait_sec)
                    continue
                raise
        raise RuntimeError(f"Не удалось выполнить запрос после {max_retries} попыток: {url}")

    @staticmethod
    def item_to_record(site: str, item: Dict[str, Any], studio_override: str = "") -> ApiVideoRecord:
        vid_id = str(item.get("id", ""))
        title = (item.get("title") or "").strip()
        # На vrporn.com студия лежит в subtitle; на povr.com в списке /videos subtitle пустой
        studio = studio_override or (item.get("subtitle") or "").strip()
        if isinstance(item.get("studio"), dict):
            studio = item["studio"].get("title") or studio

        rel_ts = int(item.get("release_date") or 0)
        rel_year = dt.datetime.fromtimestamp(rel_ts, tz=dt.timezone.utc).year if rel_ts > 0 else 0

        # Длительность: предпочитаем full, иначе trailer
        dur = 0
        for det in item.get("details") or []:
            if det.get("type") == "full" and det.get("duration_seconds"):
                dur = int(det["duration_seconds"])
                break
            elif det.get("duration_seconds") and not dur:
                dur = int(det["duration_seconds"])

        preview_url = item.get("preview_image") or ""
        preview_slug = extract_slug_tokens_from_preview(preview_url)

        actors_list: List[str] = []
        if isinstance(item.get("actors"), list):
            actors_list = [a.get("title", "").strip() for a in item["actors"] if a.get("title")]

        title_norm = normalize_title(title)
        # Если в самом title есть разделитель " - " ("Ariana, Lexy Emerald - My Fairy-Tale Prince"
        # или "King MILF - Karina King"), сохраняем также очищенный вариант
        title_clean_norm = strip_actors_from_title(title_norm, actors_list)
        if " - " in title:
            parts = [normalize_title(p) for p in title.split(" - ") if p.strip()]
            if len(parts) == 2 and not actors_list:
                # Сохраняем обе части, а в clean_norm кладём более информативную или склеенную
                pass

        return ApiVideoRecord(
            site=site,
            video_id=vid_id,
            title=title,
            title_norm=title_norm,
            title_clean_norm=title_clean_norm,
            studio=studio,
            studio_norm=normalize_studio(studio),
            release_date=rel_ts,
            release_year=rel_year,
            duration_seconds=dur,
            preview_image=preview_url,
            preview_slug_norm=preview_slug,
            actors_json=json.dumps(actors_list, ensure_ascii=False),
            details_fetched=1 if "actors" in item else 0,
        )

    def sync_studios(self, db: PlayaCatalogDB) -> List[Dict[str, str]]:
        """Загружает полный справочник студий сайта (1 запрос)."""
        start_idx = 1 if self.site == "povr.com" else 0
        data = self.get_json("studios", {"page-index": start_idx, "page-size": 500})
        studios = data.get("content") or []
        cur = db.conn.cursor()
        cur.executemany(
            "INSERT OR REPLACE INTO studios (site, studio_id, title, studio_norm) VALUES (?, ?, ?, ?)",
            [(self.site, str(s["id"]), s["title"], normalize_studio(s["title"])) for s in studios],
        )
        db.conn.commit()
        logger.info("[%s] Синхронизировано студий: %d", self.site, len(studios))
        return studios

    def sync_catalog(
        self,
        db: PlayaCatalogDB,
        max_pages: Optional[int] = None,
        by_studios_on_povr: bool = True,
    ) -> int:
        """
        Полная или частичная синхронизация каталога видео в локальный SQLite.
        - Для vrporn.com: запрашивает по 2000 видео на страницу (0-based, всего ~24 страницы).
        - Для povr.com: запрашивает по 400 видео на страницу (1-based) + обходит студии,
          чтобы заполнить поле `studio` (так как в общем `/videos` на POVR `subtitle` пустой).
        """
        studios = self.sync_studios(db)
        total_saved = 0

        if self.site == "povr.com" and by_studios_on_povr and studios and max_pages is None:
            logger.info("[povr.com] Индексация видео по %d студиям (для получения точных названий студий)...", len(studios))
            for idx, st in enumerate(studios, 1):
                sid, stitle = str(st["id"]), st["title"]
                page = 1
                while True:
                    data = self.get_json("videos", {"page-index": page, "page-size": 400, "studio": sid})
                    content = data.get("content") or []
                    if not content:
                        break
                    records = [self.item_to_record(self.site, item, studio_override=stitle) for item in content]
                    db.upsert_videos(records)
                    total_saved += len(records)
                    if page >= int(data.get("page_total") or 1):
                        break
                    page += 1
                if idx % 25 == 0 or idx == len(studios):
                    logger.info("[povr.com] Обработано студий: %d/%d (сохранено записей: %d)", idx, len(studios), total_saved)

        # Общий проход по /videos (для vrporn.com это основной проход по 2000 шт., для povr.com — добор видео без студии)
        is_one_based = self.site == "povr.com"
        page_size = 400 if self.site == "povr.com" else 2000
        start_page = 1 if is_one_based else 0
        page = start_page
        pages_fetched = 0

        while True:
            params: Dict[str, Any] = {
                "page-index": page,
                "page-size": page_size,
                "order": "release_date",
                "direction": "desc",
            }
            data = self.get_json("videos", params)
            content = data.get("content") or []
            if not content:
                break

            records = [self.item_to_record(self.site, item) for item in content]
            db.upsert_videos(records)
            total_saved += len(records)
            pages_fetched += 1

            page_total = int(data.get("page_total") or 1)
            logger.info(
                "[%s] Страница %d/%d (получено %d видео, всего в базе по сайту: %d)",
                self.site,
                pages_fetched,
                page_total,
                len(records),
                db.count_videos(self.site),
            )

            if max_pages and pages_fetched >= max_pages:
                break
            if is_one_based and page >= page_total:
                break
            if not is_one_based and page >= page_total - 1:
                break
            page += 1

        return total_saved

    def fetch_video_details(self, db: PlayaCatalogDB, video_id: str) -> Optional[ApiVideoRecord]:
        """Точечный запрос GET /video/<id> с сохранением актрис и студии в SQLite."""
        data = self.get_json(f"video/{video_id}")
        if not data:
            return None
        rec = self.item_to_record(self.site, data)
        rec.details_fetched = 1
        db.upsert_videos([rec])
        return rec


# =============================================================================
# 4. БЫСТРЫЙ ДВИЖОК СОПОСТАВЛЕНИЯ С ИНВЕРТИРОВАННЫМ ИНДЕКСОМ (`PlayaMatcher`)
# =============================================================================

@dataclass
class MatchCandidate:
    site: str
    video_id: str
    api_title: str
    api_studio: str
    api_release_date: str
    api_actors: List[str]
    confidence: float
    status: str              # 'auto_matched' | 'review' | 'unmatched'
    match_method: str        # 'exact_id' | 'exact_studio_title' | 'fuzzy_multi_feature'
    score_breakdown: Dict[str, float]


def _string_similarity(a: str, b: str) -> float:
    """
    Возвращает строгое полнострочное сходство названий в диапазоне 0..100.
    Не использует partial_ratio/WRatio, чтобы общие короткие слова ('of', 'part 2', 'the')
    не давали ложных 80%+ на совершенно разных названиях.
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 100.0
    if HAS_RAPIDFUZZ:
        r_score = fuzz.ratio(a, b)
        ts_score = fuzz.token_sort_ratio(a, b)
        # Берем максимум из прямого ratio и слегка взвешенного token_sort_ratio
        base = max(r_score, ts_score * 0.96)
    else:
        base = difflib.SequenceMatcher(None, a, b).ratio() * 100.0

    # Проверяем пересечение значимых токенов (без стоп-слов):
    toks_a = set(tokenize_significant(a))
    toks_b = set(tokenize_significant(b))
    if toks_a and toks_b:
        inter = toks_a & toks_b
        if not inter:
            # Разрешаем высокий балл без общих токенов только для коротких опечаток в 1 букву (Cum Love <-> Cam Love)
            if len(a) > 12 or len(b) > 12:
                base = min(base, 55.0)
        elif len(toks_a) >= 2 and len(toks_b) >= 2:
            # Если совпало только 1 слово из нескольких (например, 'Language of Love' vs 'Language Lessons'),
            # и редакторское расстояние велико (> 3 символов), штрафуем ложное совпадение по общему префиксу
            union_ratio = len(inter) / max(len(toks_a), len(toks_b))
            if union_ratio <= 0.5 and abs(len(a) - len(b)) + (len(a) - len(b) == 0) > 0:
                if HAS_RAPIDFUZZ and Levenshtein.distance(a, b) > 3:
                    base = min(base, 64.0)
    return base


def _actor_in_text(actor_norm: str, text_norm: str) -> bool:
    """
    Проверяет вхождение имени актрисы в нормализованный текст (title или preview_slug),
    учитывая мелкие опечатки (например, Angelika Grays <-> Angelica Grays, Bradburry <-> Bradbury).
    """
    if not actor_norm or not text_norm:
        return False
    if actor_norm in text_norm:
        return True
    actor_tokens = actor_norm.split()
    text_tokens = text_norm.split()
    if not actor_tokens or not text_tokens:
        return False
    # Проверяем, есть ли для каждого слова имени актрисы токен с расстоянием Левенштейна <= 1
    matched_tokens = 0
    for at in actor_tokens:
        if len(at) <= 2:
            if at in text_tokens:
                matched_tokens += 1
            continue
        for tt in text_tokens:
            if at == tt:
                matched_tokens += 1
                break
            if abs(len(at) - len(tt)) <= 1 and len(at) >= 5:
                if HAS_RAPIDFUZZ:
                    if Levenshtein.distance(at, tt) <= 1:
                        matched_tokens += 1
                        break
                else:
                    if difflib.SequenceMatcher(None, at, tt).ratio() >= 0.84:
                        matched_tokens += 1
                        break
    return matched_tokens == len(actor_tokens)


class PlayaMatcher:
    """
    Индексирует локальную базу видео в оперативной памяти за ~0.4 сек
    и сопоставляет десятки тысяч раздач со скоростью ~5 000–10 000 раздач/сек.
    """

    def __init__(
        self,
        db: PlayaCatalogDB,
        api_clients: Optional[Dict[str, PlayaApiClient]] = None,
        auto_match_threshold: float = 80.0,
        review_threshold: float = 62.0,
    ):
        self.db = db
        self.api_clients = api_clients or {}
        self.auto_match_threshold = auto_match_threshold
        self.review_threshold = review_threshold

        # Инвертированные индексы в памяти
        self.videos: List[ApiVideoRecord] = []
        self.by_site_id: Dict[Tuple[str, str], int] = {}
        self.by_id_any_site: Dict[str, List[int]] = defaultdict(list)
        self.by_exact_title: Dict[str, List[int]] = defaultdict(list)
        self.by_studio: Dict[str, List[int]] = defaultdict(list)
        self.by_token: Dict[str, List[int]] = defaultdict(list)
        self.token_idf: Dict[str, float] = {}

        self.reload_index()

    def reload_index(self) -> None:
        t0 = time.time()
        self.videos = self.db.load_all_videos()
        self.by_site_id.clear()
        self.by_id_any_site.clear()
        self.by_exact_title.clear()
        self.by_studio.clear()
        self.by_token.clear()
        self.token_idf.clear()

        n_docs = max(1, len(self.videos))
        df_counts: Dict[str, int] = defaultdict(int)

        for idx, v in enumerate(self.videos):
            self.by_site_id[(v.site, v.video_id)] = idx
            self.by_id_any_site[v.video_id].append(idx)

            if v.title_norm:
                self.by_exact_title[v.title_norm].append(idx)
            if v.title_clean_norm and v.title_clean_norm != v.title_norm:
                self.by_exact_title[v.title_clean_norm].append(idx)

            # Если в title API есть " - ", индексируем обе половинки (актриса - название / название - актриса)
            if " - " in v.title:
                for sub in v.title.split(" - "):
                    nsub = normalize_title(sub)
                    if nsub and nsub != v.title_norm:
                        self.by_exact_title[nsub].append(idx)

            if v.studio_norm:
                self.by_studio[v.studio_norm].append(idx)

            # Токены из названия и из слага превью
            combined_tokens = set(tokenize_significant(v.title_norm)) | set(tokenize_significant(v.preview_slug_norm))
            for tok in combined_tokens:
                self.by_token[tok].append(idx)
                df_counts[tok] += 1

        for tok, df in df_counts.items():
            self.token_idf[tok] = math.log(1.0 + n_docs / (1.0 + df))

        logger.info(
            "Индекс в памяти построен за %.2f сек: %d видео, %d студий, %d уникальных токенов",
            time.time() - t0,
            len(self.videos),
            len(self.by_studio),
            len(self.by_token),
        )

    def _retrieve_candidates(self, pt: ParsedTorrent, max_candidates: int = 120) -> Set[int]:
        """
        Быстрый отбор кандидатов (Blocking) из 75 000+ видео без полного перебора:
        1. По внешнему ID `(6378455)`
        2. По точному совпадению нормализованного названия
        3. Все видео той же студии (`by_studio`) — обычно 50..700 роликов, позволяет находить
           ролики даже с опечатками (`Cum Love` <-> `Cam Love`, `Bangin` <-> `Banging`)
        4. По самым редким (высокий IDF) токенам из названия сцены и имён актрис
        """
        candidates: Set[int] = set()

        # 1. Прямой ID
        if pt.external_id:
            for idx in self.by_id_any_site.get(pt.external_id, []):
                candidates.add(idx)

        # 2. Точное нормализованное название
        for key in {pt.scene_title_norm, pt.scene_title_no_actors_norm}:
            if key and key in self.by_exact_title:
                candidates.update(self.by_exact_title[key])

        # 3. Блок по студии
        if pt.studio_norm and pt.studio_norm in self.by_studio:
            studio_vids = self.by_studio[pt.studio_norm]
            if len(studio_vids) <= 1000:
                candidates.update(studio_vids)

        # 4. Токеновый поиск (BM25-подобное накопление весов IDF по редким словам названия и актрис)
        query_tokens = set(tokenize_significant(pt.scene_title_norm))
        for a_norm in pt.actors_norm:
            query_tokens.update(tokenize_significant(a_norm))

        # Сортируем токены по редкости (IDF)
        sorted_tokens = sorted(query_tokens, key=lambda t: self.token_idf.get(t, 0.0), reverse=True)
        token_scores: Dict[int, float] = defaultdict(float)
        for tok in sorted_tokens[:8]:
            idf = self.token_idf.get(tok, 0.0)
            if idf <= 0:
                continue
            postings = self.by_token.get(tok, [])
            # Пропускаем сверхчастые слова (>5000 документов), если уже есть другие токены
            if len(postings) > 5000 and len(sorted_tokens) > 2:
                continue
            for idx in postings:
                token_scores[idx] += idf

        if token_scores:
            top_by_tokens = sorted(token_scores.items(), key=lambda kv: kv[1], reverse=True)[:max_candidates]
            for idx, _ in top_by_tokens:
                candidates.add(idx)

        return candidates

    def _score_candidate(self, pt: ParsedTorrent, v: ApiVideoRecord) -> Tuple[float, str, Dict[str, float]]:
        """
        Вычисляет итоговый балл уверенности (0..100) и расшифровку факторов для пары (Раздача, Видео API).
        """
        breakdown: Dict[str, float] = {}

        # --- ФАКТОР 1: Совпадение внешнего ID (например, 6378455 на POVR) ---
        if pt.external_id and pt.external_id == v.video_id:
            # Проверяем, что либо сайт совпадает, либо название/актриса близки
            title_check = _string_similarity(pt.scene_title_norm, v.title_norm)
            if title_check >= 65.0 or (pt.source_site and pt.source_site in v.site):
                breakdown["exact_id"] = 100.0
                breakdown["title_sim"] = round(title_check, 1)
                return 100.0, "exact_id", breakdown

        # --- ФАКТОР 2: Сходство названий сцен (Title Similarity: 0..100) ---
        # В API название может быть:
        #   a) Чистым: 'Clowning Around'
        #   b) С ТЕМИ ЖЕ актрисами/студией через дефис/двоеточие/in:
        #      'Ariana, Lexy Emerald - My Fairy-Tale Prince',
        #      'King MILF - Karina King', 'Maddy Black in Straight into it!'
        # ВАЖНО: мы удаляем из названия API только имена актрис/студии самой раздачи (или самого видео),
        # чтобы 'Barbie Sins: Lingerie Blowjob' НЕ превращалось в 'Lingerie Blowjob' для актрисы 'Paola Hard'!
        entities_to_strip = list(pt.actors)
        if pt.studio and pt.studio_norm not in {"povr", "vrporn", "sexlikereal"}:
            entities_to_strip.append(pt.studio)
        if v.studio:
            entities_to_strip.append(v.studio)

        api_stripped = strip_actors_from_title(v.title_norm, entities_to_strip)
        api_title_variants = {v.title_norm}
        if api_stripped and len(api_stripped) >= 2:
            api_title_variants.add(api_stripped)

        # Если название в раздаче длинное и состоит из подзаголовков через двоеточие
        # (например, 'Peeking Taboo: Spend-A-Penny: Hot Bathroom Fetish VR Porn With Penny Barber'),
        # проверяем как полное название (без имени актрисы), так и его смысловые сегменты >= 8 символов
        primary_t_title = pt.scene_title_no_actors_norm if pt.scene_title_no_actors_norm else pt.scene_title_norm
        torrent_title_variants = {primary_t_title}
        if not pt.actors_norm:
            torrent_title_variants.add(pt.scene_title_norm)
        if ":" in pt.scene_title:
            for part in pt.scene_title.split(":"):
                np = strip_actors_from_title(normalize_title(part), entities_to_strip)
                if len(np) >= 8:
                    torrent_title_variants.add(np)

        best_title_sim = 0.0
        for tv in torrent_title_variants:
            for av in api_title_variants:
                sim = _string_similarity(tv, av)
                if sim > best_title_sim:
                    best_title_sim = sim

        # Проверка несовпадения номеров частей/эпизодов (например, "Part 1" vs "Part 2", "Episode 3" vs "Episode 4")
        t_nums = re.findall(r"\b(?:part|episode)\s+(\d+)\b", pt.scene_title_norm)
        v_nums = re.findall(r"\b(?:part|episode)\s+(\d+)\b", v.title_norm)
        part_mismatch_penalty = 0.0
        if t_nums and v_nums and t_nums != v_nums:
            part_mismatch_penalty = -35.0
        elif (t_nums and not v_nums) or (v_nums and not t_nums):
            # Например, "Paola Hard Episode 2" vs "Paola Hard" (без номера эпизода)
            part_mismatch_penalty = -22.0

        breakdown["title_sim"] = round(best_title_sim, 1)
        if part_mismatch_penalty:
            breakdown["part_penalty"] = part_mismatch_penalty

        # --- ФАКТОР 3: Совпадение студии (Studio Match) ---
        studio_bonus = 0.0
        t_st = pt.studio_norm
        v_st = v.studio_norm
        if t_st and t_st not in {"povr", "vrporn", "sexlikereal"}:
            if v_st:
                if t_st == v_st or t_st in v_st or v_st in t_st:
                    studio_bonus = 18.0
                else:
                    st_sim = _string_similarity(t_st, v_st)
                    if st_sim >= 80.0:
                        studio_bonus = 14.0
                    else:
                        # Разные известные студии — штраф (иногда одна сцена переиздаётся под дочерним лейблом, но редко)
                        studio_bonus = -14.0
            elif v.preview_slug_norm and t_st in v.preview_slug_norm.replace(" ", ""):
                studio_bonus = 14.0
        elif pt.source_site and pt.source_site == v.site:
            studio_bonus = 4.0

        breakdown["studio_bonus"] = studio_bonus

        # --- ФАКТОР 4: Совпадение актрис (Actor Overlap) ---
        # Проверяем актрис раздачи по:
        # 1) списку v.actors_json (если загружен)
        # 2) тексту v.title_norm
        # 3) тексту слага обложки v.preview_slug_norm
        actor_bonus = 0.0
        if pt.actors_norm:
            api_actors_list = json.loads(v.actors_json) if v.actors_json else []
            api_actors_norm_text = " ".join(normalize_actor_name(a) for a in api_actors_list)
            searchable_actor_space = f"{api_actors_norm_text} {v.title_norm} {v.preview_slug_norm}".strip()

            matched_actors = 0
            for a_norm in pt.actors_norm:
                # Игнорируем фиктивные заполнители вроде "Maids Women"
                if a_norm in {"maids women", "unknown", "various"}:
                    continue
                if _actor_in_text(a_norm, searchable_actor_space):
                    matched_actors += 1

            if matched_actors > 0:
                # Первая совпавшая актриса даёт +14, последующие до +18 суммарно
                actor_bonus = min(18.0, 14.0 + 2.0 * (matched_actors - 1))
            elif api_actors_list:
                # Если у видео в API точно известен список актрис, и ни одна не совпала
                actor_bonus = -12.0

        breakdown["actor_bonus"] = actor_bonus

        # --- ФАКТОР 5: Близость даты / года релиза (Date Proximity) ---
        date_bonus = 0.0
        if v.release_date > 0:
            if pt.release_timestamp:
                days_diff = abs(pt.release_timestamp - v.release_date) / 86400.0
                if days_diff <= 3:
                    date_bonus = 10.0
                elif days_diff <= 14:
                    date_bonus = 7.0
                elif days_diff <= 60:
                    date_bonus = 4.0
                elif pt.release_year and pt.release_year < v.release_year - 1:
                    # Раздача датирована существенно раньше выхода ролика в API
                    date_bonus = -12.0
            elif pt.release_year and v.release_year:
                if pt.release_year == v.release_year:
                    date_bonus = 4.0
                elif abs(pt.release_year - v.release_year) == 1:
                    date_bonus = 2.0
                elif pt.release_year < v.release_year - 1:
                    # Раздача 2018 года не может быть роликом 2024 года (но раздача [2026] может быть свежим SiteRip старого ролика!)
                    date_bonus = -12.0

        breakdown["date_bonus"] = date_bonus

        # --- ИТОГОВЫЙ КОМПОЗИТНЫЙ СКОРИНГ ---
        # Базовый вклад названия (до 70 баллов при 100% совпадении) + бонусы студии, актрисы и даты
        base_title_points = (best_title_sim / 100.0) * 72.0

        # Если название короткое/типовое (например, "Sensitive", "Gamer Girl", "Lingerie Blowjob")
        # и нет подтверждения ни по студии, ни по актрисе, ни по точной дате — ограничиваем потолок
        is_generic_short_title = len(pt.scene_title_norm) <= 14 and len(pt.scene_title_norm.split()) <= 2
        if is_generic_short_title and studio_bonus <= 0 and actor_bonus <= 0 and date_bonus < 7.0:
            base_title_points = min(base_title_points, 58.0)

        raw_total = base_title_points + studio_bonus + actor_bonus + date_bonus + part_mismatch_penalty

        # Если сходство самих названий сцен слишком низкое (< 68%), бонусы за студию и актрису
        # не должны вытягивать совершенно другую сцену той же актрисы в статус 'review' / 'auto_matched'
        if best_title_sim < 68.0:
            raw_total = min(raw_total, best_title_sim - 5.0)

        # Если совпала и студия (+18), и название >= 98%, это гарантированный матч (>= 90)
        if best_title_sim >= 98.0 and studio_bonus >= 14.0 and part_mismatch_penalty == 0:
            raw_total = max(raw_total, 91.0)

        # Если совпали и студия (+18), и актриса (+14), и название >= 85% (например, "Cum Love" vs "Cam Love",
        # "Bangin The Hot Housemaid" vs "Banging The Hot Housemaid")
        if best_title_sim >= 85.0 and studio_bonus >= 14.0 and actor_bonus >= 14.0 and part_mismatch_penalty == 0:
            raw_total = max(raw_total, 92.0)

        confidence = max(0.0, min(100.0, round(raw_total, 1)))
        method = "exact_studio_title" if (best_title_sim >= 98.0 and studio_bonus >= 14.0) else "fuzzy_multi_feature"
        return confidence, method, breakdown

    def match_one(
        self,
        raw_title_or_parsed: str | ParsedTorrent,
        top_k: int = 3,
        verify_details: bool = False,
    ) -> Dict[str, Any]:
        """
        Сопоставляет одну раздачу со всеми проиндексированными сайтами PLAY'A API v2.
        При `verify_details=True` автоматически дозапрашивает `GET /video/<id>` для топ-кандидатов,
        если нужно подтвердить актрису или студию.
        """
        pt = (
             parse_pornolab_title(raw_title_or_parsed)
            if isinstance(raw_title_or_parsed, str)
            else raw_title_or_parsed
        )

        # Если в раздаче есть внешний ID (например 6378455) и его ещё нет в локальной БД,
        # при наличии клиента API можем мгновенно проверить GET /video/<id>
        if verify_details and pt.external_id and pt.external_id not in self.by_id_any_site:
            target_sites = (
                [pt.source_site]
                if pt.source_site in self.api_clients
                else list(self.api_clients.keys())
            )
            for s_name in target_sites:
                try:
                    rec = self.api_clients[s_name].fetch_video_details(self.db, pt.external_id)
                    if rec:
                        new_idx = len(self.videos)
                        self.videos.append(rec)
                        self.by_site_id[(rec.site, rec.video_id)] = new_idx
                        self.by_id_any_site[rec.video_id].append(new_idx)
                except Exception:
                    pass

        cand_indices = self._retrieve_candidates(pt)
        scored: List[Tuple[float, str, Dict[str, float], int]] = []

        for idx in cand_indices:
            v = self.videos[idx]
            conf, method, bdown = self._score_candidate(pt, v)
            scored.append((conf, method, bdown, idx))

        scored.sort(key=lambda x: x[0], reverse=True)

        # Если включена ленивая верификация деталей (/video/<id>) и топ-кандидаты ещё без списка актрис/студии
        if verify_details and scored and self.api_clients:
            top_conf, _, top_bdown, top_idx = scored[0]
            # Дозапрашиваем детали, если уверенность в пограничной зоне (55..88) или у POVR-видео не известна студия/актрисы
            if 50.0 <= top_conf < 92.0:
                rescored = False
                for i in range(min(3, len(scored))):
                    _, _, _, c_idx = scored[i]
                    v_cand = self.videos[c_idx]
                    if not v_cand.details_fetched and v_cand.site in self.api_clients:
                        try:
                            updated = self.api_clients[v_cand.site].fetch_video_details(self.db, v_cand.video_id)
                            if updated:
                                self.videos[c_idx] = updated
                                rescored = True
                        except Exception:
                            pass
                if rescored:
                    scored = []
                    for idx in cand_indices:
                        v = self.videos[idx]
                        conf, method, bdown = self._score_candidate(pt, v)
                        scored.append((conf, method, bdown, idx))
                    scored.sort(key=lambda x: x[0], reverse=True)

        candidates_out: List[MatchCandidate] = []
        best_by_site: Dict[str, Dict[str, Any]] = {}

        for conf, method, bdown, idx in scored:
            v = self.videos[idx]
            if conf >= self.auto_match_threshold:
                status = "auto_matched"
            elif conf >= self.review_threshold:
                status = "review"
            else:
                status = "unmatched"

            rel_iso = (
                dt.datetime.fromtimestamp(v.release_date, tz=dt.timezone.utc).strftime("%Y-%m-%d")
                if v.release_date > 0
                else ""
            )
            mc = MatchCandidate(
                site=v.site,
                video_id=v.video_id,
                api_title=v.title,
                api_studio=v.studio,
                api_release_date=rel_iso,
                api_actors=json.loads(v.actors_json) if v.actors_json else [],
                confidence=conf,
                status=status,
                match_method=method,
                score_breakdown=bdown,
            )
            if len(candidates_out) < top_k:
                candidates_out.append(mc)
            if v.site not in best_by_site and conf >= self.review_threshold:
                best_by_site[v.site] = asdict(mc)

        best_match = asdict(candidates_out[0]) if candidates_out else None
        overall_status = candidates_out[0].status if candidates_out else "unmatched"

        return {
            "torrent_id": pt.torrent_id,
            "raw_title": pt.raw_line,
            "parsed": {
                "studio": pt.studio,
                "source_site": pt.source_site,
                "actors": pt.actors,
                "scene_title": pt.scene_title,
                "external_id": pt.external_id,
                "release_date": pt.release_date or pt.release_year,
                "resolution": pt.resolution,
            },
            "status": overall_status,
            "best_match": best_match,
            "best_by_site": best_by_site,
            "candidates": [asdict(c) for c in candidates_out],
        }

    def match_batch(
        self,
        torrents: Iterable[str | Dict[str, Any]],
        top_k: int = 3,
        verify_details: bool = False,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for idx, item in enumerate(torrents, 1):
            if isinstance(item, dict):
                raw_line = item.get("title") or item.get("raw_title") or item.get("name") or ""
                tid = str(item.get("id") or item.get("topic_id") or idx)
            else:
                raw_line = str(item)
                tid = str(idx)
            pt = parse_pornolab_title(raw_line, torrent_id=tid)
            res = self.match_one(pt, top_k=top_k, verify_details=verify_details)
            results.append(res)
        return results


# =============================================================================
# 5. CLI-ИНТЕРФЕЙС (SYNC / MATCH / DEMO)
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Синхронизация каталогов PLAY'A API v2 (vrporn.com, povr.com) и сопоставление с раздачами Pornolab."
    )
    parser.add_argument("--db", default="playa_catalog.db", help="Путь к SQLite-базе индекса (по умолчанию: playa_catalog.db)")

    sub = parser.add_subparsers(dest="cmd", required=True)

    # Команда sync
    p_sync = sub.add_parser("sync", help="Скачать/обновить каталог видео из PLAY'A API v2 в локальную БД SQLite")
    p_sync.add_argument(
        "--sites",
        nargs="+",
        default=["vrporn.com", "povr.com"],
        help="Список доменов с PLAY'A API v2 (по умолчанию: vrporn.com povr.com)",
    )
    p_sync.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Ограничить число страниц (например, --max-pages 2 для быстрого инкрементального обновления)",
    )
    p_sync.add_argument("--delay", type=float, default=1.5, help="Пауза между запросами в секундах (по умолчанию 1.5)")

    # Команда match
    p_match = sub.add_parser("match", help="Сопоставить список раздач (JSON/JSONL/CSV/TXT) с локальной базой API")
    p_match.add_argument("--input", "-i", required=True, help="Входной файл с раздачами (.json, .jsonl, .csv, .txt)")
    p_match.add_argument("--output", "-o", required=True, help="Выходной JSON-файл с результатами сопоставления")
    p_match.add_argument("--verify-details", action="store_true", help="Дозапрашивать /video/<id> для спорных кандидатов")
    p_match.add_argument("--top-k", type=int, default=3, help="Сколько лучших кандидатов сохранять (по умолчанию 3)")

    args = parser.parse_args()
    db = PlayaCatalogDB(args.db)

    if args.cmd == "sync":
        for site in args.sites:
            client = PlayaApiClient(site, request_delay=args.delay)
            client.sync_catalog(db, max_pages=args.max_pages)
        logger.info("Синхронизация завершена. Всего видео в базе: %d", db.count_videos())

    elif args.cmd == "match":
        in_path = args.input
        items: List[Any] = []
        if in_path.endswith(".json"):
            with open(in_path, "r", encoding="utf-8") as f:
                items = json.load(f)
        elif in_path.endswith(".jsonl"):
            with open(in_path, "r", encoding="utf-8") as f:
                items = [json.loads(line) for line in f if line.strip()]
        elif in_path.endswith(".csv"):
            with open(in_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                items = list(reader)
        else:
            with open(in_path, "r", encoding="utf-8") as f:
                items = [line.strip() for line in f if line.strip()]

        clients = {
            "vrporn.com": PlayaApiClient("vrporn.com", request_delay=1.5),
            "povr.com": PlayaApiClient("povr.com", request_delay=0.5),
        }
        matcher = PlayaMatcher(db, api_clients=clients)
        t0 = time.time()
        results = matcher.match_batch(items, top_k=args.top_k, verify_details=args.verify_details)
        dt_sec = time.time() - t0

        counts = defaultdict(int)
        for r in results:
            counts[r["status"]] += 1

        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        logger.info(
            "Сопоставлено %d раздач за %.2f сек: auto_matched=%d, review=%d, unmatched=%d -> %s",
            len(results),
            dt_sec,
            counts["auto_matched"],
            counts["review"],
            counts["unmatched"],
            args.output,
        )


if __name__ == "__main__":
    main()
