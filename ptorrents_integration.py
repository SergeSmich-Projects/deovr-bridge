"""
PTorrents integration — ленивая загрузка торрентов через TorrServe

Получение торрента (двухшаговый процесс):
1. Извлекаем ID из URL темы (например 586330 из .../luiza-586330.html)
2. Формируем URL /dl.php?t={id} и скачиваем .torrent (с редиректами)
3. Загружаем .torrent в TorrServe

DO NOT download torrents during parsing — only when user plays video.
"""
import logging
import re
from urllib.parse import urljoin, urlparse

import requests

import config

log = logging.getLogger(__name__)

_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.ptorrents.com/",
})


def refresh_ptorrents():
    """
    Обновить список PTorrents.
    TODO: Реализовать парсер PTorrents по аналогии с Pornolab.
    Сейчас заглушка — возвращает 0 новых тем.
    """
    log.info("PTorrents refresh: парсинг не реализован, используется существующая база")
    return 0


def _extract_topic_id(topic_url: str) -> str:
    """
    Извлечь ID торрента из URL темы PTorrents.
    
    Примеры:
    - https://www.ptorrents.com/virtual-papi-vrporn-com-luiza-586330.html → 586330
    - https://ptorrents.com/forbidden-slr-erica-canfield-tiny-586371.html → 586371
    """
    # Ищем последнее число перед .html
    match = re.search(r'(\d+)\.html$', topic_url)
    if match:
        return match.group(1)
    
    # Ищем число в конце URL
    match = re.search(r'-(\d+)(?:/|$)', topic_url)
    if match:
        return match.group(1)
    
    return ""


def get_torrent_for_video(video_id: str) -> str:
    """
    Получить stream URL для PTorrents видео через TorrServe.
    
    Процесс:
    1. Берём item из БД по video_id
    2. Извлекаем ID торрента из item['source'] (URL темы)
    3. Формируем /dl.php?t={id} и скачиваем .torrent
    4. Загружаем в TorrServe через torrserve.resolve_stream_url
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
        # Шаг 1: Извлекаем ID торрента из URL темы
        topic_id = _extract_topic_id(topic_url)
        if not topic_id:
            log.error("PTorrents: не удалось извлечь ID из URL темы: %s", topic_url)
            return None
        
        log.info("PTorrents: topic_id=%s для %s", topic_id, video_id)
        
        # Шаг 2: Формируем URL для скачивания .torrent
        dl_url = f"https://www.ptorrents.com/dl.php?t={topic_id}"
        log.info("PTorrents: скачиваем .torrent из %s", dl_url)
        
        # Шаг 3: Скачиваем .torrent (с follow redirects)
        torrent_resp = _session.get(dl_url, timeout=60, allow_redirects=True)
        
        # Проверяем что это действительно .torrent файл
        content_type = torrent_resp.headers.get('Content-Type', '')
        content = torrent_resp.content
        
        if not content or len(content) < 100:
            log.error("PTorrents: пустой ответ или слишком маленький (%d bytes) для %s", len(content), dl_url)
            return None
        
        # Проверяем что это bencode (начинается с 'd')
        if content[0:1] != b'd':
            # Возможно HTML с ошибкой или редирект на страницу
            text_snippet = content[:500].decode('utf-8', errors='ignore')
            log.error("PTorrents: ответ не похож на .torrent. Content-Type=%s, начало: %s", 
                     content_type, text_snippet[:200])
            return None
        
        log.info("PTorrents: .torrent скачан (%d bytes)", len(content))
        
        # Шаг 4: Загружаем в TorrServe
        log.info("PTorrents: загружаем .torrent в TorrServe")
        stream_url = torrserve.resolve_stream_url(
            source_type="torrent_url",
            source=dl_url,
            title=item.get('title', ''),
            forum_session=_session,
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
