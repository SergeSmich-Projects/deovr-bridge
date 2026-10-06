
# -*- coding: utf-8 -*-
"""
Flask-сервер - "мост" между форумом, TorrServe и DeoVR.

Эндпоинты:
  GET /deovr            - главный список для DeoVR (карточки, вкладки = теги)
  GET /video/<id>        - описание одного видео (резолвит ссылку в TorrServe лениво, по клику)
  GET /thumb/<id>        - прокси превью-картинки (на случай хотлинк-защиты форума)
  GET /refresh           - вручную пересобрать кэш с форума
  GET /health            - проверка живости

Как подключить в DeoVR на Quest:
  1. Запустить этот сервер на ПК в той же Wi-Fi сети, что и шлем.
  2. В DeoVR открыть "Интернет браузер" (библиотека по URL) и ввести:
       http://<IP-компьютера>:9999/deovr
     либо на компьютере положить файл по адресу /deovr (без расширения) -
     тогда достаточно ввести в DeoVR просто http://<IP-компьютера>:9999
"""

import base64
import hashlib
import html
import json
import logging
import math
import re
import struct
import threading
import time
import unicodedata
import urllib.parse
import zlib

from flask import Flask, jsonify, request, Response, redirect

import config
import store
import scraper
import torrserve

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

app = Flask(__name__)
store.init_db()


def _base_url() -> str:
    """
    Базовый адрес сервера так, как его видит клиент (DeoVR), а не фиксированный
    config.BRIDGE_PUBLIC_URL. Это важно: DeoVR на Quest открывает /deovr по
    LAN-IP компьютера (например http://192.168.1.186:9999), и ссылки video_url
    внутри карточек должны вести на ТОТ ЖЕ адрес, а не на 127.0.0.1 - иначе
    клик по карточке никуда не уходит (Quest обращается сам к себе).
    """
    return request.host_url.rstrip("/")


def _torrserve_public_base_url() -> str:
    """
    Адрес TorrServe так, как его должен увидеть САМ ПЛЕЕР (Quest) в ссылке
    потока - по той же логике, что и _base_url() выше: берём ХОСТ/IP из того
    же запроса, которым Quest уже достучался до нас (а не 127.0.0.1 из
    config.TORRSERVE_BASE_URL - для Quest 127.0.0.1 означает его самого, а не
    ваш ПК!), и меняем только порт на TorrServe (config.TORRSERVE_PUBLIC_PORT).
    Работает ТОЛЬКО если TorrServe слушает 0.0.0.0 (все интерфейсы), а не
    127.0.0.1 - см. комментарий у TORRSERVE_PUBLIC_PORT в config.py.
    """
    host = request.host.split(":")[0]
    scheme = request.scheme
    return f"{scheme}://{host}:{config.TORRSERVE_PUBLIC_PORT}"


def _is_item_visible(item: dict) -> bool:
    """Единый фильтр выдачи библиотеки.

    Скрапер может продолжать наполнять БД из всех источников, но некоторые
    источники можно временно скрыть из плееров через config.HIDDEN_LIBRARY_SITE_IDS.
    Сейчас по умолчанию скрыт xxxclub.
    """
    return (item.get("site") or "pornolab") not in config.HIDDEN_LIBRARY_SITE_IDS


def _library_items() -> list:
    return [item for item in store.all_items() if _is_item_visible(item)]


def _get_library_item(item_id: str):
    item = store.get_item(item_id)
    if not item or not _is_item_visible(item):
        return None
    return item


def _placeholder_url(base_url: str) -> str:
    return f"{base_url}/placeholder.png"


def _entity_placeholder_url(base_url: str, kind: str, entity_id: str, title: str) -> str:
    return (
        f"{base_url}/entity-placeholder/{urllib.parse.quote(kind, safe='')}/"
        f"{urllib.parse.quote(entity_id, safe='')}.png?title="
        f"{urllib.parse.quote(title or entity_id, safe='')}"
    )


def _image_url(item: dict, base_url: str) -> str:
    thumb = (item.get("thumbnail_url") or "").strip()
    return thumb or _placeholder_url(base_url)


def _seed_leech_suffix(item: dict) -> str:
    """Короткая приписка вида ' [S:46 L:9]' для отображения сидов/личей там, где
    у плеера нет отдельного поля под это (ни DeoVR, ни HereSphere не умеют
    показывать произвольные числовые метрики кроме как текстом) - встраиваем
    в заголовок. Если данных нет (xxxclub - DHT-магнет без страницы трекера,
    либо старая карточка до этой миграции) - ничего не добавляем."""
    seeders = item.get("seeders")
    leechers = item.get("leechers")
    if seeders is None and leechers is None:
        return ""
    s = seeders if seeders is not None else "?"
    l = leechers if leechers is not None else "?"
    return f" [S:{s} L:{l}]"


def _deovr_response(dto: dict):
    """Единая обёртка для DeoVR JSON.

    jsonify и так выставляет application/json, но явно фиксируем charset/CORS/no-store:
    это убирает лишние переменные при диагностике ситуаций, когда DeoVR вместо
    Selection Scene показывает сырой текст ответа.
    """
    resp = jsonify(dto)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _deovr_numeric_id(item_id) -> int:
    """DeoVR-документация описывает поле id как число.

    В нашей БД id текстовый: у pornolab это обычно "12345", а у xxxclub - "xc_...".
    Для JSON DeoVR оставляем стабильный числовой id, а настоящая строка id остаётся
    в URL (/video/<id>) и не теряется.
    """
    raw = str(item_id)
    if raw.isdigit():
        return int(raw)
    return 1_000_000_000 + (zlib.crc32(raw.encode("utf-8")) % 1_000_000_000)


def _deovr_resolution(item: dict) -> int:
    try:
        height = int(item.get("video_height") or 0)
    except (TypeError, ValueError):
        height = 0
    return height if height > 0 else 1080


def _item_to_card(item: dict, base_url: str) -> dict:
    """Короткий формат карточки для списка.

    По официальному shortened format DeoVR в списке нужны только title,
    videoLength, thumbnailUrl и video_url. Раньше мы добавляли ещё строковый id
    (например, "xc_123"), но у DeoVR id типизирован как number; на некоторых
    версиях плеера это может сорвать распознавание JSON как библиотеки и показать
    сырой текст. Поэтому id отдаём только в полной карточке /video/<id>, уже как
    число через _deovr_numeric_id().
    """
    return {
        "title": item["title"] + _seed_leech_suffix(item),
        "videoLength": item.get("video_length") or 0,
        "thumbnailUrl": _image_url(item, base_url),
        "video_url": f"{base_url}/video/{urllib.parse.quote(str(item['id']), safe='')}",
    }


# --- защита от лишних скачиваний .torrent (см. config.RESOLVE_FAIL_COOLDOWN_*) ----
# Храним в памяти процесса: для каждой карточки, у которой последняя попытка
# резолва провалилась, - время окончания "заморозки" и текст ошибки. Пока не
# истекла заморозка - НИКАКИХ обращений к форуму/TorrServe не делаем, а сразу
# отдаём тот же текст ошибки. При новой неудаче пауза удваивается (до потолка).
_resolve_fail_until = {}   # item_id -> unix_ts
_resolve_fail_backoff = {}  # item_id -> текущая пауза в секундах
_resolve_fail_error = {}   # item_id -> текст последней ошибки

# Сериализация РЕАЛЬНЫХ попыток резолва по каждому источнику (сайту). Без этого
# несколько почти одновременных запросов к разным карточкам одного сайта (плеер
# при открытии библиотеки действительно умеет дёргать метаданные сразу нескольких
# карточек параллельно, а не только выбранной) могли бы ВСЕ успеть уйти на форум
# ДО того, как первая неудача выставит общую блокировку - и тогда вместо одной
# потраченной попытки сгорело бы сразу несколько. С блокировкой по сайту только
# самый первый запрос реально стучится на форум, остальные ждут своей очереди и
# почти наверняка увидят уже выставленную блокировку/заморозку без похода в сеть.
_site_locks = {}
_site_locks_guard = threading.Lock()


def _lock_for_site(site_id: str) -> threading.Lock:
    with _site_locks_guard:
        lock = _site_locks.get(site_id)
        if lock is None:
            lock = threading.Lock()
            _site_locks[site_id] = lock
        return lock


# --- защита от "сгорания" суточного лимита при простом пролистывании библиотеки ---
# (см. config.RESOLVE_RATE_LIMIT_*) - плеер может дёргать /video/<id> для многих
# РАЗНЫХ карточек подряд сам, без осознанного нажатия play (похоже на предзагрузку
# превью/метаданных при скролле грида). Храним по каждому источнику временные
# метки ПОСЛЕДНИХ РЕАЛЬНЫХ (не из кэша) резолвов - если за скользящее окно их
# уже накопилось config.RESOLVE_RATE_LIMIT_COUNT, дальнейшие НОВЫЕ резолвы этого
# источника сразу отклоняются без обращения к форуму/TorrServe, пока окно не
# "рассосётся". На уже закэшированные (недавно резолвленные) карточки не влияет -
# они как и раньше отдаются мгновенно из кэша.
_resolve_rate_hits = {}  # site_id -> list[unix_ts] (только реальные новые резолвы)
_resolve_rate_guard = threading.Lock()


def _check_resolve_rate_limit(site_id: str):
    """Бросает torrserve.TorrServeError и НИЧЕГО не резолвит, если окно исчерпано.
    Если есть место - сразу резервирует "слот" (чтобы параллельные запросы по
    разным карточкам одного сайта не проскочили мимо лимита все разом)."""
    now = time.time()
    window = config.RESOLVE_RATE_LIMIT_WINDOW_SEC
    with _resolve_rate_guard:
        hits = [t for t in _resolve_rate_hits.get(site_id, []) if now - t < window]
        if len(hits) >= config.RESOLVE_RATE_LIMIT_COUNT:
            wait_sec = int(window - (now - hits[0]))
            _resolve_rate_hits[site_id] = hits
            raise torrserve.TorrServeError(
                f"Источник '{site_id}': за последние {window} сек. уже было "
                f"{len(hits)} новых резолвов (похоже на автопрогрев плеера при "
                f"пролистывании библиотеки, а не осознанный выбор видео) - "
                f"чтобы не сжигать суточный лимит скачиваний впустую, новые резолвы "
                f"этого источника приостановлены ещё ~{max(wait_sec, 1)} сек."
            )
        hits.append(now)
        _resolve_rate_hits[site_id] = hits

# Ключи в kv_meta - ОТДЕЛЬНЫЕ для каждого источника (pornolab / клон и т.п.), т.к.
# у каждого сайта свой независимый суточный лимит скачиваний - блокировка одного
# НЕ должна останавливать резолв по другому.
def _kv_daily_limit_until(site_id: str) -> str:
    return f"daily_limit_blocked_until:{site_id}"


def _kv_daily_limit_error(site_id: str) -> str:
    return f"daily_limit_last_error:{site_id}"


def _is_daily_limit_error(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in config.DAILY_LIMIT_MARKERS)


