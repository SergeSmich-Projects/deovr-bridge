"""
PTorrents integration — ленивая загрузка торрентов через TorrServe

Двухшаговый процесс:
1. GET страницы темы → извлечь ссылку из .download-button
2. GET dl.php с правильным Referer → следовать редиректам до .torrent
3. Загрузить .torrent в TorrServe

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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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
    match = re.search(r'(\d+)\.html$', topic_url)
    if match:
        return match.group(1)
    
    match = re.search(r'-(\d+)(?:/|$)', topic_url)
    if match:
        return match.group(1)
    
    return ""


def _extract_download_button_url(html: str) -> str:
    """
    Извлечь URL из кнопки .download-button на странице темы.
    
    Ищет: <a href="/dl.php?t=586330" class="download-button" ...>
    """
    # Ищем кнопку download-button
    match = re.search(r'class=["\']download-button["\'][^>]*href=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if match:
        return match.group(1)
    
    # Или href перед class
    match = re.search(r'href=["\']([^"\']+)["\'][^>]*class=["\']download-button["\']', html, re.IGNORECASE)
    if match:
        return match.group(1)
    
    # Или просто dl.php ссылку
    match = re.search(r'href=["\'](/dl\.php\?t=\d+)["\']', html, re.IGNORECASE)
    if match:
        return match.group(1)
    
    return ""


def _extract_torrent_from_html(html: str) -> str:
    """
    Извлечь прямую ссылку на .torrent из HTML страницы dl.php.
    
    Ищет: https://d.ptorrents.com/bt/...torrent
    """
    # Прямая ссылка на .torrent
    match = re.search(r'https?://d\.ptorrents\.com/bt/[^"\'<>\s]+\.torrent', html, re.IGNORECASE)
    if match:
        return match.group(0)
    
    match = re.search(r'/bt/[^"\'<>\s]+\.torrent', html, re.IGNORECASE)
    if match:
        return match.group(0)
    
    return ""


def get_torrent_for_video(video_id: str) -> str:
    """
    Получить stream URL для PTorrents видео через TorrServe.
    
    Двухшаговый процесс:
    1. GET страницы темы → извлечь ссылку из .download-button
    2. GET dl.php с Referer → следовать редиректам до .torrent
    3. Загрузить в TorrServe
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
        # Шаг 1: GET страницы темы
        log.info("PTorrents: загружаем страницу темы %s", topic_url)
        topic_resp = _session.get(topic_url, timeout=30)
        topic_resp.raise_for_status()
        
        # Шаг 2: Извлечь URL из кнопки .download-button
        dl_url = _extract_download_button_url(topic_resp.text)
        if not dl_url:
            log.error("PTorrents: не удалось найти .download-button на странице %s", topic_url)
            return None
        
        # Если относительный URL — делаем абсолютным
        if dl_url.startswith('/'):
            parsed = urlparse(topic_url)
            dl_url = f"{parsed.scheme}://{parsed.netloc}{dl_url}"
        
        log.info("PTorrents: кнопка скачивания: %s", dl_url)
        
        # Шаг 3: GET dl.php с правильным Referer
        log.info("PTorrents: скачиваем .torrent через %s", dl_url)
        dl_resp = _session.get(
            dl_url,
            headers={"Referer": topic_url},
            timeout=60,
            allow_redirects=True
        )
        
        # Проверяем ответ
        content = dl_resp.content
        content_type = dl_resp.headers.get('Content-Type', '')
        
        if not content or len(content) < 100:
            log.error("PTorrents: пустой ответ (%d bytes) для %s", len(content), dl_url)
            return None
        
        # Если вернулся HTML — возможно JS редирект, парсим ссылку на .torrent
        if content_type.startswith('text/html') or content[0:1] != b'd':
            html_text = content.decode('utf-8', errors='ignore')
            log.info("PTorrents: dl.php вернул HTML, ищем ссылку на .torrent")
            
            torrent_url = _extract_torrent_from_html(html_text)
            if not torrent_url:
                log.error("PTorrents: не удалось найти .torrent ссылку в HTML ответе")
                log.debug("HTML preview: %s", html_text[:500])
                return None
            
            # Если относительный URL — делаем абсолютным
            if torrent_url.startswith('/'):
                parsed = urlparse(dl_url)
                torrent_url = f"{parsed.scheme}://{parsed.netloc}{torrent_url}"
            
            log.info("PTorrents: найдена прямая ссылка: %s", torrent_url)
            
            # Скачиваем .torrent по прямой ссылке
            torrent_resp = _session.get(torrent_url, timeout=60)
            content = torrent_resp.content
        
        # Проверяем что это .torrent файл (bencode начинается с 'd')
        if content[0:1] != b'd':
            text_snippet = content[:500].decode('utf-8', errors='ignore')
            log.error("PTorrents: ответ не .torrent. Content-Type=%s, preview: %s", 
                     content_type, text_snippet[:200])
            return None
        
        log.info("PTorrents: .torrent скачан (%d bytes)", len(content))
        
        # Шаг 4: Загрузить в TorrServe
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
    """
    return get_torrent_for_video(video_id)
