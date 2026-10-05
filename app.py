
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

import logging
import threading
import time
import urllib.parse

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


def _item_to_card(item: dict, base_url: str) -> dict:
    """Короткий формат карточки для списка (экономит трафик, DeoVR подтянет video_url при клике)."""
    thumb = item["thumbnail_url"] or f"{base_url}/static/placeholder.jpg"
    return {
        "id": item["id"],
        "title": item["title"] + _seed_leech_suffix(item),
        "videoLength": item.get("video_length") or 0,
        "thumbnailUrl": thumb,
        "video_url": f"{base_url}/video/{item['id']}",
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
    return jsonify({"status": "ok", "items": len(store.all_items())})


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


@app.get("/deovr")
@app.get("/")
def deovr_root():
    """
    Источники НЕ смешиваются между собой (так попросил пользователь) - у каждого
    источника (config.FORUM_SITES) своя вкладка "Всё"/"Клон: Всё" и свои вкладки
    по тегам ("<тег>" / "Клон: <тег>"), посчитанные отдельно для его собственных
    карточек.
    """
    base_url = _base_url()
    all_items = store.all_items()

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

    return jsonify({"scenes": scenes, "authorized": "0"})


@app.get("/video/<item_id>")
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
    item = store.get_item(item_id)
    if not item:
        return jsonify({"error": "not found"}), 404

    stream_url = f"{_base_url()}/stream/{urllib.parse.quote(str(item_id), safe='')}"

    return jsonify({
        "id": item_id,
        "title": item["title"] + _seed_leech_suffix(item),
        "is3d": True,
        "screenType": item["screen_type"],
        "stereoMode": item["stereo_mode"],
        "videoLength": item.get("video_length") or 0,
        "thumbnailUrl": item["thumbnail_url"],
        "encodings": [{
            "name": "h264",
            "videoSources": [{"resolution": 1080, "url": stream_url}],
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
    item = store.get_item(item_id)
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
    for i in store.all_items():
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
    for i in store.all_items():
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
    item = store.get_item(item_id)
    if not item:
        return jsonify({"access": -1}), 404

    base_url = _base_url()
    projection, fov = _HERESPHERE_PROJECTION.get(item.get("screen_type"), ("equirectangular", None))
    stereo = "tb" if item.get("stereo_mode") == "tb" else "sbs"

    dto = {
        "access": 1,
        "title": item["title"] + _seed_leech_suffix(item),
        "thumbnailImage": item["thumbnail_url"] or f"{base_url}/static/placeholder.jpg",
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
    dto["media"] = [{"name": "TorrServe", "sources": [{"resolution": 1080, "url": stream_url}]}]

    resp = jsonify(dto)
    resp.headers["HereSphere-JSON-Version"] = "1"
    return resp


@app.get("/thumb/<item_id>")
def thumb_proxy(item_id):
    item = store.get_item(item_id)
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