def _global_block_remaining(site_id: str) -> tuple:
    """Возвращает (секунд_осталось, текст_ошибки) если активна общая блокировка
    "суточный лимит ЭТОГО источника исчерпан", иначе (0, None). Состояние лежит
    в БД, переживает перезапуск сервера."""
    until = int(store.get_meta(_kv_daily_limit_until(site_id), "0") or "0")
    now = int(time.time())
    if until > now:
        return until - now, store.get_meta(_kv_daily_limit_error(site_id), "")
    return 0, None


def _resolve_cached_stream_url(item: dict) -> str:
    """
    Переиспользует уже полученный ранее stream_url из TorrServe, если он не
    старше STREAM_CACHE_TTL_SEC - иначе резолвит заново (магнит/скачивание
    .torrent + upload в TorrServe) и сохраняет в кэш. Нужно, т.к. HereSphere
    и повторные открытия карточки иначе дёргали бы TorrServe на каждый запрос.

    Дополнительно: прежде чем лезть на форум, проверяет (а) глобальную
    блокировку "суточный лимит скачиваний исчерпан" и (б) персональную
    "заморозку" этой же карточки после её недавней неудачи - в обоих случаях
    никакого реального HTTP-запроса не делается, чтобы не жечь лимит впустую.
    """
    now = int(time.time())
    cached = item.get("stream_url")
    built_at = item.get("stream_built_at") or 0
    if cached and (now - built_at) < config.STREAM_CACHE_TTL_SEC:
        return cached

    item_id = item["id"]
    site_id = item.get("site") or "pornolab"

    remaining, last_error = _global_block_remaining(site_id)
    if remaining > 0:
        mins = remaining // 60
        raise torrserve.TorrServeError(
            f"Суточный лимит скачиваний источника '{site_id}' исчерпан - резолв приостановлен ещё "
            f"~{mins} мин., чтобы не тратить лимит впустую. Последнее сообщение форума: {last_error}"
        )

    fail_until = _resolve_fail_until.get(item_id, 0)
    if fail_until > now:
        wait_sec = fail_until - now
        raise torrserve.TorrServeError(
            f"Эта раздача недавно уже не резолвилась, повтор отложен ещё на {wait_sec} сек. "
            f"(чтобы не долбить форум на каждый показ карточки). Последняя ошибка: "
            f"{_resolve_fail_error.get(item_id, '')}"
        )

    # Реальный поход на форум - СЕРИАЛИЗУЕМ по источнику (см. комментарий у _site_locks):
    # если несколько карточек одного сайта резолвятся почти одновременно, в сеть
    # пойдёт только первая, остальные дождутся своей очереди и, скорее всего, увидят
    # уже выставленную этой первой попыткой блокировку - без лишних походов на форум.
    with _lock_for_site(site_id):
        now = int(time.time())

        # состояние могло измениться, пока ждали очереди - проверяем ещё раз
        remaining, last_error = _global_block_remaining(site_id)
        if remaining > 0:
            mins = remaining // 60
            raise torrserve.TorrServeError(
                f"Суточный лимит скачиваний источника '{site_id}' исчерпан - резолв приостановлен ещё "
                f"~{mins} мин., чтобы не тратить лимит впустую. Последнее сообщение форума: {last_error}"
            )
        fail_until = _resolve_fail_until.get(item_id, 0)
        if fail_until > now:
            wait_sec = fail_until - now
            raise torrserve.TorrServeError(
                f"Эта раздача недавно уже не резолвилась, повтор отложен ещё на {wait_sec} сек. "
                f"(чтобы не долбить форум на каждый показ карточки). Последняя ошибка: "
                f"{_resolve_fail_error.get(item_id, '')}"
            )

        # Только для источников со СЧЁТНЫМ суточным лимитом скачивания .torrent
        # (source_type == "torrent_url") - у magnet-источников (например, xxxclub)
        # никакого ограничиваемого ресурса форума не тратится, их не ограничиваем.
        if item.get("source_type") == "torrent_url":
            _check_resolve_rate_limit(site_id)

        try:
            if item.get("source_type") == "torrent_url" and item.get("topic_id"):
                # "освежаем" анти-лич cookie (bb_dl и т.п.) именно для ЭТОЙ темы перед скачиванием -
                # см. scraper.touch_topic
                scraper.touch_topic(site_id, item["topic_id"])

            stream_url = torrserve.resolve_stream_url(
                item["source_type"], item["source"],
                forum_session=scraper.get_forum_session(site_id),
                title=item["title"],
                public_base_url=_torrserve_public_base_url(),
            )
        except Exception as e:
            err_text = str(e)
            # эскалация паузы для этой конкретной карточки
            prev_backoff = _resolve_fail_backoff.get(item_id, 0)
            backoff = min(
                prev_backoff * 2 if prev_backoff else config.RESOLVE_FAIL_COOLDOWN_SEC,
                config.RESOLVE_FAIL_COOLDOWN_MAX_SEC,
            )
            _resolve_fail_backoff[item_id] = backoff
            _resolve_fail_until[item_id] = now + backoff
            _resolve_fail_error[item_id] = err_text

            if _is_daily_limit_error(err_text):
                # это не проблема конкретной раздачи, а общий бан ИМЕННО ЭТОГО источника -
                # замораживаем резолв для всех карточек ЭТОГО сайта (у другого источника
                # свой независимый суточный лимит, его не трогаем)
                store.set_meta(_kv_daily_limit_until(site_id), str(now + config.DAILY_LIMIT_COOLDOWN_SEC))
                store.set_meta(_kv_daily_limit_error(site_id), err_text)
                log.warning(
                    "[%s] Обнаружен суточный лимит скачиваний - резолв по этому источнику "
                    "полностью приостановлен на %d сек.", site_id, config.DAILY_LIMIT_COOLDOWN_SEC
                )
            raise

        # успех - сбрасываем историю неудач по этой карточке
        _resolve_fail_until.pop(item_id, None)
        _resolve_fail_backoff.pop(item_id, None)
        _resolve_fail_error.pop(item_id, None)

        store.save_stream_url(item_id, stream_url)
        scraper.persist_cookies(site_id)  # сессия могла обновиться - сохраняем для следующего запуска
        return stream_url


# --- HereSphere ---------------------------------------------------------
# Формат по официальной схеме плеера HereSphere (https://heresphere.com):
#   POST /heresphere          -> {"access":1, "library":[{"name":..., "list":[url,...]}]}
#   POST /heresphere/scan     -> {"scanData":[{"link":..., "title":..., "tags":[...]}]}  (для сайдбара тегов)
#   GET|POST /heresphere/<id> -> полная карточка одного видео (здесь же лениво резолвим TorrServe)
# В отличие от DeoVR, HereSphere сам строит боковую панель тегов/актрис/студий
# по полю "tags" каждого видео - достаточно присылать теги с префиксами:
#   "#:Тег" - обычный тег, "@:Имя" - актриса, "Studio:Имя" - студия.
_HERESPHERE_PROJECTION = {
    "dome": ("equirectangular", None),
    "sphere": ("equirectangular360", None),
    "fisheye": ("fisheye", 180.0),
}


def _heresphere_tags(item: dict) -> list:
    tags = [{"name": f"#:{t}"} for t in item.get("tags", [])]
    tags += [{"name": f"@:{p}"} for p in item.get("performers", [])]
    tags += [{"name": f"Studio:{s}"} for s in item.get("studio", [])]
    return tags


def _heresphere_summary_tag(item: dict) -> list:
    """Тег 'Summary:...' - по конвенции HereSphere (см. heresphere-server/stash-vr)
    показывается отдельной строкой НАД шкалой проигрывателя, а не в общем списке
    тегов - подходящее место для сидов/личей. НЕ добавляем его в _heresphere_tags(),
    потому что та функция также используется в /heresphere/scan для построения
    общего ДЕРЕВА тегов библиотеки - там уникальный Summary на каждое видео
    только засорил бы список тегов для фильтрации."""
    seeders = item.get("seeders")
    leechers = item.get("leechers")
    if seeders is None and leechers is None:
        return []
    s = seeders if seeders is not None else "?"
    l = leechers if leechers is not None else "?"
    return [{"name": f"Summary:Сиды {s} / Личи {l}"}]


def _heresphere_video_url(base_url: str, item_id: str) -> str:
    return f"{base_url}/heresphere/{urllib.parse.quote(str(item_id), safe='')}"


def _heresphere_date_added(item: dict) -> str:
    ts = item.get("updated_at") or int(time.time())
    return time.strftime("%Y-%m-%d", time.gmtime(ts))



@app.get("/health")
def health():
    all_items = store.all_items()
    visible_items = [item for item in all_items if _is_item_visible(item)]
    return jsonify({
        "status": "ok",
        "items": len(visible_items),
        "items_total": len(all_items),
        "hidden_sites": sorted(config.HIDDEN_LIBRARY_SITE_IDS),
    })


_PLACEHOLDER_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


@app.get("/placeholder.png")
@app.get("/static/placeholder.jpg")
def placeholder_image():
    return Response(_PLACEHOLDER_PNG, mimetype="image/png")


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _entity_placeholder_png(kind: str, entity_id: str) -> bytes:
    """Небольшая PNG-заглушка без внешних зависимостей.

    SVG в PLAY'A на некоторых устройствах показывается как пустая карточка, а
    Pillow в проекте нет. Генерируем простую PNG-картинку стандартной библиотекой:
    цвет стабилен для entity_id, актёры получают силуэт, студии — плитку.
    """
    digest = hashlib.sha1(f"{kind}:{entity_id}".encode("utf-8")).digest()
    base = (70 + digest[0] % 90, 45 + digest[1] % 90, 90 + digest[2] % 120)
    accent = (min(base[0] + 70, 255), min(base[1] + 70, 255), min(base[2] + 70, 255))
    width = height = 300
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            # Мягкий фон + простая иконка, чтобы заглушка не выглядела пустой.
            shade = int(28 * (x / width) + 18 * (y / height))
            r, g, b = (min(base[0] + shade, 255), min(base[1] + shade, 255), min(base[2] + shade, 255))
            if kind == "studio":
                if 78 <= x <= 222 and 92 <= y <= 208:
                    r, g, b = accent
                if (98 <= x <= 202 and 118 <= y <= 138) or (98 <= x <= 202 and 162 <= y <= 182):
                    r, g, b = (245, 245, 245)
            else:
                if (x - 150) ** 2 + (y - 112) ** 2 <= 48 ** 2:
                    r, g, b = accent
                if ((x - 150) / 78) ** 2 + ((y - 214) / 58) ** 2 <= 1:
                    r, g, b = accent
            row.extend((r, g, b))
        rows.append(bytes(row))
    raw = b"".join(rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, 6))
        + _png_chunk(b"IEND", b"")
    )


