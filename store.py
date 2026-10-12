
# -*- coding: utf-8 -*-
"""Простое sqlite-хранилище карточек, собранных с форума."""

import sqlite3
import json
import time
import threading
from typing import Optional

import config

_lock = threading.RLock()
_db_initialized = False
_db_initialized_path = None


def _conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn, table: str, column: str, coltype: str):
    """Добавляет колонку, если её ещё нет (мягкая миграция старой базы)."""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db():
    global _db_initialized, _db_initialized_path
    with _lock, _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id TEXT PRIMARY KEY,
                topic_id TEXT,
                title TEXT,
                tags TEXT,            -- JSON list
                thumbnail_url TEXT,
                source_type TEXT,     -- 'magnet' | 'torrent_url'
                source TEXT,          -- magnet-ссылка или URL на .torrent
                screen_type TEXT,
                stereo_mode TEXT,
                video_length INTEGER DEFAULT 0,
                video_height INTEGER,
                created_at INTEGER,
                updated_at INTEGER,
                -- кэш уже построенного стрим-URL из TorrServe, чтобы не пересчитывать каждый раз
                stream_url TEXT,
                stream_built_at INTEGER
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_updated ON items(updated_at)")

        # мягкая миграция для баз, созданных до добавления актрис/студии (HereSphere)
        _ensure_column(conn, "items", "performers", "TEXT DEFAULT '[]'")
        _ensure_column(conn, "items", "studio", "TEXT DEFAULT '[]'")

        # мягкая миграция: "ранг" карточки внутри СВОЕГО источника - отражает
        # реальный порядок раздела на трекере (номер страницы + позиция в
        # списке на момент обнаружения), а НЕ время, когда мы сами её нашли.
        # Раньше сортировали по updated_at (момент записи в нашу базу) - из-за
        # этого старые раздачи, которые постепенный архивный обход находит
        # только сейчас, выглядели "самыми новыми". Чем МЕНЬШЕ source_rank,
        # тем ближе к началу раздела (новее) карточка. 999999999 - значение
        # по умолчанию для карточек, у которых ранга ещё нет (старые записи
        # до этой миграции) - они уходят в конец списка, а не притворяются
        # новыми.
        _ensure_column(conn, "items", "source_rank", "INTEGER DEFAULT 999999999")

        # Вертикальное разрешение из поля "Тип видео" (например, 3072p). Оно нужно
        # не только для фильтра MIN_VIDEO_HEIGHT_PX в scraper.py, но и для DeoVR:
        # /video/<id> должен отдавать реальный resolution, а не старый хардкод 1080.
        _ensure_column(conn, "items", "video_height", "INTEGER")

        # Когда карточка впервые появилась в локальной библиотеке. updated_at может
        # меняться при повторном upsert/обновлении метаданных, а для UI нужна именно
        # дата добавления. Для старых БД backfill'им её прежним updated_at.
        _ensure_column(conn, "items", "created_at", "INTEGER")
        conn.execute("UPDATE items SET created_at = COALESCE(updated_at, strftime('%s','now')) WHERE created_at IS NULL")

        # сиды/личи со страницы списка раздела (NULL = неизвестно - у xxxclub их
        # в принципе нет, это DHT-магнет без страницы трекера) и скриншоты из
        # спойлера темы (JSON-список URL, часто в стерео-картинке)
        _ensure_column(conn, "items", "seeders", "INTEGER")
        _ensure_column(conn, "items", "leechers", "INTEGER")
        _ensure_column(conn, "items", "screenshots", "TEXT DEFAULT '[]'")

        # мягкая миграция для баз, созданных до поддержки нескольких источников
        # (карточка хранит, с какого сайта она взята - нужно, чтобы знать, какой
        # сессией/куками резолвить её ссылку на скачивание, и чтобы показывать
        # клон отдельной вкладкой, не смешивая с основным разделом)
        _ensure_column(conn, "items", "site", "TEXT DEFAULT 'pornolab'")

        # курсор инкрементального обхода большого раздела (782 страницы)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS crawl_state (
                section_url TEXT PRIMARY KEY,
                backfill_cursor_page INTEGER DEFAULT 0,
                backfill_complete INTEGER DEFAULT 0
            )
            """
        )
        # счётчик "сколько НОВЫХ страниц архива мы когда-либо просмотрели" для
        # источников с непрозрачным курсором пагинации (xxxclub) - в отличие
        # от pornolab, где номер страницы сам по себе уже порядковый, здесь
        # нужен свой монотонный счётчик, чтобы строить source_rank карточек
        # (см. комментарий у items.source_rank).
        _ensure_column(conn, "crawl_state", "pages_fetched_seq", "INTEGER DEFAULT 0")

        # маленькое key-value хранилище для разного служебного состояния (например,
        # "до какого момента не дёргать dl.php - форум сказал, что суточный лимит
        # скачиваний исчерпан"). Переживает перезапуск сервера - важно, чтобы после
        # рестарта мы не начали снова ломиться в форум и жечь остаток лимита.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )

        # Кэш внешних метаданных для PLAY'A: постеры актёров/логотипы студий
        # и служебные ссылки на источник. Храним только URL/текст, сами картинки
        # не скачиваем в репозиторий/БД.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entity_meta (
                kind TEXT NOT NULL,          -- actor | studio
                entity_id TEXT NOT NULL,
                title TEXT,
                preview_url TEXT,
                banner_url TEXT,
                source_url TEXT,
                trailer_url TEXT,
                description TEXT,
                updated_at INTEGER,
                PRIMARY KEY (kind, entity_id)
            )
            """
        )

        # Локальные списки актёров/студий. Это не дублирует нативное избранное
        # PLAY'A для видео: клиент не умеет добавлять actor/studio в избранное,
        # поэтому держим только entity-level списки, управляемые нашим web UI/API.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entity_lists (
                list_name TEXT NOT NULL,
                kind TEXT NOT NULL,          -- actor | studio
                entity_id TEXT NOT NULL,
                title TEXT,
                created_at INTEGER,
                PRIMARY KEY (list_name, kind, entity_id)
            )
            """
        )

        # Канонические псевдонимы для объединения сущностей из разных каталогов
        # и трекеров: "VR Bangers", "VRBangers" и "vrbangers.com" должны вести
        # к одной studio-карточке. alias_key уже нормализован вызывающим кодом.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS entity_aliases (
                kind TEXT NOT NULL,          -- actor | studio
                alias_key TEXT NOT NULL,
                alias TEXT,
                canonical_title TEXT NOT NULL,
                source TEXT DEFAULT 'manual',
                external_id TEXT DEFAULT '',
                updated_at INTEGER,
                PRIMARY KEY (kind, alias_key)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entity_aliases_canonical "
            "ON entity_aliases(kind, canonical_title)"
        )

        # Кэш внешних PLAY'A trailer-карточек. Подписанные ссылки на mp4 сюда
        # намеренно не пишем: они короткоживущие и обновляются при открытии video.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS external_scenes (
                provider TEXT NOT NULL,
                external_id TEXT NOT NULL,
                title TEXT,
                subtitle TEXT,
                preview_url TEXT,
                release_date INTEGER,
                trailer_duration INTEGER,
                actor_keys TEXT DEFAULT '[]',
                actor_titles TEXT DEFAULT '[]',
                studio_id TEXT DEFAULT '',
                studio_title TEXT DEFAULT '',
                updated_at INTEGER,
                PRIMARY KEY (provider, external_id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_external_scenes_updated ON external_scenes(updated_at)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS external_scene_matches (
                provider TEXT NOT NULL,
                external_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                score INTEGER,
                updated_at INTEGER,
                PRIMARY KEY (provider, external_id, item_id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_external_matches_item ON external_scene_matches(item_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS external_entity_refs (
                provider TEXT NOT NULL,
                kind TEXT NOT NULL,
                alias_key TEXT NOT NULL,
                external_id TEXT NOT NULL,
                title TEXT,
                updated_at INTEGER,
                PRIMARY KEY (provider, kind, alias_key)
            )
            """
        )

        # Кэш сопоставлений VRPorn/POVR ←→ pornolab/ptorrents
        # Используется для вкладки "For You" и отображения статусов на карточках
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS match_cache (
                item_id TEXT NOT NULL,
                provider TEXT NOT NULL,        -- vrporn или povr
                external_id TEXT NOT NULL,
                match_score INTEGER,
                match_type TEXT NOT NULL,      -- full_with_trailer | trailer_only | full_only
                auto_matched INTEGER DEFAULT 0,
                matched_at INTEGER,
                PRIMARY KEY (item_id, provider)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_match_cache_external ON match_cache(provider, external_id)")

        _db_initialized = True
        _db_initialized_path = config.DB_PATH


def ensure_db():
    """Гарантирует, что схема БД создана и все мягкие миграции применены.

    app.py вызывает init_db() при старте, но вспомогательные скрипты и тесты могут
    обращаться к store напрямую. Поэтому публичные функции дополнительно вызывают
    ensure_db(), чтобы старая cache.sqlite3 пользователя не падала на новых
    колонках вроде source_rank/site/video_height.
    """
    if not _db_initialized or _db_initialized_path != config.DB_PATH:
        init_db()


def get_meta(key: str, default=None):
    ensure_db()
    with _lock, _conn() as conn:
        r = conn.execute("SELECT value FROM kv_meta WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else default


def set_meta(key: str, value: str):
    ensure_db()
    with _lock, _conn() as conn:
        conn.execute(
            """
            INSERT INTO kv_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )


def upsert_item(item: dict):
    ensure_db()
    with _lock, _conn() as conn:
        conn.execute(
            """
            INSERT INTO items (id, topic_id, title, tags, thumbnail_url, source_type, source,
                                screen_type, stereo_mode, video_length, video_height, created_at, updated_at,
                                performers, studio, site, source_rank, seeders, leechers, screenshots)
            VALUES (:id, :topic_id, :title, :tags, :thumbnail_url, :source_type, :source,
                    :screen_type, :stereo_mode, :video_length, :video_height, :created_at, :updated_at,
                    :performers, :studio, :site, :source_rank, :seeders, :leechers, :screenshots)
            ON CONFLICT(id) DO UPDATE SET
                topic_id=excluded.topic_id,
                title=excluded.title,
                tags=excluded.tags,
                thumbnail_url=excluded.thumbnail_url,
                source_type=excluded.source_type,
                source=excluded.source,
                screen_type=excluded.screen_type,
                stereo_mode=excluded.stereo_mode,
                video_length=excluded.video_length,
                video_height=excluded.video_height,
                updated_at=excluded.updated_at,
                performers=excluded.performers,
                studio=excluded.studio,
                site=excluded.site,
                source_rank=excluded.source_rank,
                seeders=excluded.seeders,
                leechers=excluded.leechers,
                screenshots=excluded.screenshots
            """,
            {
                "id": item["id"],
                "topic_id": item.get("topic_id", item["id"]),
                "title": item["title"],
                "tags": json.dumps(item.get("tags", []), ensure_ascii=False),
                "thumbnail_url": item.get("thumbnail_url", ""),
                "source_type": item.get("source_type", "magnet"),
                "source": item.get("source", ""),
                "screen_type": item.get("screen_type", config.DEFAULT_SCREEN_TYPE),
                "stereo_mode": item.get("stereo_mode", config.DEFAULT_STEREO_MODE),
                "video_length": item.get("video_length", 0),
                "video_height": _optional_int(item.get("video_height")),
                "created_at": _optional_int(item.get("created_at")) or int(time.time()),
                "updated_at": int(time.time()),
                "performers": json.dumps(item.get("performers", []), ensure_ascii=False),
                "studio": json.dumps(item.get("studio", []), ensure_ascii=False),
                "site": item.get("site", "pornolab"),
                "source_rank": item.get("source_rank", 999999999),
                "seeders": item.get("seeders"),
                "leechers": item.get("leechers"),
                "screenshots": json.dumps(item.get("screenshots", []), ensure_ascii=False),
            },
        )


def _optional_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_list(value) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _row_to_dict(r):
    d = dict(r)
    d["tags"] = _json_list(d.get("tags"))
    d["performers"] = _json_list(d.get("performers"))
    d["studio"] = _json_list(d.get("studio"))
    d["screenshots"] = _json_list(d.get("screenshots"))
    d["screen_type"] = d.get("screen_type") or config.DEFAULT_SCREEN_TYPE
    d["stereo_mode"] = d.get("stereo_mode") or config.DEFAULT_STEREO_MODE
    d["video_length"] = int(d.get("video_length") or 0)
    d["video_height"] = _optional_int(d.get("video_height"))
    d["created_at"] = int(d.get("created_at") or d.get("updated_at") or 0)
    d["source_rank"] = d.get("source_rank") if d.get("source_rank") is not None else 999999999
    d["site"] = d.get("site") or "pornolab"
    return d


def all_items():
    ensure_db()
    with _lock, _conn() as conn:
        # ВАЖНО: порядок = как на самом трекере (source_rank, меньше - новее/выше
        # в разделе), а НЕ updated_at (момент, когда МЫ записали карточку к себе -
        # раньше сортировали по нему, из-за чего старые раздачи, найденные только
        # сейчас постепенным архивным обходом, выглядели "самыми новыми"). Среди
        # карточек без ранга (старые записи до миграции, source_rank=sentinel)
        # используем updated_at как запасной критерий, просто чтобы не было
        # полностью случайного порядка внутри этой группы.
        rows = conn.execute(
            "SELECT * FROM items ORDER BY COALESCE(source_rank, 999999999) ASC, updated_at DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def set_source_rank(item_id: str, rank: int):
    """Точечно обновляет ранг уже существующей карточки - используется разовым
    скриптом переиндексации (rerank_existing.py), который заново проходит
    список раздела ТОЛЬКО ради актуальных позиций, не трогая остальные поля."""
    ensure_db()
    with _lock, _conn() as conn:
        conn.execute("UPDATE items SET source_rank = ? WHERE id = ?", (rank, item_id))


def get_item(item_id: str):
    ensure_db()
    with _lock, _conn() as conn:
        r = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not r:
            return None
        return _row_to_dict(r)


def item_exists(item_id: str) -> bool:
    """Быстрая проверка без разбора JSON-полей - используется краулером, чтобы не
    перескачивать и не парсить страницу темы, которая уже есть в кэше."""
    ensure_db()
    with _lock, _conn() as conn:
        r = conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone()
        return r is not None


def save_stream_url(item_id: str, stream_url: str):
    ensure_db()
    with _lock, _conn() as conn:
        conn.execute(
            "UPDATE items SET stream_url = ?, stream_built_at = ? WHERE id = ?",
            (stream_url, int(time.time()), item_id),
        )


def all_tags():
    tags = set()
    for item in all_items():
        for t in item["tags"]:
            tags.add(t)
    return sorted(tags)


def get_entity_meta(kind: str, entity_id: str):
    ensure_db()
    with _lock, _conn() as conn:
        r = conn.execute(
            "SELECT * FROM entity_meta WHERE kind = ? AND entity_id = ?",
            (kind, entity_id),
        ).fetchone()
        return dict(r) if r else None


def upsert_entity_meta(kind: str, entity_id: str, title: str, preview_url: str = "",
                       banner_url: str = "", source_url: str = "", trailer_url: str = "",
                       description: str = ""):
    ensure_db()
    with _lock, _conn() as conn:
        conn.execute(
            """
            INSERT INTO entity_meta (kind, entity_id, title, preview_url, banner_url,
                                     source_url, trailer_url, description, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, entity_id) DO UPDATE SET
                title=excluded.title,
                preview_url=excluded.preview_url,
                banner_url=excluded.banner_url,
                source_url=excluded.source_url,
                trailer_url=excluded.trailer_url,
                description=excluded.description,
                updated_at=excluded.updated_at
            """,
            (kind, entity_id, title, preview_url or "", banner_url or "", source_url or "",
             trailer_url or "", description or "", int(time.time())),
        )


def set_entity_in_list(list_name: str, kind: str, entity_id: str, title: str = "", enabled: bool = True):
    ensure_db()
    with _lock, _conn() as conn:
        if enabled:
            conn.execute(
                """
                INSERT INTO entity_lists (list_name, kind, entity_id, title, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(list_name, kind, entity_id) DO UPDATE SET
                    title=excluded.title
                """,
                (list_name, kind, entity_id, title or "", int(time.time())),
            )
        else:
            conn.execute(
                "DELETE FROM entity_lists WHERE list_name = ? AND kind = ? AND entity_id = ?",
                (list_name, kind, entity_id),
            )


def toggle_entity_in_list(list_name: str, kind: str, entity_id: str, title: str = "") -> bool:
    enabled = not is_entity_in_list(list_name, kind, entity_id)
    set_entity_in_list(list_name, kind, entity_id, title, enabled)
    return enabled


def is_entity_in_list(list_name: str, kind: str, entity_id: str) -> bool:
    ensure_db()
    with _lock, _conn() as conn:
        r = conn.execute(
            "SELECT 1 FROM entity_lists WHERE list_name = ? AND kind = ? AND entity_id = ?",
            (list_name, kind, entity_id),
        ).fetchone()
        return r is not None


def entity_list_ids(list_name: str, kind: str) -> set:
    ensure_db()
    with _lock, _conn() as conn:
        rows = conn.execute(
            "SELECT entity_id FROM entity_lists WHERE list_name = ? AND kind = ?",
            (list_name, kind),
        ).fetchall()
        return {r["entity_id"] for r in rows}


def all_entity_list_entries(list_name: str = None, kind: str = None) -> list:
    ensure_db()
    where = []
    args = []
    if list_name is not None:
        where.append("list_name = ?")
        args.append(list_name)
    if kind is not None:
        where.append("kind = ?")
        args.append(kind)
    sql = "SELECT list_name, kind, entity_id, title, created_at FROM entity_lists"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC, title COLLATE NOCASE ASC"
    with _lock, _conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]



def upsert_entity_alias(kind: str, alias_key: str, alias: str, canonical_title: str,
                        source: str = "manual", external_id: str = ""):
    ensure_db()
    if kind not in {"actor", "studio"} or not alias_key or not canonical_title:
        return
    with _lock, _conn() as conn:
        conn.execute(
            """
            INSERT INTO entity_aliases
                (kind, alias_key, alias, canonical_title, source, external_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kind, alias_key) DO UPDATE SET
                alias=excluded.alias,
                canonical_title=excluded.canonical_title,
                source=excluded.source,
                external_id=excluded.external_id,
                updated_at=excluded.updated_at
            """,
            (kind, alias_key, alias or "", canonical_title, source or "manual",
             external_id or "", int(time.time())),
        )


def resolve_entity_alias(kind: str, alias_key: str):
    ensure_db()
    if not alias_key:
        return None
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT canonical_title FROM entity_aliases WHERE kind = ? AND alias_key = ?",
            (kind, alias_key),
        ).fetchone()
        return row["canonical_title"] if row else None


def all_entity_aliases(kind: str = None) -> list:
    ensure_db()
    sql = "SELECT kind, alias_key, alias, canonical_title, source, external_id, updated_at FROM entity_aliases"
    args = []
    if kind is not None:
        sql += " WHERE kind = ?"
        args.append(kind)
    sql += " ORDER BY kind, canonical_title COLLATE NOCASE, alias COLLATE NOCASE"
    with _lock, _conn() as conn:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]


def upsert_external_entity_ref(provider: str, kind: str, alias_key: str,
                               external_id: str, title: str = ""):
    ensure_db()
    with _lock, _conn() as conn:
        conn.execute(
            """
            INSERT INTO external_entity_refs
                (provider, kind, alias_key, external_id, title, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, kind, alias_key) DO UPDATE SET
                external_id=excluded.external_id,
                title=excluded.title,
                updated_at=excluded.updated_at
            """,
            (provider, kind, alias_key, str(external_id), title or "", int(time.time())),
        )


def get_external_entity_ref(provider: str, kind: str, alias_key: str):
    ensure_db()
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT * FROM external_entity_refs WHERE provider=? AND kind=? AND alias_key=?",
            (provider, kind, alias_key),
        ).fetchone()
        return dict(row) if row else None


def upsert_external_scene(scene: dict):
    ensure_db()
    with _lock, _conn() as conn:
        conn.execute(
            """
            INSERT INTO external_scenes
                (provider, external_id, title, subtitle, preview_url, release_date,
                 trailer_duration, actor_keys, actor_titles, studio_id, studio_title, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, external_id) DO UPDATE SET
                title=excluded.title,
                subtitle=excluded.subtitle,
                preview_url=excluded.preview_url,
                release_date=excluded.release_date,
                trailer_duration=excluded.trailer_duration,
                actor_keys=excluded.actor_keys,
                actor_titles=excluded.actor_titles,
                studio_id=excluded.studio_id,
                studio_title=excluded.studio_title,
                updated_at=excluded.updated_at
            """,
            (
                scene["provider"], str(scene["external_id"]), scene.get("title", ""),
                scene.get("subtitle", ""), scene.get("preview_url", ""),
                _optional_int(scene.get("release_date")), _optional_int(scene.get("trailer_duration")),
                json.dumps(scene.get("actor_keys", []), ensure_ascii=False),
                json.dumps(scene.get("actor_titles", []), ensure_ascii=False),
                str(scene.get("studio_id") or ""), scene.get("studio_title", ""), int(time.time()),
            ),
        )


def external_scenes_for_actor(actor_key: str, provider: str = None) -> list:
    ensure_db()
    with _lock, _conn() as conn:
        if provider:
            rows = conn.execute(
                "SELECT * FROM external_scenes WHERE provider=? ORDER BY release_date DESC",
                (provider,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM external_scenes ORDER BY release_date DESC").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["actor_keys"] = _json_list(item.get("actor_keys"))
            item["actor_titles"] = _json_list(item.get("actor_titles"))
            if actor_key in item["actor_keys"]:
                result.append(item)
        return result


def get_external_scene(provider: str, external_id: str):
    ensure_db()
    with _lock, _conn() as conn:
        row = conn.execute(
            "SELECT * FROM external_scenes WHERE provider=? AND external_id=?",
            (provider, str(external_id)),
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["actor_keys"] = _json_list(item.get("actor_keys"))
        item["actor_titles"] = _json_list(item.get("actor_titles"))
        return item


def save_external_scene_match(provider: str, external_id: str, item_id: str, score: int):
    ensure_db()
    with _lock, _conn() as conn:
        conn.execute(
            """
            INSERT INTO external_scene_matches (provider, external_id, item_id, score, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(provider, external_id, item_id) DO UPDATE SET
                score=excluded.score, updated_at=excluded.updated_at
            """,
            (provider, str(external_id), item_id, int(score), int(time.time())),
        )


def external_scene_matches(provider: str, external_id: str) -> list:
    ensure_db()
    with _lock, _conn() as conn:
        rows = conn.execute(
            """
            SELECT m.*, i.title AS item_title, i.video_height, i.seeders, i.site
            FROM external_scene_matches m
            JOIN items i ON i.id = m.item_id
            WHERE m.provider=? AND m.external_id=?
            ORDER BY m.score DESC, COALESCE(i.seeders, 0) DESC, COALESCE(i.video_height, 0) DESC
            """,
            (provider, str(external_id)),
        ).fetchall()
        return [dict(row) for row in rows]


def external_matches_for_item(item_id: str) -> list:
    ensure_db()
    with _lock, _conn() as conn:
        # Сначала ищем в match_cache (новые сопоставления)
        mc_rows = conn.execute(
            """
            SELECT mc.provider, mc.external_id, mc.match_score as score, mc.match_type, mc.auto_matched,
                   es.title, es.subtitle, es.preview_url, es.trailer_duration
            FROM match_cache mc
            LEFT JOIN external_scenes es ON es.provider=mc.provider AND es.external_id=mc.external_id
            WHERE mc.item_id=?
            ORDER BY mc.match_score DESC
            """,
            (item_id,),
        ).fetchall()
        
        if mc_rows:
            return [dict(row) for row in mc_rows]
        
        # Fallback на старую таблицу external_scene_matches
        rows = conn.execute(
            """
            SELECT m.*, s.title, s.subtitle, s.preview_url, s.trailer_duration
            FROM external_scene_matches m
            JOIN external_scenes s ON s.provider=m.provider AND s.external_id=m.external_id
            WHERE m.item_id=? ORDER BY m.score DESC, s.release_date DESC
            """,
            (item_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def get_crawl_state(section_url: str) -> dict:
    ensure_db()
    with _lock, _conn() as conn:
        r = conn.execute(
            "SELECT * FROM crawl_state WHERE section_url = ?", (section_url,)
        ).fetchone()
        if not r:
            return {"backfill_cursor_page": 0, "backfill_complete": 0, "pages_fetched_seq": 0}
        d = dict(r)
        d.setdefault("pages_fetched_seq", 0)
        return d


def save_crawl_state(section_url: str, cursor_page: int, complete: bool = False, pages_fetched_seq: int = None):
    """
    pages_fetched_seq: монотонный счётчик "сколько НОВЫХ страниц архива вообще
    когда-либо просмотрели" - нужен источникам с непрозрачным курсором (xxxclub),
    чтобы строить source_rank карточек (см. комментарий у items.source_rank в
    init_db()). Если None - оставляем прежнее значение как есть (не трогаем).
    """
    ensure_db()
    with _lock, _conn() as conn:
        if pages_fetched_seq is None:
            r = conn.execute(
                "SELECT pages_fetched_seq FROM crawl_state WHERE section_url = ?", (section_url,)
            ).fetchone()
            pages_fetched_seq = (r["pages_fetched_seq"] if r and r["pages_fetched_seq"] is not None else 0)
        conn.execute(
            """
            INSERT INTO crawl_state (section_url, backfill_cursor_page, backfill_complete, pages_fetched_seq)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(section_url) DO UPDATE SET
                backfill_cursor_page=excluded.backfill_cursor_page,
                backfill_complete=excluded.backfill_complete,
                pages_fetched_seq=excluded.pages_fetched_seq
            """,
            (section_url, cursor_page, 1 if complete else 0, pages_fetched_seq),
        )


# === Кэш сопоставлений (match_cache) ===

def save_match_cache(item_id: str, provider: str, external_id: str, match_score: int, match_type: str, auto_matched: bool = False):
    """Сохраняет результат сопоставления."""
    ensure_db()
    with _lock, _conn() as conn:
        conn.execute(
            """
            INSERT INTO match_cache (item_id, provider, external_id, match_score, match_type, auto_matched, matched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id, provider) DO UPDATE SET
                external_id=excluded.external_id,
                match_score=excluded.match_score,
                match_type=excluded.match_type,
                auto_matched=excluded.auto_matched,
                matched_at=excluded.matched_at
            """,
            (item_id, provider, external_id, match_score, match_type, 1 if auto_matched else 0, int(time.time())),
        )


def get_match_cache(item_id: str, provider: str = None) -> Optional[dict]:
    """Получает сохранённое сопоставление для карточки."""
    ensure_db()
    with _lock, _conn() as conn:
        if provider:
            r = conn.execute(
                "SELECT * FROM match_cache WHERE item_id = ? AND provider = ?", (item_id, provider)
            ).fetchone()
        else:
            r = conn.execute(
                "SELECT * FROM match_cache WHERE item_id = ? ORDER BY matched_at DESC LIMIT 1", (item_id,)
            ).fetchone()
        return dict(r) if r else None


def get_matched_items(provider: str = None) -> list:
    """Получает все карточки с сопоставлениями (для вкладки For You)."""
    ensure_db()
    with _lock, _conn() as conn:
        if provider:
            rows = conn.execute(
                """
                SELECT mc.*, i.title as item_title, i.site as item_site, i.tags, i.performers, i.studio, i.video_height
                FROM match_cache mc
                JOIN items i ON mc.item_id = i.id
                WHERE mc.provider = ?
                ORDER BY mc.matched_at DESC
                """,
                (provider,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT mc.*, i.title as item_title, i.site as item_site, i.tags, i.performers, i.studio, i.video_height
                FROM match_cache mc
                JOIN items i ON mc.item_id = i.id
                ORDER BY mc.matched_at DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]


def has_match_cache(item_id: str) -> bool:
    """Проверяет наличие сопоставления для карточки."""
    ensure_db()
    with _lock, _conn() as conn:
        r = conn.execute("SELECT COUNT(*) as cnt FROM match_cache WHERE item_id = ?", (item_id,)).fetchone()
        return r["cnt"] > 0 if r else False


def get_match_cache_by_external(external_id: str, provider: str) -> Optional[dict]:
    """Получает сопоставление по external_id."""
    ensure_db()
    with _lock, _conn() as conn:
        r = conn.execute(
            "SELECT * FROM match_cache WHERE external_id = ? AND provider = ?",
            (external_id, provider)
        ).fetchone()
        return dict(r) if r else None


def clear_match_cache(item_id: str = None, provider: str = None):
    """Очищает кэш сопоставлений."""
    ensure_db()
    with _lock, _conn() as conn:
        if item_id:
            if provider:
                conn.execute("DELETE FROM match_cache WHERE item_id = ? AND provider = ?", (item_id, provider))
            else:
                conn.execute("DELETE FROM match_cache WHERE item_id = ?", (item_id,))
        else:
            conn.execute("DELETE FROM match_cache")
