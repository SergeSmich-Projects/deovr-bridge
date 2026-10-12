"""
POVR integration через Playa API v2
"""
import logging
import time

log = logging.getLogger(__name__)

# Кэш для API ответов
_cache = {}
_cache_ttl = 300  # 5 минут


def refresh_povr():
    """
    Обновить список POVR через Playa API.
    
    TODO: Реализовать парсинг POVR через Playa API.
    Сейчас заглушка — возвращает 0 новых тем.
    """
    log.info("POVR refresh: парсинг не реализован, используется существующая база")
    return 0


def _get_client():
    """Получить PlayaApiClient для POVR."""
    try:
        from matcher import PlayaApiClient
        return PlayaApiClient('povr.com', request_delay=0.5)
    except Exception as e:
        log.error("Failed to create POVR API client: %s", e)
        return None


def get_povr_videos(page_index=0, page_size=12, order="newest", actor_id=None, **kwargs):
    """Получить список видео POVR."""
    cache_key = f"videos_{page_index}_{page_size}_{order}_{actor_id}"
    if cache_key in _cache and time.time() - _cache[cache_key]['time'] < _cache_ttl:
        return _cache[cache_key]['data']
    
    client = _get_client()
    if not client:
        return {"error": "POVR API client not available"}
    
    try:
        # PLAY'A API использует page-index с 1 для POVR
        params = {
            "page-index": page_index + 1,
            "page-size": page_size,
            "order": order,
            "direction": "desc",
        }
        
        if actor_id:
            params["actor"] = actor_id
        
        data = client.get_json("videos", params)
        
        # Преобразуем в формат PLAY'a
        content = []
        for item in data.get("content", []):
            content.append({
                "id": str(item.get("id", "")),
                "title": item.get("title", ""),
                "subtitle": item.get("subtitle", ""),
                "preview_image": item.get("preview_image", ""),
                "release_date": item.get("release_date", 0),
                "duration": 0,
                "has_scripts": False,
            })
        
        result = {
            "page_index": page_index,
            "page_size": page_size,
            "page_total": int(data.get("page_total", 1)),
            "content": content,
        }
        
        _cache[cache_key] = {'data': result, 'time': time.time()}
        return result
        
    except Exception as e:
        log.error("POVR get_videos error: %s", e)
        return {"error": str(e)}


def get_video_details(video_id):
    """Получить детали видео POVR."""
    # Убираем префикс povr_ если есть
    if video_id.startswith("povr_"):
        video_id = video_id[5:]
    
    cache_key = f"details_{video_id}"
    if cache_key in _cache and time.time() - _cache[cache_key]['time'] < _cache_ttl:
        return _cache[cache_key]['data']
    
    client = _get_client()
    if not client:
        return None
    
    try:
        data = client.get_json(f"video/{video_id}")
        _cache[cache_key] = {'data': data, 'time': time.time()}
        return data
    except Exception as e:
        log.error("POVR get_video_details error for %s: %s", video_id, e)
        return None


def get_stream_for_video(video_id):
    """Получить ссылку на стриминг видео."""
    # Убираем префикс povr_ если есть
    if video_id.startswith("povr_"):
        video_id = video_id[5:]
    
    # POVR не поддерживает прямой стриминг через наш API
    # Возвращаем ссылку на трейлер
    details = get_video_details(video_id)
    if not details:
        return None
    
    for detail in details.get("details", []):
        if detail.get("type") == "trailer":
            links = detail.get("links", [])
            if links:
                return links[0].get("url")
    
    return None


def get_povr_actors(page_index=0, page_size=50, **kwargs):
    """Получить список актёров POVR."""
    client = _get_client()
    if not client:
        return {"error": "POVR API client not available"}
    
    try:
        data = client.get_json("actors", {"page-index": page_index + 1, "page-size": page_size})
        content = []
        for item in data.get("content", []):
            content.append({
                "id": str(item.get("id", "")),
                "title": item.get("title", ""),
            })
        return {
            "page_index": page_index,
            "page_size": page_size,
            "page_total": int(data.get("page_total", 1)),
            "content": content,
        }
    except Exception as e:
        log.error("POVR get_actors error: %s", e)
        return {"error": str(e)}


def get_povr_actor(actor_id):
    """Получить детали актёра POVR."""
    client = _get_client()
    if not client:
        return None
    
    try:
        return client.get_json(f"actor/{actor_id}")
    except Exception as e:
        log.error("POVR get_actor error for %s: %s", actor_id, e)
        return None


def get_povr_studios(page_index=0, page_size=50, **kwargs):
    """Получить список студий POVR."""
    client = _get_client()
    if not client:
        return {"error": "POVR API client not available"}
    
    try:
        data = client.get_json("studios", {"page-index": page_index + 1, "page-size": page_size})
        content = []
        for item in data.get("content", []):
            content.append({
                "id": str(item.get("id", "")),
                "title": item.get("title", ""),
            })
        return {
            "page_index": page_index,
            "page_size": page_size,
            "page_total": int(data.get("page_total", 1)),
            "content": content,
        }
    except Exception as e:
        log.error("POVR get_studios error: %s", e)
        return {"error": str(e)}


def get_povr_studio(studio_id):
    """Получить детали студии POVR."""
    client = _get_client()
    if not client:
        return None
    
    try:
        return client.get_json(f"studio/{studio_id}")
    except Exception as e:
        log.error("POVR get_studio error for %s: %s", studio_id, e)
        return None