@app.get("/entity-placeholder/<kind>/<entity_id>.png")
@app.get("/entity-placeholder/<kind>/<entity_id>.svg")
def entity_placeholder(kind, entity_id):
    # .svg оставляем как legacy URL, но фактически отдаём PNG: PLAY'A так
    # стабильнее показывает fallback-карточки актёров/студий.
    resp = Response(_entity_placeholder_png(kind, entity_id), mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.get("/entity-image/<kind>/<entity_id>/<digest>.<ext>")
def entity_image_proxy(kind, entity_id, digest, ext):
    """Локальный proxy для внешних постеров/логотипов.

    PLAY'A иногда не показывает внешние CDN/SVG/WebP так же стабильно, как URL с
    того же хоста, где находится API. Поэтому в JSON отдаём локальный URL, а этот
    endpoint забирает разрешённые CDN-картинки и возвращает их с нормальным
    Content-Type. Это не open proxy: разрешены только cdn.vrspy.com и
    images.povr.com.
    """
    url = _normalize_external_image_url(request.args.get("url") or "")
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.lower()
    if parsed.scheme not in {"http", "https"} or host not in {"cdn.vrspy.com", "images.povr.com"}:
        return "", 404
    try:
        content, content_type = _external_binary_get(url)
    except Exception as e:
        log.info("Не удалось проксировать entity image %s/%s: %s", kind, entity_id, e)
        content, content_type = _entity_placeholder_png(kind, entity_id), "image/png"
    resp = Response(content, mimetype=content_type.split(";", 1)[0] or "image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.get("/debug/sites")
def debug_sites():
    """Быстрая диагностика без прокрутки огромного /deovr - сколько карточек
    реально накоплено по каждому источнику и парочка примеров с каждого."""
    items = store.all_items()
    by_site = {}
    for i in items:
        by_site.setdefault(i.get("site") or "pornolab", []).append(i)

    out = {}
    for site in config.FORUM_SITES:
        site_items = by_site.get(site["id"], [])
        out[site["id"]] = {
            "label": site.get("name") or "(основной)",
            "count": len(site_items),
            "sample": [
                {"id": i["id"], "title": i["title"], "tags": i["tags"],
                 "source_type": i["source_type"], "has_source": bool(i["source"]),
                 "thumbnail_url": i["thumbnail_url"]}
                for i in site_items[:5]
            ],
        }
    return jsonify(out)


@app.get("/refresh")
def refresh():
    # В проде лучше защитить токеном/запускать по крону, тут - ручной запуск для теста
    count = scraper.refresh_all()
    return jsonify({"status": "ok", "items_updated": count})


def _site_label(site_id: str) -> str:
    """Название из config.FORUM_SITES (пустое у основного источника - без приставки
    к вкладкам, как было раньше); у клона и т.п. - "Клон", "Клон2" и т.д."""
    for site in config.FORUM_SITES:
        if site["id"] == site_id:
            return site.get("name") or ""
    return site_id


def _prefixed(label: str, name: str) -> str:
    return f"{label}: {name}" if label else name


@app.route("/deovr", methods=["GET", "POST"])
@app.route("/", methods=["GET", "POST"])
def deovr_root():
    """
    Источники НЕ смешиваются между собой (так попросил пользователь) - у каждого
    источника (config.FORUM_SITES) своя вкладка "Всё"/"Клон: Всё" и свои вкладки
    по тегам ("<тег>" / "Клон: <тег>"), посчитанные отдельно для его собственных
    карточек.
    """
    base_url = _base_url()
    all_items = _library_items()

    items_by_site = {}
    for i in all_items:
        items_by_site.setdefault(i.get("site") or "pornolab", []).append(i)

    scenes = []
    for site in config.FORUM_SITES:
        items = items_by_site.get(site["id"], [])
        label = site.get("name") or ""

        # items уже отсортированы по source_rank ASC, т.е. ТОЧНО как на самом
        # трекере (см. store.all_items()) - берём только первые config.MAX_SCENE_ITEMS,
        # иначе на разделах
        # с большим архивом (пройденным полностью по backfill) ответ /deovr
        # разрастается до десятков мегабайт и роняет браузер/плеер (см.
        # комментарий у MAX_SCENE_ITEMS в config.py). В БД ничего не удаляется -
        # это только ограничение одного JSON-ответа.
        scenes.append({
            "name": _prefixed(label, "Всё"),
            "list": [_item_to_card(i, base_url) for i in items[: config.MAX_SCENE_ITEMS]],
        })

        # счётчики тегов считаем по ПОЛНОМУ списку (чтобы топ самых частых тегов
        # не зависел от обрезки выше), а вот каждый итоговый список по тегу тоже обрезаем
        tag_counts = {}
        for i in items:
            for t in i["tags"]:
                tag_counts.setdefault(t, []).append(i)

        # самые часто встречающиеся теги - по одной вкладке на каждый (классический вид DeoVR)
        top_tags = sorted(tag_counts.items(), key=lambda kv: -len(kv[1]))[: config.MAX_TAG_SCENES]
        for tag, tag_items in top_tags:
            scenes.append({
                "name": _prefixed(label, tag),
                "list": [_item_to_card(i, base_url) for i in tag_items[: config.MAX_SCENE_ITEMS]],
            })

    return _deovr_response({"scenes": scenes, "authorized": 0})


@app.route("/video/<item_id>", methods=["GET", "POST"])
def video_detail(item_id):
    """
    ВАЖНО: этот адрес плеер (DeoVR) дёргает для КАЖДОЙ карточки, которую
    показывает в сетке/подгружает заранее - НЕ только по клику play. Поэтому
    здесь НИЧЕГО не резолвится по-настоящему (не качается .torrent, не
    трогается TorrServe/форум) - только отдаём лёгкие метаданные. Ссылка
    videoSources.url ведёт на НАШ ЖЕ /stream/<id>, который и делает реальный
    резолв, но ТОЛЬКО когда плеер по-настоящему начинает читать видео-поток
    (т.е. реально нажали play) - см. stream_redirect().
    """
    item = _get_library_item(item_id)
    if not item:
        return jsonify({"error": "not found"}), 404

    base_url = _base_url()
    stream_url = f"{base_url}/stream/{urllib.parse.quote(str(item_id), safe='')}"

    resolution = _deovr_resolution(item)

    return _deovr_response({
        "authorized": 0,
        "id": _deovr_numeric_id(item_id),
        "title": item["title"] + _seed_leech_suffix(item),
        "is3d": True,
        "screenType": item["screen_type"],
        "stereoMode": item["stereo_mode"],
        "videoLength": item.get("video_length") or 0,
        "thumbnailUrl": _image_url(item, base_url),
        "encodings": [{
            "name": "h264",
            "videoSources": [{"resolution": resolution, "height": resolution, "url": stream_url}],
        }],
    })


@app.get("/stream/<item_id>")
def stream_redirect(item_id):
    """
    Единственное место, где реально качается .torrent/добавляется magnet в
    TorrServe - сюда плеер обращается ТОЛЬКО когда по-настоящему начинает
    читать видео-поток (т.е. пользователь реально нажал play), а не просто
    при подгрузке карточки/метаданных (см. комментарий в video_detail()).
    Резолвим и делаем редирект на настоящий адрес TorrServe.
    """
    item = _get_library_item(item_id)
    if not item:
        return jsonify({"error": "not found"}), 404

    try:
        stream_url = _resolve_cached_stream_url(item)
    except Exception as e:
        log.exception("Не удалось получить поток из TorrServe для %s: %s", item_id, e)
        return jsonify({"error": f"torrserve error: {e}"}), 502

    return redirect(stream_url, code=302)


def _heresphere_library(base_url: str) -> list:
    """Как и в /deovr - отдельная вкладка библиотеки на каждый источник, без смешивания."""
    items_by_site = {}
    for i in _library_items():
        items_by_site.setdefault(i.get("site") or "pornolab", []).append(i)

    library = []
    for site in config.FORUM_SITES:
        items = items_by_site.get(site["id"], [])
        label = site.get("name") or ""
        library.append({
            "name": _prefixed(label, "Всё"),
            "list": [_heresphere_video_url(base_url, i["id"]) for i in items[: config.MAX_SCENE_ITEMS]],
        })
    return library


@app.route("/heresphere", methods=["GET", "POST"])
@app.route("/heresphere/", methods=["GET", "POST"])
def heresphere_index():
    base_url = _base_url()
    # Реальный плеер стучится сюда через POST; GET оставляем для проверки
    # из обычного браузера, чтобы не возвращать пустую страницу.
    dto = {"access": 1, "library": _heresphere_library(base_url)}
    resp = jsonify(dto)
    resp.headers["HereSphere-JSON-Version"] = "1"
    return resp


@app.route("/heresphere/scan", methods=["GET", "POST"])
def heresphere_scan():
    """Отдаёт теги/заголовки ВСЕХ видео одним запросом - именно отсюда HereSphere
    строит свою боковую панель тегов/актрис, не дёргая каждое видео по отдельности."""
    base_url = _base_url()
    scan_data = []
    for i in _library_items():
        scan_data.append({
            "link": _heresphere_video_url(base_url, i["id"]),
            "title": i["title"],
            "dateAdded": _heresphere_date_added(i),
            "duration": (i.get("video_length") or 0) * 1000,
            "tags": _heresphere_tags(i),
        })
    resp = jsonify({"scanData": scan_data})
    resp.headers["HereSphere-JSON-Version"] = "1"
    return resp


@app.route("/heresphere/events/<item_id>", methods=["POST"])
def heresphere_events(item_id):
    # События play/pause/close от плеера - статистику пока не ведём, просто подтверждаем приём.
    return "", 204


@app.route("/heresphere/<item_id>", methods=["GET", "POST"])
def heresphere_video(item_id):
    item = _get_library_item(item_id)
    if not item:
        return jsonify({"access": -1}), 404

    base_url = _base_url()
    projection, fov = _HERESPHERE_PROJECTION.get(item.get("screen_type"), ("equirectangular", None))
    stereo = "tb" if item.get("stereo_mode") == "tb" else "sbs"

    dto = {
        "access": 1,
        "title": item["title"] + _seed_leech_suffix(item),
        "thumbnailImage": _image_url(item, base_url),
        "dateAdded": _heresphere_date_added(item),
        "duration": (item.get("video_length") or 0) * 1000,
        "projection": projection,
        "stereo": stereo,
        "tags": _heresphere_tags(item) + _heresphere_summary_tag(item),
        "eventServer": f"{base_url}/heresphere/events/{urllib.parse.quote(str(item_id), safe='')}",
        "writeFavorite": False,
        "writeRating": False,
        "writeTags": False,
    }
    if fov:
        dto["fov"] = fov

    # Как и в /video/<id> (DeoVR) - НИЧЕГО не резолвим по-настоящему здесь,
    # этот адрес плеер может дёргать просто при подгрузке карточки. Реальный
    # резолв - только в /stream/<id>, когда плеер действительно начнёт читать
    # видео-поток (нажали play).
    stream_url = f"{base_url}/stream/{urllib.parse.quote(str(item_id), safe='')}"
    resolution = _deovr_resolution(item)
    dto["media"] = [{"name": "TorrServe", "sources": [{"resolution": resolution, "url": stream_url}]}]

    resp = jsonify(dto)
    resp.headers["HereSphere-JSON-Version"] = "1"
    return resp


# --- PLAY'A VR API v2 ---------------------------------------------------
# Документация: https://github.com/Playa-vr/PLAYA-API-v2/blob/main/docs.md
# PLAY'A при добавлении сайта автоматически обращается к /api/playa/v2/version,
# затем /config и /videos. Аутентификацию отключаем (auth=false), чтобы мост
# работал как локальная гостевая библиотека без JWT/логина.
_PLAYA_PREFIX = "/api/playa/v2"

_PLAYA_PROJECTION = {
    "flat": "FLT",
    "dome": "180",
    "sphere": "360",
    "fisheye": "FSH",
    "mkx200": "FSH",
    "rf52": "FSH",
}
_PLAYA_STEREO = {
    "sbs": "LR",
    "lr": "LR",
    "tb": "TB",
    "overunder": "TB",
    "off": "MN",
    "mono": "MN",
    "mn": "MN",
}


def _playa_response(data=None, code: int = 1, message: str = None):
    status = {"code": code}
    if message:
        status["message"] = message
    payload = {"status": status}
    if code == 1 and data is not None:
        payload["data"] = data
    resp = jsonify(payload)
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _playa_not_found(message: str = "Not found"):
    return _playa_response(code=404, message=message)


def _playa_arg(name: str, default: str = "") -> str:
    """Читает query-параметр PLAY'A.

    В логах реального клиента уже встретились URL с HTML-escaped разделителями
    `&amp;` внутри query string. Flask в таком случае видит ключи вида
    `amp;page-size`, поэтому для устойчивости поддерживаем оба варианта.
    """
    return request.args.get(name, request.args.get(f"amp;{name}", default))


def _playa_entity_id(kind: str, title: str) -> str:
    digest = hashlib.sha1(f"{kind}:{title}".encode("utf-8")).hexdigest()[:16]
    return f"{kind}_{digest}"


def _slugify_povr(title: str) -> str:
    """Best-effort slug для публичных страниц POVR: Mia Malkova -> mia-malkova."""
    text = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode("ascii")
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text


def _external_get(url: str) -> str:
    """GET для внешних метаданных с браузерным отпечатком, если доступен curl_cffi."""
    timeout = config.EXTERNAL_ENTITY_META_TIMEOUT_SEC
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        from curl_cffi import requests as curl_requests
        resp = curl_requests.get(url, headers=headers, timeout=timeout, impersonate="chrome124")
    except Exception:
        import requests
        resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    encoding = getattr(resp, "encoding", None) or "utf-8"
    content = getattr(resp, "content", None)
    if content is not None:
        return content.decode(encoding, errors="ignore")
    return resp.text


def _first_abs_url(base_url: str, value: str) -> str:
    if not value:
        return ""
    return urllib.parse.urljoin(base_url, value)


def _normalize_external_image_url(url: str) -> str:
    """Делаем внешние картинки дружелюбнее для PLAY'A.

    VRSpy часто отдаёт URL с query `format=webp`; часть клиентов плохо показывает
    webp, хотя путь уже указывает на .jpg. POVR-лого иногда находится как SVG,
    а PLAY'A на некоторых платформах SVG-превью не показывает. Поэтому для
    сохранения в metadata приводим такие ссылки к jpg/png-путям без query.
    """
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.netloc.lower()
    path = parsed.path
    if host.endswith("cdn.vrspy.com"):
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    if host.endswith("images.povr.com") and path.lower().endswith("/200.svg"):
        path = path[:-len("200.svg")] + "t760povr.png"
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    return url


def _image_urls_from_tag(img, base_url: str) -> list:
    urls = []
    for attr in ("src", "data-src", "data-original", "data-lazy-src"):
        value = img.get(attr) if img else ""
        if value:
            urls.append(_first_abs_url(base_url, value))
    for attr in ("srcset", "data-srcset"):
        value = img.get(attr) if img else ""
        if not value:
            continue
        for part in value.split(","):
            candidate = part.strip().split(" ", 1)[0]
            if candidate:
                urls.append(_first_abs_url(base_url, candidate))
    result = []
    seen = set()
    for url in urls:
        url = _normalize_external_image_url(url)
        if url and url not in seen:
            seen.add(url)
            result.append(url)
    return result


_ENTITY_META_SOURCE_MARKER = "deovr-bridge-meta-v2"


def _mark_entity_meta_source(url: str) -> str:
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, _ENTITY_META_SOURCE_MARKER))


