"""
PTorrents integration — ленивая загрузка торрентов через TorrServe

Двухшаговый процесс:
1. dl.php HTML → извлекаем ссылку на .torrent (d.ptorrents.com/bt/...torrent)
2. Скачиваем .torrent и загружаем в TorrServe

DO NOT download torrents during parsing — only when user plays video.
"""
import logging
import re

import requests

import config

log = logging.getLogger(__name__)

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
})


def refresh_ptorrents():
    """
    Обновить список PTorrents.
    Парсинг страниц 1-340 PTorrents и сохранение метаданных в БД.
    
    TODO: Реализовать парсер PTorrents по аналогии с Pornolab.
    Сейчас заглушка — возвращает 0 новых тем.
    """
    log.info("PTorrents refresh: парсинг не реализован, используется существующая база")
    return 0


def _extract_torrent_url(html: str) -> str:
    """
    Извлечь ссылку на .torrent из HTML страницы dl.php.
    
    Ищем паттерны:
    - d.ptorrents.com/bt/xxx.torrent
    - /bt/xxx.torrent
    - download.php?file=xxx
    """
    # Паттерн для d.ptorrents.com/bt/...torrent
    match = re.search(r'https?://d\.ptorrents\.com/bt/[^"\'<>\s]+\.torrent', html)
    if match:
        return match.group(0)
    
    # Паттерн для /bt/...torrent
    match = re.search(r'/bt/[^"\'<>\s]+\.torrent', html)
    if match:
        return match.group(0)
    
    # Паттерн для download.php
    match = re.search(r'download\.php\?file=[^"\'<>\s]+', html)
    if match:
        return match.group(0)
    
    return ""


def get_torrent_for_video(video_id: str) -> str:
    """
    Получить stream URL для PTorrents видео через TorrServe.
    
    Двухшаговый процесс:
    1. Сходить на topic_url (item['source'])
    2. Извлечь ссылку на .torrent из HTML
    3. Использовать torrserve.resolve_stream_url для загрузки в TorrServe
    """
    import store
    import torrserve
    
    item = store.get_item(video_id)
    if not item:
        log.error("PTorrents item not found: %s", video_id)
        return None
    
    topic_url = item.get('source', '')
    if not topic_url:
        log.error("PTorrents item has no source URL: %s", video_id)
        return None
    
    try:
        # Шаг 1: Сходим на topic_url
        log.info("PTorrents: скачиваем HTML для %s из %s", video_id, topic_url)
        resp = _session.get(topic_url, timeout=30)
        resp.raise_for_status()
        
        # Шаг 2: Извлекаем ссылку на .torrent
        torrent_url = _extract_torrent_url(resp.text)
        if not torrent_url:
            log.error("PTorrents: не удалось найти .torrent URL в HTML для %s", video_id)
            return None
        
        # Если относительный URL — делаем абсолютным
        if torrent_url.startswith('/'):
            from urllib.parse import urlparse
            parsed = urlparse(topic_url)
            torrent_url = f"{parsed.scheme}://{parsed.netloc}{torrent_url}"
        
        log.info("PTorrents: найден .torrent URL: %s", torrent_url)
        
        # Шаг 3: Используем torrserve.resolve_stream_url
        stream_url = torrserve.resolve_stream_url(
            source_type="torrent_url",
            source=torrent_url,
            title=item.get('title', ''),
        )
        
        log.info("PTorrents: stream URL для %s: %s", video_id, stream_url)
        return stream_url
        
    except requests.RequestException as e:
        log.error("PTorrents HTTP error for %s: %s", video_id, e)
        return None
    except Exception as e:
        log.error("PTorrents error for %s: %s", video_id, e, exc_info=True)
        return None


def get_stream_for_video(video_id: str):
    """
    Получить ссылку на стриминг видео через TorrServe.
    Использует get_torrent_for_video для lazy загрузки.
    """
    return get_torrent_for_video(video_id)
