
# -*- coding: utf-8 -*-
"""Простое sqlite-хранилище карточек, собранных с форума."""

import sqlite3
import json
import time
import threading

import config

_lock = threading.Lock()


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


def get_meta(key: str, default=None):
    with _lock, _conn() as conn:
        r = conn.execute("SELECT value FROM kv_meta WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else default


def set_meta(key: str, value: str):
    with _lock, _conn() as conn:
        conn.execute(
            """
            INSERT INTO kv_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )


def upsert_item(item: dict):
    with _lock, _conn() as conn:
        conn.execute(
            """
            INSERT INTO items (id, topic_id, title, tags, thumbnail_url, source_type, source,
                                screen_type, stereo_mode, video_length, updated_at,
                                performers, studio, site, source_rank, seeders, leechers, screenshots)
            VALUES (:id, :topic_id, :title, :tags, :thumbnail_url, :source_type, :source,
                    :screen_type, :stereo_mode, :video_length, :updated_at,
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


def _row_to_dict(r):
    d = dict(r)
    d["tags"] = json.loads(d["tags"] or "[]")
    d["performers"] = json.loads(d.get("performers") or "[]")
    d["studio"] = json.loads(d.get("studio") or "[]")
    d["screenshots"] = json.loads(d.get("screenshots") or "[]")
    return d


def all_items():
    with _lock, _conn() as conn:
        # ВАЖНО: порядок = как на самом трекере (source_rank, меньше - новее/выше
        # в разделе), а НЕ updated_at (момент, когда МЫ записали карточку к себе -
        # раньше сортировали по нему, из-за чего старые раздачи, найденные только
        # сейчас постепенным архивным обходом, выглядели "самыми новыми"). Среди
        # карточек без ранга (старые записи до миграции, source_rank=sentinel)
        # используем updated_at как запасной критерий, просто чтобы не было
        # полностью случайного порядка внутри этой группы.
        rows = conn.execute(
            "SELECT * FROM items ORDER BY source_rank ASC, updated_at DESC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def set_source_rank(item_id: str, rank: int):
    """Точечно обновляет ранг уже существующей карточки - используется разовым
    скриптом переиндексации (rerank_existing.py), который заново проходит
    список раздела ТОЛЬКО ради актуальных позиций, не трогая остальные поля."""
    with _lock, _conn() as conn:
        conn.execute("UPDATE items SET source_rank = ? WHERE id = ?", (rank, item_id))


def get_item(item_id: str):
    with _lock, _conn() as conn:
        r = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        if not r:
            return None
        return _row_to_dict(r)


def item_exists(item_id: str) -> bool:
    """Быстрая проверка без разбора JSON-полей - используется краулером, чтобы не
    перескачивать и не парсить страницу темы, которая уже есть в кэше."""
    with _lock, _conn() as conn:
        r = conn.execute("SELECT 1 FROM items WHERE id = ?", (item_id,)).fetchone()
        return r is not None


def save_stream_url(item_id: str, stream_url: str):
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


def get_crawl_state(section_url: str) -> dict:
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