def _has_entity_meta_source_marker(url: str) -> bool:
    return urllib.parse.urlsplit(url or "").fragment == _ENTITY_META_SOURCE_MARKER


def _compact_slug(slug: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", slug or "")


def _slugs_match(a: str, b: str) -> bool:
    a = a or ""
    b = b or ""
    return a == b or (_compact_slug(a) and _compact_slug(a) == _compact_slug(b))


def _is_vrspy_actor_source(title: str, source_url: str) -> bool:
    slug = _slugify_povr(title)
    if not slug or not source_url:
        return False
    parsed = urllib.parse.urlsplit(source_url)
    return parsed.netloc.lower().endswith("vrspy.com") and parsed.path.rstrip("/").lower().endswith(f"/star/{slug}")


def _is_vrspy_actor_image_url(title: str, image_url: str) -> bool:
    slug = _slugify_povr(title)
    if not slug or not image_url:
        return False
    parsed = urllib.parse.urlsplit(image_url)
    path = urllib.parse.unquote(parsed.path.lower())
    return parsed.netloc.lower().endswith("cdn.vrspy.com") and f"/stars/{slug}/" in path


def _is_povr_studio_source(title: str, source_url: str) -> bool:
    slug = _slugify_povr(title)
    if not slug or not source_url:
        return False
    parsed = urllib.parse.urlsplit(source_url)
    parts = [p for p in parsed.path.lower().split("/") if p]
    return parsed.netloc.lower().endswith("povr.com") and len(parts) >= 2 and parts[-2] == "studios" and _slugs_match(parts[-1], slug)


def _is_povr_studio_logo_url(image_url: str) -> bool:
    if not image_url:
        return False
    parsed = urllib.parse.urlsplit(image_url)
    path = parsed.path.lower()
    return (
        parsed.netloc.lower().endswith("images.povr.com")
        and "/assets/logos/channels/" in path
        and path.endswith((".png", ".jpg", ".jpeg", ".webp"))
    )


def _cached_entity_meta_is_current(kind: str, title: str, cached: dict) -> bool:
    if not cached:
        return False
    preview_url = cached.get("preview_url") or ""
    source_url = cached.get("source_url") or ""
    if kind == "actor":
        # Старый POVR-кэш давал одинаковые постеры. Для актёров считаем свежим
        # только новый кэш с реальной картинкой: либо VRSpy, либо опциональный
        # StashBox provider. Отрицательный результат не считаем свежим, чтобы
        # после исправлений/сбоев сети можно было ретраить.
        if not (preview_url and _has_entity_meta_source_marker(source_url)):
            return False
        return _is_vrspy_actor_source(title, source_url) and _is_vrspy_actor_image_url(title, preview_url) or bool(preview_url)
    if kind == "studio":
        # Для студий принимаем только новый кэш с настоящими POVR logo assets или
        # опциональный StashBox provider, а не случайные thumbnails/первую картинку.
        if not (preview_url and _has_entity_meta_source_marker(source_url)):
            return False
        return _is_povr_studio_source(title, source_url) and _is_povr_studio_logo_url(preview_url) or bool(preview_url)
    return True


def _external_image_url(base_url: str, kind: str, entity_id: str, source_url: str) -> str:
    source_url = _normalize_external_image_url(source_url)
    if not source_url:
        return ""
    parsed = urllib.parse.urlsplit(source_url)
    ext = (parsed.path.rsplit(".", 1)[-1] if "." in parsed.path else "jpg").lower()
    if ext not in {"jpg", "jpeg", "png", "webp"}:
        ext = "jpg"
    digest = hashlib.sha1(source_url.encode("utf-8")).hexdigest()[:12]
    return (
        f"{base_url}/entity-image/{urllib.parse.quote(kind, safe='')}/"
        f"{urllib.parse.quote(entity_id, safe='')}/{digest}.{ext}?url="
        f"{urllib.parse.quote(source_url, safe='')}"
    )


def _external_binary_get(url: str) -> tuple:
    timeout = config.EXTERNAL_ENTITY_META_TIMEOUT_SEC
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "image/jpeg,image/png,image/*,*/*;q=0.8",
    }
    try:
        from curl_cffi import requests as curl_requests
        resp = curl_requests.get(url, headers=headers, timeout=timeout, impersonate="chrome124")
    except Exception:
        import requests
        resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "image/jpeg")
    return resp.content, content_type


def _extract_povr_video_trailer(video_url: str) -> str:
    """Пробует достать прямой mp4 трейлера со страницы видео POVR.

    На страницах роликов POVR публично видны ссылки вида
    /download/<id>/trailer/ovr1920stream.mp4. В PLAY'A ActorView/StudioView
    отдельного поля trailer нет, поэтому сейчас используем это только как
    диагностическую/служебную ссылку в entity_meta/debug API.
    """
    try:
        html = _external_get(video_url)
    except Exception as e:
        log.info("Не удалось получить POVR video trailer %s: %s", video_url, e)
        return ""
    links = re.findall(r"href=[\"']([^\"']*/download/\d+/trailer/[^\"']+\.mp4)[\"']", html, re.I)
    if not links:
        links = re.findall(r"https?://[^\"'<> ]+/download/\d+/trailer/[^\"'<> ]+\.mp4", html, re.I)
    if not links:
        return ""
    # Для предпросмотра не нужен 6K гигабайтный трейлер: выбираем умеренное качество,
    # если оно есть, иначе первый попавшийся вариант.
    preferred = sorted(
        (_first_abs_url(video_url, link) for link in links),
        key=lambda u: ("1080" not in u and "1920" not in u, "ovr6k" in u.lower(), u),
    )
    return preferred[0]


_ENTITY_DIRECTORY_CACHE = {}
_ENTITY_DIRECTORY_CACHE_TTL_SEC = 6 * 3600


def _cached_directory_map(key: str):
    cached = _ENTITY_DIRECTORY_CACHE.get(key)
    if cached and (time.time() - cached[0]) < _ENTITY_DIRECTORY_CACHE_TTL_SEC:
        return cached[1]
    return None


def _set_directory_map(key: str, data: dict) -> dict:
    _ENTITY_DIRECTORY_CACHE[key] = (time.time(), data)
    return data


