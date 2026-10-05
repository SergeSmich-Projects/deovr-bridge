
# -*- coding: utf-8 -*-
"""
Обёртка над HTTP API TorrServe.

Логика:
  1) magnet-ссылка -> POST /torrents {"action":"add","link":magnet,"save_to_db":false}
     TorrServe скачивает метаданные и сразу возвращает список файлов.
  2) "непрямая" ссылка на .torrent с форума (нужны куки форума) -> мы сами скачиваем
     .torrent байтами (с авторизованной сессией) и заливаем в TorrServe через
     POST /torrent/upload (multipart) - тоже сразу возвращает hash + список файлов.

Дальше по списку файлов выбираем самый большой видео-файл и строим прямую ссылку
на поток: GET /stream/{fname}?link={hash}&index={N}&play
"""

import io
import logging
import re
import urllib.parse

import requests

import config

log = logging.getLogger("torrserve")

_session = requests.Session()
if config.TORRSERVE_USER:
    _session.auth = (config.TORRSERVE_USER, config.TORRSERVE_PASS)


class TorrServeError(RuntimeError):
    pass


def _base():
    return config.TORRSERVE_BASE_URL.rstrip("/")


def add_magnet(link: str) -> dict:
    """Добавляет magnet/hash-ссылку, TorrServe сам резолвит метаданные и список файлов."""
    resp = _session.post(
        f"{_base()}/torrents",
        json={"action": "add", "link": link, "save_to_db": False},
        timeout=60,
    )
    if not resp.ok:
        raise TorrServeError(f"TorrServe /torrents(add) ответил {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError:
        raise TorrServeError(f"Неожиданный ответ TorrServe (add): {resp.text[:300]}")


def upload_torrent_file(torrent_bytes: bytes, title: str = "", poster: str = "") -> dict:
    """Заливает .torrent (байты) в TorrServe, возвращает метаданные раздачи (hash + файлы)."""
    files = {"file": ("upload.torrent", io.BytesIO(torrent_bytes), "application/x-bittorrent")}
    data = {"save_to_db": "false"}
    if title:
        data["title"] = title
    if poster:
        data["poster"] = poster
    resp = _session.post(f"{_base()}/torrent/upload", files=files, data=data, timeout=60)
    if not resp.ok:
        raise TorrServeError(f"TorrServe /torrent/upload ответил {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError:
        raise TorrServeError(f"Неожиданный ответ TorrServe (upload): {resp.text[:300]}")


def get_torrent_by_hash(hash_: str) -> dict:
    """Запасной вариант - перечитать метаданные уже добавленной раздачи по её hash."""
    resp = _session.post(
        f"{_base()}/torrents",
        json={"action": "get", "hash": hash_},
        timeout=30,
    )
    if not resp.ok:
        raise TorrServeError(f"TorrServe /torrents(get) ответил {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError:
        raise TorrServeError(f"Неожиданный ответ TorrServe (get): {resp.text[:300]}")


def _extract_hash(info: dict) -> str:
    for key in ("hash", "Hash"):
        if info.get(key):
            return info[key]
    inner = info.get("torrent") or info.get("Torrent")
    if inner:
        return _extract_hash(inner)
    return ""


def _extract_files(info: dict):
    for key in ("file_stats", "FileStats", "Files", "files"):
        if info.get(key):
            return info[key]
    inner = info.get("torrent") or info.get("Torrent")
    if inner:
        return _extract_files(inner)
    return []


def _pick_video_index(files: list) -> int:
    best_idx, best_size = 0, -1
    for i, f in enumerate(files):
        path = (f.get("path") or f.get("Path") or f.get("name") or f.get("Name") or "").lower()
        length = f.get("length") or f.get("Length") or f.get("size") or f.get("Size") or 0
        if path.endswith(config.VIDEO_EXTENSIONS) and length > best_size:
            best_idx, best_size = i, length
    return best_idx


def _html_to_text_snippet(content: bytes, limit: int = 700) -> str:
    """Вытаскивает читаемый текст из HTML-страницы (без <head>/<script>/<style> и тегов) -
    сырые первые байты почти всегда состоят из мета-тегов/dns-prefetch и бесполезны для
    диагностики, настоящее сообщение сайта обычно дальше, в <body>."""
    try:
        text = content.decode("windows-1251", errors="replace")
    except Exception:
        return repr(content[:200])

    title_m = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
    title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""

    body_m = re.search(r"(?is)<body[^>]*>(.*)</body>", text)
    body = body_m.group(1) if body_m else text
    body = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = body.replace("&nbsp;", " ")
    body = " ".join(body.split())

    prefix = f"[<title>: {title}] " if title else ""
    return (prefix + body)[:limit]


def resolve_stream_url(source_type: str, source: str, forum_session=None, title: str = "",
                        public_base_url: str = None) -> str:
    """
    Главная функция: по данным карточки (magnet или ссылка на .torrent) возвращает
    готовый HTTP-адрес потока TorrServe, который можно отдать в DeoVR как videoSources.url

    ВАЖНО про public_base_url: сам запрос к TorrServe (добавление magnet/.torrent)
    всегда идёт на config.TORRSERVE_BASE_URL (обычно 127.0.0.1 - наш сервер и
    TorrServe работают на одном ПК). А вот ИТОГОВУЮ ссылку /stream/... видит уже
    Quest - ОТДЕЛЬНОЕ устройство в сети, для которого "127.0.0.1" означает его
    самого, а не ваш ПК! Поэтому для текста самой ссылки нужен LAN-адрес ПК
    (тот же, по которому Quest уже открывает /deovr) - его передаёт app.py через
    public_base_url. Если не передан (например, при вызове из debug-скриптов) -
    используем тот же внутренний адрес, что и для запросов к TorrServe.
    """
    if source_type == "torrent_url":
        sess = forum_session or requests
        r = sess.get(source, timeout=30)
        r.raise_for_status()
        content = r.content
        if content[:1] not in (b"d",) or b"announce" not in content[:2000]:
            # похоже, пришёл не .torrent файл, а HTML (протухшая сессия, лимит скачиваний
            # в сутки, предупреждение/подтверждение вместо файла и т.п.) - печатаем, что
            # РЕАЛЬНО прислал сервер (уже очищенный от HTML-тегов текст), чтобы не гадать вслепую
            text_snippet = _html_to_text_snippet(content)
            raise TorrServeError(
                "Скачанный файл не похож на .torrent. HTTP "
                f"{r.status_code}, итоговый URL после редиректов: {r.url}, "
                f"Content-Type: {r.headers.get('Content-Type')!r}. "
                f"Текст страницы: {text_snippet!r}"
            )
        info = upload_torrent_file(content, title=title)
    elif source_type == "magnet":
        info = add_magnet(source)
    else:
        raise TorrServeError(f"Неизвестный source_type: {source_type}")

    torrent_hash = _extract_hash(info)
    if not torrent_hash:
        raise TorrServeError(f"TorrServe не вернул hash раздачи. Ответ: {str(info)[:300]}")

    files = _extract_files(info)
    if not files:
        # иногда список файлов приходит не сразу - перечитаем метаданные по hash
        info2 = get_torrent_by_hash(torrent_hash)
        files = _extract_files(info2)

    if files:
        idx = _pick_video_index(files)
        fname = files[idx].get("path") or files[idx].get("Path") or files[idx].get("name") or "video.mp4"
        fname = fname.split("/")[-1]
    else:
        idx, fname = 0, "video.mp4"

    quoted_hash = urllib.parse.quote(torrent_hash, safe="")
    quoted_name = urllib.parse.quote(fname)
    base = (public_base_url or _base()).rstrip("/")
    return f"{base}/stream/{quoted_name}?link={quoted_hash}&index={idx}&play"
