
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
import logging
import math
import re
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
_PLAYA_FAV_VIDEO_CATEGORY_ID = "__favorites_videos__"
_PLAYA_FAV_ACTOR_CATEGORY_ID = "__favorites_actors__"
_PLAYA_FAV_STUDIO_CATEGORY_ID = "__favorites_studios__"


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


def _extract_povr_video_trailer(video_url: str) -> str:
    """Пробует достать прямой mp4 трейлера со страницы видео POVR.

    На страницах роликов POVR публично видны ссылки вида
    /download/<id>/trailer/ovr1920stream.mp4. В PLAY'A ActorView/StudioView
    отдельного поля trailer нет, поэтому сейчас используем это только как
    диагностическую/служебную ссылку в entity_meta и debug/favorites API.
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


def _fetch_povr_entity_meta(kind: str, title: str) -> dict:
    slug = _slugify_povr(title)
    if not slug:
        return {}
    if kind == "actor":
        path = f"/pornstars/{slug}"
        image_markers = ("/actors/", "/headshots/")
    elif kind == "studio":
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
    if soup:
        h1 = soup.find("h1")
        # Если нас редиректнуло/отдало не ту страницу, не кэшируем чужую картинку.
        if h1 and title and h1.get_text(" ", strip=True).casefold() != title.casefold():
            if _slugify_povr(h1.get_text(" ", strip=True)) != slug:
                return {}
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-original") or ""
            if not src:
                continue
            src_abs = _first_abs_url(source_url, src)
            src_low = src_abs.lower()
            if any(marker in src_low for marker in image_markers):
                preview_url = src_abs
                break
        if not preview_url and kind == "actor":
            m = re.search(r"https?://[^\"'<> ]+/wvr/actors/[^\"'<> ]+\.(?:jpg|png|webp)", html, re.I)
            preview_url = m.group(0) if m else ""
        if not preview_url and kind == "studio":
            m = re.search(r"https?://[^\"'<> ]+/assets/logos/channels/[^\"'<> ]+\.(?:svg|png|jpg|webp)", html, re.I)
            preview_url = m.group(0) if m else ""
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
                banner_url = _first_abs_url(source_url, img.get("src") or img.get("data-src") or "")
    else:
        m = re.search(r"https?://[^\"'<> ]+/(?:wvr/actors|headshots|assets/logos/channels)/[^\"'<> ]+\.(?:jpg|png|webp|svg)", html, re.I)
        preview_url = m.group(0) if m else ""

    trailer_url = _extract_povr_video_trailer(first_video_url) if first_video_url else ""
    return {
        "preview_url": preview_url,
        "banner_url": banner_url,
        "source_url": source_url,
        "trailer_url": trailer_url,
        "description": description,
    }


def _playa_entity_meta(kind: str, title: str) -> dict:
    entity_id = _playa_entity_id(kind, title)
    cached = store.get_entity_meta(kind, entity_id)
    now = int(time.time())
    if cached and (now - int(cached.get("updated_at") or 0)) < config.EXTERNAL_ENTITY_META_TTL_SEC:
        return cached
    if not config.ENABLE_EXTERNAL_ENTITY_META:
        return cached or {}
    try:
        meta = _fetch_povr_entity_meta(kind, title)
    except Exception as e:
        log.info("Не удалось обновить внешние метаданные %s %r: %s", kind, title, e)
        meta = {}
    # Кэшируем и отрицательный результат, чтобы не стучаться во внешний сайт на
    # каждый запрос PLAY'A, если артиста/студию там не нашли.
    store.upsert_entity_meta(
        kind,
        entity_id,
        title,
        preview_url=meta.get("preview_url", ""),
        banner_url=meta.get("banner_url", ""),
        source_url=meta.get("source_url", ""),
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


def _playa_video_status_id(item: dict) -> str:
    return "favorite" if store.is_favorite("video", str(item["id"])) else "normal"


def _playa_status(item: dict) -> str:
    # Поле status PLAY'A показывает бейдж рядом с названием. Сиды/пиры теперь
    # уводим во вторую строку subtitle, а status оставляем под избранное.
    return "★ Favorite" if _playa_video_status_id(item) == "favorite" else ""


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
        return "date:?"
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _playa_info_line(item: dict) -> str:
    return f"{_playa_seed_peer_text(item)} · {_playa_quality_name(item)} · {_playa_added_date(item)}"


def _playa_subtitle(item: dict) -> str:
    # PLAY'A отображает subtitle под названием карточки. Даём две строки:
    # 1) студия + актёры, 2) сиды/пиры + разрешение + дата добавления.
    return f"{_playa_people_line(item)}\n{_playa_info_line(item)}"


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
        "title": item["title"],
        "subtitle": _playa_subtitle(item),
        "status": _playa_status(item),
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
    included_statuses = _csv_ids(_playa_arg("included-statuses") or "")
    excluded_statuses = _csv_ids(_playa_arg("excluded-statuses") or "")

    fav_video_ids = store.favorite_ids("video")
    fav_actor_ids = store.favorite_ids("actor")
    fav_studio_ids = store.favorite_ids("studio")

    include_fav_videos = _PLAYA_FAV_VIDEO_CATEGORY_ID in included_categories
    include_fav_actors = _PLAYA_FAV_ACTOR_CATEGORY_ID in included_categories
    include_fav_studios = _PLAYA_FAV_STUDIO_CATEGORY_ID in included_categories
    exclude_fav_videos = _PLAYA_FAV_VIDEO_CATEGORY_ID in excluded_categories
    exclude_fav_actors = _PLAYA_FAV_ACTOR_CATEGORY_ID in excluded_categories
    exclude_fav_studios = _PLAYA_FAV_STUDIO_CATEGORY_ID in excluded_categories

    included_categories = [
        cid for cid in included_categories
        if cid not in {_PLAYA_FAV_VIDEO_CATEGORY_ID, _PLAYA_FAV_ACTOR_CATEGORY_ID, _PLAYA_FAV_STUDIO_CATEGORY_ID}
    ]
    excluded_categories = [
        cid for cid in excluded_categories
        if cid not in {_PLAYA_FAV_VIDEO_CATEGORY_ID, _PLAYA_FAV_ACTOR_CATEGORY_ID, _PLAYA_FAV_STUDIO_CATEGORY_ID}
    ]

    actor_name = maps["actors"].get(actor_id) if actor_id else None
    studio_name = maps["studios"].get(studio_id) if studio_id else None
    included_names = [maps["categories"].get(cid) for cid in included_categories]
    excluded_names = [maps["categories"].get(cid) for cid in excluded_categories]

    if (actor_id and not actor_name) or (studio_id and not studio_name) or any(name is None for name in included_names):
        return []

    included_statuses = {s.casefold() for s in included_statuses}
    excluded_statuses = {s.casefold() for s in excluded_statuses}

    filtered = []
    for item in items:
        item_id = str(item["id"])
        actor_ids = {_playa_entity_id("actor", title) for title in item.get("performers", [])}
        studio_ids = {_playa_entity_id("studio", title) for title in item.get("studio", [])}
        is_video_fav = item_id in fav_video_ids
        has_fav_actor = bool(actor_ids & fav_actor_ids)
        has_fav_studio = bool(studio_ids & fav_studio_ids)
        status_id = "favorite" if is_video_fav else "normal"

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

        if include_fav_videos and not is_video_fav:
            continue
        if include_fav_actors and not has_fav_actor:
            continue
        if include_fav_studios and not has_fav_studio:
            continue
        if exclude_fav_videos and is_video_fav:
            continue
        if exclude_fav_actors and has_fav_actor:
            continue
        if exclude_fav_studios and has_fav_studio:
            continue

        if included_statuses and status_id not in included_statuses:
            continue
        if excluded_statuses and status_id in excluded_statuses:
            continue
        filtered.append(item)

    order = (_playa_arg("order") or "").strip().casefold()
    direction = (_playa_arg("direction", "asc") or "asc").strip().casefold()
    reverse = direction == "desc"
    if order == "title":
        filtered.sort(key=lambda x: (x.get("title") or "").casefold(), reverse=reverse)
    elif order == "release_date":
        filtered.sort(key=lambda x: int(x.get("created_at") or x.get("updated_at") or 0), reverse=reverse)
    elif order == "popularity":
        filtered.sort(key=lambda x: int(x.get("seeders") or 0), reverse=reverse)
    return filtered

def _playa_enrich_entity_entry(kind: str, entry: dict) -> dict:
    if entry.get("_enriched"):
        return entry
    meta = _playa_entity_meta(kind, entry["title"])
    if meta.get("preview_url"):
        entry["preview"] = meta["preview_url"]
    entry["banner"] = meta.get("banner_url") or ""
    entry["description"] = meta.get("description") or ""
    entry["source_url"] = meta.get("source_url") or ""
    entry["trailer_url"] = meta.get("trailer_url") or ""
    entry["_enriched"] = True
    return entry


def _playa_entity_entries(kind: str, items: list, field: str, base_url: str, enrich: bool = False) -> list:
    by_title = {}
    fav_ids = store.favorite_ids(kind)
    fav_video_ids = store.favorite_ids("video")
    for item in items:
        for title in item.get(field, []):
            if not title:
                continue
            entry = by_title.setdefault(
                title,
                {
                    "id": _playa_entity_id(kind, title),
                    "title": title,
                    "preview": _image_url(item, base_url),
                    "banner": "",
                    "description": "",
                    "source_url": "",
                    "trailer_url": "",
                    "count": 0,
                    "favorite_video_count": 0,
                    "studios": set(),
                },
            )
            entry["count"] += 1
            if str(item["id"]) in fav_video_ids:
                entry["favorite_video_count"] += 1
            entry["studios"].update(item.get("studio", []))

    entries = list(by_title.values())
    for entry in entries:
        entry["is_favorite"] = entry["id"] in fav_ids

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
    # Избранные актёры/студии держим вверху левой панели, не меняя id.
    entries.sort(key=lambda x: not x.get("is_favorite", False))
    if enrich:
        for entry in entries:
            _playa_enrich_entity_entry(kind, entry)
    return entries

def _playa_category_entries(items: list, base_url: str, include_favorites: bool = True) -> list:
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
    categories = sorted(by_title.values(), key=lambda x: x["title"].casefold())
    if include_favorites:
        categories = _playa_favorite_categories(base_url) + categories
    return categories


def _playa_favorite_categories(base_url: str) -> list:
    return [
        {"id": _PLAYA_FAV_VIDEO_CATEGORY_ID, "title": "★ Favorite videos", "preview": _placeholder_url(base_url)},
        {"id": _PLAYA_FAV_ACTOR_CATEGORY_ID, "title": "★ Videos with favorite actors", "preview": _placeholder_url(base_url)},
        {"id": _PLAYA_FAV_STUDIO_CATEGORY_ID, "title": "★ Videos from favorite studios", "preview": _placeholder_url(base_url)},
    ]

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
    return f"★ {entry['title']}" if entry.get("is_favorite") else entry["title"]


def _playa_entity_properties(entry: dict) -> list:
    props = [
        {"name": "Favorite", "value": "yes" if entry.get("is_favorite") else "no"},
        {"name": "Videos", "value": str(int(entry.get("count") or 0))},
        {"name": "Favorite videos", "value": str(int(entry.get("favorite_video_count") or 0))},
    ]
    if entry.get("source_url"):
        props.append({"name": "POVR", "value": entry["source_url"]})
    if entry.get("trailer_url"):
        props.append({"name": "Trailer preview", "value": entry["trailer_url"]})
    return props


@app.get(f"{_PLAYA_PREFIX}/actors")
def playa_actors():
    base_url = _base_url()
    entries = _playa_entity_entries("actor", _library_items(), "performers", base_url)
    page_index, page_size = _playa_page_args()
    page = _playa_page(entries, page_index, page_size)
    page["content"] = [
        {"id": e["id"], "title": _playa_entity_display_title(e), "preview": _playa_enrich_entity_entry("actor", e)["preview"]}
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
    entry = _playa_enrich_entity_entry("actor", entry)
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
    page["content"] = [
        {"id": e["id"], "title": _playa_entity_display_title(e), "preview": _playa_enrich_entity_entry("studio", e)["preview"]}
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
    entry = _playa_enrich_entity_entry("studio", entry)
    description = entry.get("description") or f"{entry['count']} video(s) in local deovr-bridge library."
    if entry.get("source_url"):
        description += f"\nPOVR: {entry['source_url']}"
    if entry.get("trailer_url"):
        description += f"\nTrailer preview: {entry['trailer_url']}"
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
    base_url = _base_url()
    categories = _playa_category_entries(_library_items(), base_url, include_favorites=False)
    return _playa_response([
        {"id": "favorites", "title": "Favorites", "items": _playa_favorite_categories(base_url)},
        {"id": "tags", "title": "Tags", "items": categories},
    ])


@app.get(f"{_PLAYA_PREFIX}/video-statuses")
@app.get(f"{_PLAYA_PREFIX}/video-statuses;")
def playa_video_statuses():
    return _playa_response([
        {"id": "favorite", "title": "Favorite videos"},
        {"id": "normal", "title": "Not favorite"},
    ])


@app.route(f"{_PLAYA_PREFIX}/event", methods=["PUT", "POST"])
def playa_event():
    # Пока статистику не ведём: PLAY'A получает валидный ответ, а мы ничего не пишем.
    return _playa_response()


def _favorite_payload(kind: str, entity_id: str) -> dict:
    return {"kind": kind, "id": entity_id, "favorite": store.is_favorite(kind, entity_id)}


def _validate_favorite_kind(kind: str) -> bool:
    return kind in {"video", "actor", "studio"}


@app.get("/favorites")
def favorites_index():
    """Локальный JSON-список избранного.

    PLAY'A API v2 не даёт документированного write endpoint для favorites, поэтому
    управление пока делаем простыми локальными URL/HTTP-методами:
      POST/PUT/DELETE /favorites/<video|actor|studio>/<id>
      GET /favorite/<video|actor|studio>/<id>/toggle
    """
    return jsonify({"favorites": store.all_favorites()})


@app.route("/favorites/<kind>/<entity_id>", methods=["GET", "POST", "PUT", "DELETE"])
def favorites_item(kind, entity_id):
    if not _validate_favorite_kind(kind):
        return jsonify({"error": "kind must be video, actor or studio"}), 400
    entity_id = urllib.parse.unquote(entity_id)
    if request.method in {"POST", "PUT"}:
        store.set_favorite(kind, entity_id, True)
    elif request.method == "DELETE":
        store.set_favorite(kind, entity_id, False)
    return jsonify(_favorite_payload(kind, entity_id))


@app.get("/favorite/<kind>/<entity_id>/toggle")
def favorite_toggle(kind, entity_id):
    if not _validate_favorite_kind(kind):
        return jsonify({"error": "kind must be video, actor or studio"}), 400
    entity_id = urllib.parse.unquote(entity_id)
    enabled = store.toggle_favorite(kind, entity_id)
    return jsonify({"kind": kind, "id": entity_id, "favorite": enabled})


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
        "favorites": store.all_favorites(),
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