def _external_get_json(url: str, headers: dict = None) -> dict:
    timeout = config.EXTERNAL_ENTITY_META_TIMEOUT_SEC
    req_headers = {
        "User-Agent": "PLAYA/1.0 deovr-bridge/1.0",
        "Accept": "application/json",
    }
    if headers:
        req_headers.update(headers)
    try:
        from curl_cffi import requests as curl_requests
        resp = curl_requests.get(url, headers=req_headers, timeout=timeout, impersonate="chrome124")
    except Exception:
        import requests
        resp = requests.get(url, headers=req_headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _external_post_json(url: str, payload: dict, headers: dict = None) -> dict:
    timeout = config.EXTERNAL_ENTITY_META_TIMEOUT_SEC
    req_headers = {
        "User-Agent": "deovr-bridge/1.0",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if headers:
        req_headers.update(headers)
    body = json.dumps(payload).encode("utf-8")
    try:
        from curl_cffi import requests as curl_requests
        resp = curl_requests.post(url, data=body, headers=req_headers, timeout=timeout, impersonate="chrome124")
    except Exception:
        import requests
        resp = requests.post(url, data=body, headers=req_headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _playa_api_url(base_url: str, path: str, query: dict = None) -> str:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return url


def _best_playa_list_match(content: list, title: str) -> dict:
    if not isinstance(content, list):
        return {}
    slug = _slugify_povr(title)
    exact = []
    for entry in content:
        entry_title = entry.get("title") or ""
        entry_id = entry.get("id") or ""
        if _slugs_match(_slugify_povr(entry_title), slug) or _slugs_match(_slugify_povr(entry_id), slug):
            exact.append(entry)
    if exact:
        return exact[0]
    return content[0] if content else {}


def _playa_payload_ok(payload: dict) -> bool:
    return int(((payload.get("status") or {}).get("code") or 0)) == 1


def _playa_page_content(payload: dict) -> list:
    if not _playa_payload_ok(payload):
        return []
    data = payload.get("data") or {}
    if isinstance(data, dict):
        content = data.get("content") or []
        return content if isinstance(content, list) else []
    return []


def _vrporn_entity_meta_from_entry(kind: str, entry: dict, detail: dict = None) -> dict:
    detail = detail or {}
    entity_id = entry.get("id") or _slugify_povr(entry.get("title") or "")
    if not entity_id:
        return {}
    detail_prefix = "actor" if kind == "actor" else "studio"
    detail_url = _playa_api_url(
        config.VRPORN_BASE_URL.rstrip("/"),
        f"/api/playa/v2/{detail_prefix}/{urllib.parse.quote(entity_id, safe='')}",
    )
    preview_url = _normalize_external_image_url(detail.get("preview") or entry.get("preview") or "")
    if not preview_url:
        return {}
    return {
        "preview_url": preview_url,
        "banner_url": _normalize_external_image_url(detail.get("banner") or ""),
        "source_url": detail_url,
        "trailer_url": "",
        "description": detail.get("description") or "",
    }


def _fetch_vrporn_playa_studio_directory_map() -> dict:
    key = "vrporn-playa-studios"
    cached = _cached_directory_map(key)
    if cached is not None:
        return cached
    base_url = config.VRPORN_BASE_URL.rstrip("/")
    url = _playa_api_url(base_url, "/api/playa/v2/studios", {"page-size": 1000})
    content = _playa_page_content(_external_get_json(url))
    result = {}
    for entry in content:
        meta = _vrporn_entity_meta_from_entry("studio", entry)
        if not meta:
            continue
        title_slug = _slugify_povr(entry.get("title") or "")
        id_slug = _slugify_povr(entry.get("id") or "")
        compact_title = _compact_slug(title_slug)
        compact_id = _compact_slug(id_slug)
        for key_slug in {title_slug, id_slug, compact_title, compact_id}:
            if key_slug:
                result[key_slug] = meta
    return _set_directory_map(key, result)



def _fetch_vrporn_playa_entity_meta(kind: str, title: str) -> dict:
    """Берём artwork напрямую из PLAY'A API VRPorn.com.

    Это ровно тот источник/формат, который сам PLAY'A уже умеет показывать.
    Студии забираем пачкой из `/studios` (там всего ~400 записей), поэтому лого
    находятся одним запросом. Актёров ищем точечно через `title`, без detail-запроса:
    list view уже содержит `preview`, а второй запрос сильно замедлял прогрузку.
    """
    if kind == "studio":
        slug = _slugify_povr(title)
        directory = _fetch_vrporn_playa_studio_directory_map()
        return directory.get(slug) or directory.get(_compact_slug(slug)) or {}
    if kind != "actor":
        return {}

    base_url = config.VRPORN_BASE_URL.rstrip("/")
    search_url = _playa_api_url(base_url, "/api/playa/v2/actors", {"title": title, "page-size": 8})
    entry = _best_playa_list_match(_playa_page_content(_external_get_json(search_url)), title)
    if not entry:
        return {}
    return _vrporn_entity_meta_from_entry("actor", entry)



def _stashbox_headers(api_key: str) -> dict:
    headers = {}
    if api_key:
        # Разные Stash/Stash-Box инсталляции встречаются с разными схемами.
        # Обычно достаточно ApiKey, Authorization оставляем как совместимый bonus.
        headers["ApiKey"] = api_key
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _pick_stashbox_image(images) -> str:
    if not isinstance(images, list):
        return ""
    candidates = []
    for image in images:
        if isinstance(image, str):
            candidates.append(image)
        elif isinstance(image, dict):
            for key in ("url", "image", "path", "thumbnail", "thumbnail_url"):
                value = image.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    candidates.append(value)
                    break
    for url in candidates:
        if url.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            return _normalize_external_image_url(url)
    return _normalize_external_image_url(candidates[0]) if candidates else ""


def _fetch_stashbox_entity_meta(kind: str, title: str) -> dict:
    """Опциональный Stash-Box provider (ThePornDB/StashDB/etc.).

    Stash-Box — структурированная GraphQL-БД с performers/studios/images. Нужен
    API key, поэтому provider выключен по умолчанию и включается env-переменной.
    Если схема конкретного endpoint немного отличается, просто пропускаем его и
    продолжаем публичные VRSpy/POVR fallback'и.
    """
    providers = getattr(config, "STASHBOX_METADATA_PROVIDERS", []) or []
    if not providers:
        return {}
    slug = _slugify_povr(title)
    if not slug:
        return {}
    if kind == "actor":
        field, items_field, type_name = "queryPerformers", "performers", "Performer"
        extra = "aliases images { url } urls { url }"
    elif kind == "studio":
        field, items_field, type_name = "queryStudios", "studios", "Studio"
        extra = "aliases images { url } urls { url }"
    else:
        return {}
    query = f"""
    query {type_name}Search($name: String!) {{
      {field}(input: {{ name: $name }}) {{
        {items_field} {{
          id
          name
          {extra}
        }}
      }}
    }}
    """
    for provider in providers:
        endpoint = (provider.get("endpoint") or "").strip()
        if not endpoint:
            continue
        try:
            data = _external_post_json(
                endpoint,
                {"query": query, "variables": {"name": title}},
                headers=_stashbox_headers(provider.get("api_key") or ""),
            )
        except Exception as e:
            log.info("StashBox %s %r недоступен: %s", kind, title, e)
            continue
        if data.get("errors"):
            log.debug("StashBox %s %r вернул errors: %s", kind, title, data.get("errors"))
            continue
        rows = (((data.get("data") or {}).get(field) or {}).get(items_field) or [])
        best = None
        for row in rows:
            name = row.get("name") or ""
            aliases = row.get("aliases") or []
            slugs = {_slugify_povr(name), *(_slugify_povr(a) for a in aliases if isinstance(a, str))}
            if slug in slugs or any(_slugs_match(s, slug) for s in slugs):
                best = row
                break
        if not best and rows:
            best = rows[0]
        if not best:
            continue
        preview_url = _pick_stashbox_image(best.get("images"))
        if not preview_url:
            continue
        return {
            "preview_url": preview_url,
            "banner_url": "",
            "source_url": f"{endpoint.rstrip('/')}?{kind}={urllib.parse.quote(best.get('id') or slug, safe='')}",
            "trailer_url": "",
            "description": "",
        }
    return {}


def _fetch_vrspy_actor_directory_map() -> dict:
    key = "vrspy-stars"
    cached = _cached_directory_map(key)
    if cached is not None:
        return cached
    directory_url = urllib.parse.urljoin(config.VRSPY_BASE_URL.rstrip("/") + "/", "stars")
    html = _external_get(directory_url)
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return _set_directory_map(key, {})

    result = {}
    for link in soup.find_all("a", href=re.compile(r"/star/", re.I)):
        href = link.get("href") or ""
        detail_url = _first_abs_url(directory_url, href)
        slug = urllib.parse.urlsplit(detail_url).path.rstrip("/").rsplit("/", 1)[-1].lower()
        if not slug or slug in result:
            continue
        img = link.find("img")
        preview_url = ""
        if img:
            for src in _image_urls_from_tag(img, detail_url):
                if _is_vrspy_actor_image_url(slug.replace("-", " "), src):
                    preview_url = src
                    break
        if not preview_url:
            # Иногда картинка и ссылка в HTML не вложены друг в друга. Берём
            # первую CDN-картинку с тем же slug рядом в markup как best effort.
            m = re.search(
                rf"https?://cdn\.vrspy\.com/stars/{re.escape(slug)}/[^\"'<> )]+\.(?:jpg|jpeg|png|webp)(?:\?[^\"'<> )]+)?",
                str(link),
                re.I,
            )
            preview_url = _normalize_external_image_url(m.group(0)) if m else ""
        if preview_url:
            result[slug] = {
                "preview_url": preview_url,
                "banner_url": "",
                "source_url": detail_url,
                "trailer_url": "",
                "description": "",
            }
    return _set_directory_map(key, result)


def _fetch_povr_studio_directory_map() -> dict:
    key = "povr-studios"
    cached = _cached_directory_map(key)
    if cached is not None:
        return cached
    directory_url = urllib.parse.urljoin(config.POVR_BASE_URL.rstrip("/") + "/", "studios")
    html = _external_get(directory_url)
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return _set_directory_map(key, {})

    result = {}
    for link in soup.find_all("a", href=re.compile(r"/studios/", re.I)):
        href = link.get("href") or ""
        detail_url = _first_abs_url(directory_url, href)
        slug = urllib.parse.urlsplit(detail_url).path.rstrip("/").rsplit("/", 1)[-1].lower()
        if not slug:
            continue
        img = link.find("img")
        img_title = (img.get("alt") if img else "") or link.get_text(" ", strip=True)
        preview_url = ""
        if img:
            for src in _image_urls_from_tag(img, detail_url):
                if _is_povr_studio_logo_url(src):
                    preview_url = src
                    break
        if preview_url:
            meta = {
                "preview_url": preview_url,
                "banner_url": "",
                "source_url": detail_url,
                "trailer_url": "",
                "description": "",
            }
            result[slug] = meta
            title_slug = _slugify_povr(img_title)
            if title_slug:
                result[title_slug] = meta
    return _set_directory_map(key, result)


def _fetch_vrspy_actor_meta(title: str) -> dict:
    """Постер актёра с VRSpy: /star/<slug> содержит явный avatar.

    Этот источник оказался чище для актёрских фото, чем общий лендинг VRPorn,
    и даёт разные URL вида cdn.vrspy.com/stars/<slug>/...jpg.
    """
    slug = _slugify_povr(title)
    if not slug:
        return {}
    try:
        directory_meta = _fetch_vrspy_actor_directory_map().get(slug)
    except Exception as e:
        log.debug("Не удалось получить VRSpy stars directory: %s", e)
        directory_meta = None
    if directory_meta and directory_meta.get("preview_url"):
        return directory_meta

    source_url = urllib.parse.urljoin(config.VRSPY_BASE_URL.rstrip("/") + "/", f"star/{slug}")
    html = _external_get(source_url)
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        soup = None
    if not soup:
        return {}

    h1 = soup.find("h1")
    h1_slug = _slugify_povr(h1.get_text(" ", strip=True)) if h1 else ""

    preview_url = ""
    banner_url = ""
    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").casefold()
        for src in _image_urls_from_tag(img, source_url):
            src_low = urllib.parse.unquote(src.lower())
            if f"/stars/{slug}/" not in src_low:
                continue
            if "profile-banner" in src_low or "banner" in alt:
                banner_url = banner_url or src
                continue
            if "avatar" in alt or not preview_url:
                preview_url = src
                if "avatar" in alt:
                    break
        if preview_url and "avatar" in alt:
            break

    description = ""
    bio_heading = soup.find(string=re.compile(r"Bio|Career", re.I))
    if bio_heading:
        parent = getattr(bio_heading, "parent", None)
        if parent:
            for p in parent.find_all_next("p"):
                text = p.get_text(" ", strip=True)
                if len(text) > 80:
                    description = text[:1200]
                    break
    if not description:
        for ptag in soup.find_all("p"):
            text = ptag.get_text(" ", strip=True)
            if len(text) > 80:
                description = text[:1200]
                break

    if not preview_url and banner_url:
        preview_url = banner_url
    if h1_slug and slug not in h1_slug and (preview_url or banner_url):
        log.debug("VRSpy actor page title %r does not match %r, but image URL matched", h1_slug, slug)

    # Даже отрицательный результат кэшируем с VRSpy source_url: так старый POVR
    # кэш не будет возвращаться, а для отсутствующего актёра останется уникальный
    # локальный placeholder вместо одинакового чужого постера.
    return {
        "preview_url": preview_url,
        "banner_url": banner_url,
        "source_url": source_url,
        "trailer_url": "",
        "description": description,
    }


def _fetch_povr_studio_directory_meta(title: str) -> dict:
    slug = _slugify_povr(title)
    if not slug:
        return {}
    try:
        return _fetch_povr_studio_directory_map().get(slug) or {}
    except Exception as e:
        log.debug("Не удалось получить POVR studios directory: %s", e)
        return {}



def _fetch_povr_entity_meta(kind: str, title: str) -> dict:
    slug = _slugify_povr(title)
    if not slug:
        return {}
    if kind == "actor":
        path = f"/pornstars/{slug}"
        image_markers = ("/actors/", "/headshots/")
    elif kind == "studio":
        directory_meta = _fetch_povr_studio_directory_meta(title)
        if directory_meta.get("preview_url"):
            return directory_meta
        path = f"/studios/{slug}"
        image_markers = ("/assets/logos/channels/",)
    else:
        return {}

    source_url = urllib.parse.urljoin(config.POVR_BASE_URL.rstrip("/") + "/", path.lstrip("/"))
    html = _external_get(source_url)

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        soup = None

    preview_url = ""
    description = ""
    first_video_url = ""
    banner_url = ""
    if not soup:
        return {}

    h1 = soup.find("h1")
    h1_slug = _slugify_povr(h1.get_text(" ", strip=True)) if h1 else ""
    page_ok = _slugs_match(h1_slug, slug)
    canonical = soup.find("link", rel=lambda value: value and "canonical" in value)
    if canonical and canonical.get("href"):
        canonical_slug = urllib.parse.urlsplit(_first_abs_url(source_url, canonical["href"])).path.rstrip("/").rsplit("/", 1)[-1]
        page_ok = page_ok or _slugs_match(canonical_slug, slug)
    if not page_ok:
        return {}

    for img in soup.find_all("img"):
        for src_abs in _image_urls_from_tag(img, source_url):
            src_low = src_abs.lower()
            if kind == "studio":
                if _is_povr_studio_logo_url(src_abs):
                    preview_url = src_abs
                    break
            elif any(marker in src_low for marker in image_markers):
                preview_url = src_abs
                break
        if preview_url:
            break

    for p in soup.find_all("p"):
        text = p.get_text(" ", strip=True)
        if len(text) > 80:
            description = text[:1200]
            break
    first_video = soup.find("a", href=re.compile(r"/vr-porn/"))
    if first_video and first_video.get("href"):
        first_video_url = _first_abs_url(source_url, first_video["href"])
        img = first_video.find("img")
        if img:
            candidates = _image_urls_from_tag(img, source_url)
            banner_url = candidates[0] if candidates else ""

    preview_url = _normalize_external_image_url(preview_url)
    banner_url = _normalize_external_image_url(banner_url)

    trailer_url = _extract_povr_video_trailer(first_video_url) if first_video_url else ""
    return {
        "preview_url": preview_url,
        "banner_url": banner_url,
        "source_url": source_url,
        "trailer_url": trailer_url,
        "description": description,
    }


def _playa_refresh_meta_requested() -> bool:
    try:
        return _playa_arg("refresh") == "1" or _playa_arg("refresh-meta") == "1"
    except RuntimeError:
        return False


def _playa_entity_meta(kind: str, title: str, force_refresh: bool = False) -> dict:
    entity_id = _playa_entity_id(kind, title)
    cached = store.get_entity_meta(kind, entity_id)
    now = int(time.time())
    cache_current = _cached_entity_meta_is_current(kind, title, cached)
    force_refresh = force_refresh or _playa_refresh_meta_requested()
    if cached and cache_current and not force_refresh and (now - int(cached.get("updated_at") or 0)) < config.EXTERNAL_ENTITY_META_TTL_SEC:
        return cached
    if not config.ENABLE_EXTERNAL_ENTITY_META:
        return cached if cache_current else {}

    try:
        # Самый простой и точный путь: взять artwork из PLAY'A API VRPorn.com.
        # Этот сайт уже подключается в PLAY'A и отдаёт actors/studios с preview.
        meta = _fetch_vrporn_playa_entity_meta(kind, title)
    except Exception as e:
        log.info("Не удалось обновить VRPorn PLAY'A metadata %s %r: %s", kind, title, e)
        meta = {}

    if not meta.get("preview_url"):
        try:
            meta = _fetch_stashbox_entity_meta(kind, title)
        except Exception as e:
            log.info("Не удалось обновить StashBox-метаданные %s %r: %s", kind, title, e)
            meta = {}

    if not meta.get("preview_url"):
        if kind == "actor":
            try:
                meta = _fetch_vrspy_actor_meta(title)
            except Exception as e:
                log.info("Не удалось обновить VRSpy-метаданные actor %r: %s", title, e)
                meta = {}
        else:
            try:
                meta = _fetch_povr_entity_meta(kind, title)
            except Exception as e:
                log.info("Не удалось обновить внешние метаданные %s %r: %s", kind, title, e)
                meta = {}

    # Если обновление временно не удалось, не стираем уже хороший кэш. Но старый
    # неподходящий кэш (например, POVR actor или не-logo studio image) больше не
    # возвращаем, чтобы не показывать одинаковые/чужие постеры.
    if not any(meta.get(key) for key in ("preview_url", "banner_url", "source_url", "trailer_url", "description")):
        return cached if cache_current else {}

    # Кэшируем успешный результат; старые/пустые результаты не закрепляем,
    # чтобы повторный refresh мог подтянуть появившиеся картинки.
    store.upsert_entity_meta(
        kind,
        entity_id,
        title,
        preview_url=meta.get("preview_url", ""),
        banner_url=meta.get("banner_url", ""),
        source_url=_mark_entity_meta_source(meta.get("source_url", "")),
        trailer_url=meta.get("trailer_url", ""),
        description=meta.get("description", ""),
    )
    return store.get_entity_meta(kind, entity_id) or {}


def _playa_seed_peer_text(item: dict) -> str:
    seeders = item.get("seeders")
    leechers = item.get("leechers")
    if seeders is None and leechers is None:
        return "S:? P:?"
    s = seeders if seeders is not None else "?"
    p = leechers if leechers is not None else "?"
    return f"S:{s} P:{p}"


def _playa_status(item: dict) -> str:
    # Нативное избранное есть в PLAY'A, поэтому свой status/badge не навязываем.
    return ""


def _playa_studio_line(item: dict) -> str:
    studios = [x for x in item.get("studio", []) if x]
    if studios:
        return ", ".join(studios[:3])
    site_id = item.get("site") or "pornolab"
    return _site_label(site_id) or site_id


def _playa_actor_line(item: dict) -> str:
    performers = [x for x in item.get("performers", []) if x]
    if performers:
        return ", ".join(performers[:4])
    return item.get("title") or ""


def _playa_people_line(item: dict) -> str:
    studios = [x for x in item.get("studio", []) if x]
    performers = [x for x in item.get("performers", []) if x]
    if studios and performers:
        return f"{', '.join(studios[:2])} · {', '.join(performers[:3])}"
    if studios:
        return ", ".join(studios[:3])
    if performers:
        return ", ".join(performers[:3])
    site_id = item.get("site") or "pornolab"
    return _site_label(site_id) or site_id


def _playa_added_date(item: dict) -> str:
    ts = int(item.get("created_at") or item.get("updated_at") or 0)
    if not ts:
        return "??.??.??"
    return time.strftime("%d.%m.%y", time.localtime(ts))


def _playa_info_line(item: dict) -> str:
    # PLAY'A не поддерживает HTML/CSS в subtitle, поэтому цвет кодируем
    # компактными emoji-индикаторами. Формат короче, чем YYYY-MM-DD.
    seeders = item.get("seeders")
    leechers = item.get("leechers")
    s = seeders if seeders is not None else "?"
    p = leechers if leechers is not None else "?"
    return f"🟢S{s} 🔴P{p} · {_playa_quality_name(item)} · {_playa_added_date(item)}"


def _playa_subtitle(item: dict) -> str:
    return _playa_actor_line(item)


def _playa_quality_name(item: dict) -> str:
    height = _deovr_resolution(item)
    return f"{height}p" if height else "1080p"


def _playa_quality_order(item: dict) -> int:
    height = _deovr_resolution(item)
    if height >= 7680:
        return 85
    if height >= 6720:
        return 75
    if height >= 5760:
        return 65
    if height >= 4800:
        return 55
    if height >= 3840:
        return 45
    if height >= 2880:
        return 35
    return 25


def _playa_video_link(item: dict, base_url: str) -> dict:
    item_id = str(item["id"])
    return {
        "is_stream": True,
        "is_download": True,
        "url": f"{base_url}/stream/{urllib.parse.quote(item_id, safe='')}",
        "projection": _PLAYA_PROJECTION.get((item.get("screen_type") or "").lower(), "180"),
        "stereo": _PLAYA_STEREO.get((item.get("stereo_mode") or "").lower(), "LR"),
        "quality_name": _playa_quality_name(item),
        "quality_order": _playa_quality_order(item),
    }


def _playa_list_view(item: dict, base_url: str) -> dict:
    return {
        "id": str(item["id"]),
        "title": _playa_studio_line(item),
        "subtitle": _playa_subtitle(item),
        "status": _playa_status(item),
        # PLAY'A API v2 has no documented CSS/hover field for list cards. Keep the
        # seed/peer/resolution/date string off the visible card and expose it as
        # auxiliary metadata for clients that tolerate/read non-standard fields.
        "tooltip": _playa_info_line(item),
        "description": _playa_info_line(item),
        "preview_image": _image_url(item, base_url),
        "has_scripts": False,
        "release_date": int(item.get("created_at") or item.get("updated_at") or 0),
        "details": [
            {
                "type": "full",
                "duration_seconds": int(item.get("video_length") or 0),
                "has_scripts": False,
                "transparency_mode": 0,
            }
        ],
    }


def _playa_description(item: dict) -> str:
    lines = []
    if item.get("studio"):
        lines.append("Studios: " + ", ".join(item["studio"]))
    if item.get("performers"):
        lines.append("Actors: " + ", ".join(item["performers"]))
    if item.get("tags"):
        lines.append("Categories: " + ", ".join(item["tags"]))
    lines.append("Peers: " + _playa_seed_peer_text(item))
    if item.get("video_height"):
        lines.append(f"Resolution: {item['video_height']}p")
    lines.append(f"Source: {item.get('site') or 'pornolab'} / {item.get('topic_id') or item.get('id')}")
    return "\n".join(lines)


def _playa_studio_ref(item: dict):
    studios = [title for title in item.get("studio", []) if title]
    if not studios:
        return None
    title = studios[0]
    return {"id": _playa_entity_id("studio", title), "title": title}


def _playa_video_view(item: dict, base_url: str) -> dict:
    item_id = str(item["id"])
    return {
        "id": item_id,
        "title": item["title"],
        "subtitle": _playa_subtitle(item),
        "status": _playa_status(item),
        "description": _playa_description(item),
        "preview_image": _image_url(item, base_url),
        "release_date": int(item.get("created_at") or item.get("updated_at") or 0),
        # В референсной реализации PLAY'A API v2 поле VideoView.studio - ОДИН
        # объект, не массив. Если отдать массив, карточка открывается, но клиент
        # может не распарсить detail-модель и не дойти до ссылки воспроизведения.
        "studio": _playa_studio_ref(item),
        "categories": [
            {"id": _playa_entity_id("category", title), "title": title}
            for title in item.get("tags", [])
        ],
        "actors": [
            {"id": _playa_entity_id("actor", title), "title": title}
            for title in item.get("performers", [])
        ],
        "views": 0,
        "details": [
            {
                "type": "full",
                "duration_seconds": int(item.get("video_length") or 0),
                "links": [_playa_video_link(item, base_url)],
            }
        ],
    }


def _playa_page_args(default_size: int = 60) -> tuple:
    try:
        page_index = int(_playa_arg("page-index", 0))
    except (TypeError, ValueError):
        page_index = 0
    try:
        page_size = int(_playa_arg("page-size", default_size))
    except (TypeError, ValueError):
        page_size = default_size
    page_index = max(page_index, 0)
    page_size = min(max(page_size, 1), 500)
    return page_index, page_size


def _playa_page(items: list, page_index: int, page_size: int) -> dict:
    total = len(items)
    page_total = max(1, math.ceil(total / page_size))
    start = page_index * page_size
    end = start + page_size
    return {
        "page_index": page_index,
        "page_size": page_size,
        "page_total": page_total,
        "item_total": total,
        "content": items[start:end] if start < total else [],
    }


def _csv_ids(value: str) -> list:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _playa_entity_map(kind: str, values: list) -> dict:
    return {_playa_entity_id(kind, value): value for value in values if value}


def _playa_collect_entities(items: list) -> dict:
    tags = sorted({tag for item in items for tag in item.get("tags", []) if tag}, key=str.casefold)
    actors = sorted({actor for item in items for actor in item.get("performers", []) if actor}, key=str.casefold)
    studios = sorted({studio for item in items for studio in item.get("studio", []) if studio}, key=str.casefold)
    return {
        "categories": _playa_entity_map("category", tags),
        "actors": _playa_entity_map("actor", actors),
        "studios": _playa_entity_map("studio", studios),
    }


def _playa_filter_videos(items: list) -> list:
    maps = _playa_collect_entities(items)

    title = (_playa_arg("title") or "").strip().casefold()
    actor_id = (_playa_arg("actor") or "").strip()
    studio_id = (_playa_arg("studio") or "").strip()
    included_categories = _csv_ids(_playa_arg("included-categories") or "")
    excluded_categories = _csv_ids(_playa_arg("excluded-categories") or "")

    actor_name = maps["actors"].get(actor_id) if actor_id else None
    studio_name = maps["studios"].get(studio_id) if studio_id else None
    included_names = [maps["categories"].get(cid) for cid in included_categories]
    excluded_names = [maps["categories"].get(cid) for cid in excluded_categories]

    if (actor_id and not actor_name) or (studio_id and not studio_name) or any(name is None for name in included_names):
        return []

    filtered = []
    for item in items:
        haystack = " ".join(
            [item.get("title") or ""]
            + item.get("tags", [])
            + item.get("studio", [])
            + item.get("performers", [])
        ).casefold()
        if title and title not in haystack:
            continue
        if actor_name and actor_name not in item.get("performers", []):
            continue
        if studio_name and studio_name not in item.get("studio", []):
            continue
        item_tags = set(item.get("tags", []))
        if any(name not in item_tags for name in included_names if name):
            continue
        if any(name in item_tags for name in excluded_names if name):
            continue
        filtered.append(item)

    order = (_playa_arg("order") or "").strip().casefold()
    direction = (_playa_arg("direction", "asc") or "asc").strip().casefold()
    reverse = direction == "desc"
    if order == "title":
        filtered.sort(key=lambda x: (_playa_people_line(x) or x.get("title") or "").casefold(), reverse=reverse)
    elif order == "release_date":
        filtered.sort(key=lambda x: int(x.get("created_at") or x.get("updated_at") or 0), reverse=reverse)
    elif order == "popularity":
        filtered.sort(key=lambda x: int(x.get("seeders") or 0), reverse=reverse)
    return filtered


_ENTITY_META_REFRESHING = set()
_ENTITY_META_REFRESH_LOCK = threading.Lock()


def _playa_cached_entity_meta(kind: str, title: str) -> dict:
    cached = store.get_entity_meta(kind, _playa_entity_id(kind, title))
    return cached if _cached_entity_meta_is_current(kind, title, cached) else {}


def _playa_apply_entity_meta_to_entry(kind: str, entry: dict, meta: dict) -> dict:
    if meta.get("preview_url"):
        entry["preview"] = _normalize_external_image_url(meta["preview_url"])
    if meta.get("banner_url"):
        entry["banner"] = _normalize_external_image_url(meta["banner_url"])
    entry["description"] = meta.get("description") or entry.get("description") or ""
    entry["source_url"] = meta.get("source_url") or entry.get("source_url") or ""
    entry["trailer_url"] = meta.get("trailer_url") or entry.get("trailer_url") or ""
    return entry


def _playa_schedule_entity_meta_refresh(kind: str, entries: list, force: bool = False):
    if not config.ENABLE_EXTERNAL_ENTITY_META:
        return
    titles = []
    seen = set()
    limit = len(entries) if force else 60
    for entry in entries[:limit]:
        title = entry.get("title") or ""
        if not title or title in seen:
            continue
        seen.add(title)
        if force or not _playa_cached_entity_meta(kind, title):
            titles.append(title)
    if not titles:
        return
    key = (kind, hashlib.sha1("\n".join(sorted(titles)).encode("utf-8")).hexdigest()[:16])
    with _ENTITY_META_REFRESH_LOCK:
        if key in _ENTITY_META_REFRESHING:
            return
        _ENTITY_META_REFRESHING.add(key)

    def worker():
        try:
            for title in titles:
                try:
                    _playa_entity_meta(kind, title, force_refresh=True)
                except Exception as e:
                    log.info("Фоновое обновление metadata %s %r не удалось: %s", kind, title, e)
        finally:
            with _ENTITY_META_REFRESH_LOCK:
                _ENTITY_META_REFRESHING.discard(key)

    threading.Thread(target=worker, name=f"playa-{kind}-meta-refresh", daemon=True).start()


def _playa_apply_cached_entity_entry(kind: str, entry: dict) -> dict:
    meta = _playa_cached_entity_meta(kind, entry["title"])
    if meta:
        _playa_apply_entity_meta_to_entry(kind, entry, meta)
    return entry


def _entity_list_name() -> str:
    return config.ENTITY_LIST_DEFAULT_NAME or "watchlist"


def _valid_entity_list_kind(kind: str) -> bool:
    return kind in {"actor", "studio"}


def _entity_title_from_id(kind: str, entity_id: str) -> str:
    field = "performers" if kind == "actor" else "studio"
    for item in _library_items():
        for title in item.get(field, []):
            if _playa_entity_id(kind, title) == entity_id:
                return title
    return entity_id


def _entity_list_toggle_url(base_url: str, kind: str, entity_id: str, title: str) -> str:
    return (
        f"{base_url}/entity-list/{urllib.parse.quote(kind, safe='')}/"
        f"{urllib.parse.quote(entity_id, safe='')}/toggle?title={urllib.parse.quote(title or entity_id, safe='')}"
    )


def _playa_enrich_entity_entry(kind: str, entry: dict, base_url: str = None) -> dict:
    if entry.get("_enriched"):
        return entry
    meta = _playa_entity_meta(kind, entry["title"])
    _playa_apply_entity_meta_to_entry(kind, entry, meta)
    entry["_enriched"] = True
    return entry


def _playa_entity_entries(kind: str, items: list, field: str, base_url: str, enrich: bool = False) -> list:
    by_title = {}
    listed_ids = store.entity_list_ids(_entity_list_name(), kind) if _valid_entity_list_kind(kind) else set()
    for item in items:
        for title in item.get(field, []):
            if not title:
                continue
            entity_id = _playa_entity_id(kind, title)
            entry = by_title.setdefault(
                title,
                {
                    "id": entity_id,
                    "title": title,
                    "preview": _entity_placeholder_url(base_url, kind, entity_id, title),
                    "banner": "",
                    "description": "",
                    "source_url": "",
                    "trailer_url": "",
                    "count": 0,
                    "studios": set(),
                    "in_list": entity_id in listed_ids,
                },
            )
            entry["in_list"] = entry["id"] in listed_ids
            entry["count"] += 1
            entry["studios"].update(item.get("studio", []))

    entries = list(by_title.values())
    title_filter = (_playa_arg("title") or "").strip().casefold()
    if title_filter:
        entries = [entry for entry in entries if title_filter in entry["title"].casefold()]
    order = (_playa_arg("order", "title") or "title").strip().casefold()
    direction = (_playa_arg("direction", "asc") or "asc").strip().casefold()
    reverse = direction == "desc"
    if order == "popularity":
        entries.sort(key=lambda x: (x["count"], x["title"].casefold()), reverse=reverse)
    else:
        entries.sort(key=lambda x: x["title"].casefold(), reverse=reverse)
    # Локальный actor/studio list: держим отмеченные сущности сверху, не меняя id.
    entries.sort(key=lambda x: not x.get("in_list", False))
    if enrich:
        for entry in entries:
            _playa_enrich_entity_entry(kind, entry, base_url)
    return entries


def _playa_category_entries(items: list, base_url: str) -> list:
    by_title = {}
    for item in items:
        for title in item.get("tags", []):
            if not title:
                continue
            by_title.setdefault(
                title,
                {
                    "id": _playa_entity_id("category", title),
                    "title": title,
                    "preview": _image_url(item, base_url),
                },
            )
    return sorted(by_title.values(), key=lambda x: x["title"].casefold())


@app.get(f"{_PLAYA_PREFIX}/version")
def playa_version():
    return _playa_response("1.10.1")


@app.get(f"{_PLAYA_PREFIX}/config")
def playa_config():
    return _playa_response({
        "site_name": "deovr-bridge",
        "site_logo": _placeholder_url(_base_url()),
        "auth": False,
        "auth_by_code": False,
        "actors": True,
        "categories": True,
        "categories_groups": True,
        "studios": True,
        "scripts": False,
        "masks": False,
        "analytics": False,
        "nsfw": True,
        "ar": False,
    })


@app.get(f"{_PLAYA_PREFIX}/videos")
def playa_videos():
    base_url = _base_url()
    items = _playa_filter_videos(_library_items())
    page_index, page_size = _playa_page_args()
    content = [_playa_list_view(item, base_url) for item in items]
    return _playa_response(_playa_page(content, page_index, page_size))


@app.get(f"{_PLAYA_PREFIX}/video/<item_id>")
def playa_video_detail(item_id):
    item = _get_library_item(item_id)
    if not item:
        return _playa_not_found("Video not found")
    return _playa_response(_playa_video_view(item, _base_url()))


def _playa_entity_display_title(entry: dict) -> str:
    return f"★ {entry['title']}" if entry.get("in_list") else entry["title"]


def _playa_entity_properties(entry: dict) -> list:
    # ActorView/StudioView properties in PLAY'A are plain text, not clickable
    # links. Поэтому управление списком делаем через web UI /entity-lists, а не
    # через некликабельные URL в карточке.
    props = [{"name": "Videos", "value": str(int(entry.get("count") or 0))}]
    props.append({"name": "Local list", "value": "yes" if entry.get("in_list") else "no"})
    return props


@app.get(f"{_PLAYA_PREFIX}/actors")
def playa_actors():
    base_url = _base_url()
    entries = _playa_entity_entries("actor", _library_items(), "performers", base_url)
    page_index, page_size = _playa_page_args()
    page = _playa_page(entries, page_index, page_size)
    _playa_schedule_entity_meta_refresh("actor", page["content"], force=_playa_refresh_meta_requested())
    page["content"] = [
        {"id": e["id"], "title": _playa_entity_display_title(e), "preview": _playa_apply_cached_entity_entry("actor", e)["preview"]}
        for e in page["content"]
    ]
    return _playa_response(page)


@app.get(f"{_PLAYA_PREFIX}/actor/<actor_id>")
def playa_actor_detail(actor_id):
    base_url = _base_url()
    entries = _playa_entity_entries("actor", _library_items(), "performers", base_url)
    entry = next((e for e in entries if e["id"] == actor_id), None)
    if not entry:
        return _playa_not_found("Actor not found")
    entry = _playa_enrich_entity_entry("actor", entry, base_url)
    return _playa_response({
        "id": entry["id"],
        "title": _playa_entity_display_title(entry),
        "preview": entry["preview"],
        "banner": entry.get("banner") or None,
        "studios": [
            {"id": _playa_entity_id("studio", title), "title": title}
            for title in sorted(entry["studios"], key=str.casefold)
        ],
        "properties": _playa_entity_properties(entry),
        "aliases": [],
        "views": int(entry["count"]),
    })


@app.get(f"{_PLAYA_PREFIX}/studios")
def playa_studios():
    base_url = _base_url()
    entries = _playa_entity_entries("studio", _library_items(), "studio", base_url)
    page_index, page_size = _playa_page_args()
    page = _playa_page(entries, page_index, page_size)
    _playa_schedule_entity_meta_refresh("studio", page["content"], force=_playa_refresh_meta_requested())
    page["content"] = [
        {"id": e["id"], "title": _playa_entity_display_title(e), "preview": _playa_apply_cached_entity_entry("studio", e)["preview"]}
        for e in page["content"]
    ]
    return _playa_response(page)


@app.get(f"{_PLAYA_PREFIX}/studio/<studio_id>")
def playa_studio_detail(studio_id):
    base_url = _base_url()
    entries = _playa_entity_entries("studio", _library_items(), "studio", base_url)
    entry = next((e for e in entries if e["id"] == studio_id), None)
    if not entry:
        return _playa_not_found("Studio not found")
    entry = _playa_enrich_entity_entry("studio", entry, base_url)
    description = entry.get("description") or f"{entry['count']} video(s) in local deovr-bridge library."
    return _playa_response({
        "id": entry["id"],
        "title": _playa_entity_display_title(entry),
        "preview": entry["preview"],
        "description": description,
        "views": int(entry["count"]),
    })


@app.get(f"{_PLAYA_PREFIX}/categories")
@app.get(f"{_PLAYA_PREFIX}/categories;")
def playa_categories():
    return _playa_response(_playa_category_entries(_library_items(), _base_url()))


@app.get(f"{_PLAYA_PREFIX}/categories-groups")
@app.get(f"{_PLAYA_PREFIX}/categories-groups;")
def playa_categories_groups():
    categories = _playa_category_entries(_library_items(), _base_url())
    return _playa_response([{"id": "tags", "title": "Tags", "items": categories}])


@app.get(f"{_PLAYA_PREFIX}/video-statuses")
@app.get(f"{_PLAYA_PREFIX}/video-statuses;")
def playa_video_statuses():
    return _playa_response([])


@app.route(f"{_PLAYA_PREFIX}/event", methods=["PUT", "POST"])
def playa_event():
    # Пока статистику не ведём: PLAY'A получает валидный ответ, а мы ничего не пишем.
    return _playa_response()



def _entity_list_payload() -> dict:
    return {"list_name": _entity_list_name(), "items": store.all_entity_list_entries(_entity_list_name())}


@app.get("/entity-lists.json")
def entity_lists_json():
    return jsonify(_entity_list_payload())


@app.route("/entity-list/<kind>/<entity_id>", methods=["GET", "POST", "PUT", "DELETE"])
def entity_list_item(kind, entity_id):
    if not _valid_entity_list_kind(kind):
        return jsonify({"error": "kind must be actor or studio"}), 400
    entity_id = urllib.parse.unquote(entity_id)
    title = request.values.get("title") or _entity_title_from_id(kind, entity_id)
    if request.method == "DELETE":
        enabled = False
        store.set_entity_in_list(_entity_list_name(), kind, entity_id, title, False)
    else:
        enabled = True
        store.set_entity_in_list(_entity_list_name(), kind, entity_id, title, True)
    if "text/html" in (request.headers.get("Accept") or ""):
        return redirect(request.referrer or "/entity-lists")
    return jsonify({"list_name": _entity_list_name(), "kind": kind, "id": entity_id, "title": title, "listed": enabled})


@app.get("/entity-list/<kind>/<entity_id>/toggle")
def entity_list_toggle(kind, entity_id):
    if not _valid_entity_list_kind(kind):
        return jsonify({"error": "kind must be actor or studio"}), 400
    entity_id = urllib.parse.unquote(entity_id)
    title = request.args.get("title") or _entity_title_from_id(kind, entity_id)
    enabled = store.toggle_entity_in_list(_entity_list_name(), kind, entity_id, title)
    if "text/html" in (request.headers.get("Accept") or ""):
        return redirect(request.referrer or "/entity-lists")
    return jsonify({"list_name": _entity_list_name(), "kind": kind, "id": entity_id, "title": title, "listed": enabled})


def _entity_list_html_rows(kind: str, entries: list, base_url: str) -> str:
    rows = []
    for entry in entries:
        listed = bool(entry.get("in_list"))
        title = html.escape(entry["title"])
        count = int(entry.get("count") or 0)
        mark = "★" if listed else "☆"
        action = "Убрать" if listed else "Добавить"
        cls = "listed" if listed else ""
        url = html.escape(_entity_list_toggle_url(base_url, kind, entry["id"], entry["title"]), quote=True)
        rows.append(
            f'<tr class="{cls}"><td class="star">{mark}</td><td>{title}</td>'
            f'<td class="count">{count}</td><td><a href="{url}">{action}</a></td></tr>'
        )
    return "\n".join(rows) or '<tr><td colspan="4">Пока пусто</td></tr>'


@app.get("/entity-lists")
def entity_lists_page():
    base_url = _base_url()
    items = _library_items()
    actors = _playa_entity_entries("actor", items, "performers", base_url)
    studios = _playa_entity_entries("studio", items, "studio", base_url)
    q = (request.args.get("q") or "").strip().casefold()
    if q:
        actors = [e for e in actors if q in e["title"].casefold()]
        studios = [e for e in studios if q in e["title"].casefold()]
    actor_rows = _entity_list_html_rows("actor", actors, base_url)
    studio_rows = _entity_list_html_rows("studio", studios, base_url)
    query_value = html.escape(request.args.get("q") or "", quote=True)
    page = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>deovr-bridge entity lists</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#111;color:#eee;margin:24px;}}
a{{color:#8bd3ff}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:24px}}
table{{width:100%;border-collapse:collapse;background:#1b1b1b;border-radius:12px;overflow:hidden}}
th,td{{padding:10px 12px;border-bottom:1px solid #2a2a2a;text-align:left}} th{{background:#242424}}
.star{{width:36px;color:#ffd35a;font-size:20px}} .count{{width:80px;color:#aaa}} tr.listed{{background:#24210f}}
input{{padding:9px 12px;border-radius:8px;border:1px solid #444;background:#1b1b1b;color:#eee;min-width:280px}}
button{{padding:9px 14px;border-radius:8px;border:0;background:#3662ff;color:white}}
.help{{color:#bbb;max-width:960px;line-height:1.45}}
</style></head><body>
<h1>Локальный список актёров/студий</h1>
<p class="help">PLAY'A не даёт documented write endpoint для избранного актёров/студий. Поэтому список хранится локально в deovr-bridge: отмеченные элементы поднимаются вверх и помечаются ★ в PLAY'A Actors/Studios. Нативное избранное видео PLAY'A не трогаем.</p>
<form method="get"><input name="q" placeholder="Фильтр по имени" value="{query_value}"> <button>Найти</button> <a href="/entity-lists">Сброс</a></form>
<div class="grid">
<section><h2>Актёры</h2><table><thead><tr><th></th><th>Имя</th><th>Видео</th><th></th></tr></thead><tbody>{actor_rows}</tbody></table></section>
<section><h2>Студии</h2><table><thead><tr><th></th><th>Название</th><th>Видео</th><th></th></tr></thead><tbody>{studio_rows}</tbody></table></section>
</div></body></html>"""
    return Response(page, mimetype="text/html; charset=utf-8")



@app.get("/debug/playa/video/<item_id>")
def debug_playa_video(item_id):
    item = _get_library_item(item_id)
    if not item:
        return jsonify({"error": "not found"}), 404
    return jsonify(_playa_video_view(item, _base_url()))


@app.get("/debug/playa/entities")
def debug_playa_entities():
    base_url = _base_url()
    items = _library_items()
    return jsonify({
        "actors": _playa_entity_entries("actor", items, "performers", base_url, enrich=(_playa_arg("enrich") == "1")),
        "studios": _playa_entity_entries("studio", items, "studio", base_url, enrich=(_playa_arg("enrich") == "1")),
    })


@app.get("/thumb/<item_id>")
def thumb_proxy(item_id):
    item = _get_library_item(item_id)
    if not item or not item["thumbnail_url"]:
        return "", 404
    site_id = item.get("site") or "pornolab"
    r = scraper.get_forum_session(site_id).get(item["thumbnail_url"], timeout=15)
    return Response(r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"))


def _background_refresh_loop():
    while True:
        try:
            scraper.refresh_all()
        except Exception:
            log.exception("Фоновое обновление упало")
        time.sleep(max(config.REFRESH_INTERVAL_SEC, 60))


if __name__ == "__main__":
    if config.REFRESH_INTERVAL_SEC > 0:
        threading.Thread(target=_background_refresh_loop, daemon=True).start()
    app.run(host=config.BRIDGE_HOST, port=config.BRIDGE_PORT, debug=False, threaded=True)
