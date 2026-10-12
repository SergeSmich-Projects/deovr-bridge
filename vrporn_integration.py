"""
VRPorn integration через Playa API v2
"""
import logging
import time

log = logging.getLogger(__name__)

# Кэш для API ответов
_cache = {}
_cache_ttl = 300  # 5 минут


def refresh_vrporn():
    """
    Обновить список VRPorn через Playa API.
    
    TODO: Реализовать парсинг VRPorn через Playa API.
    Сейчас заглушка — возвращает 0 новых тем.
    """
    log.info("VRPorn refresh: парсинг не реализован, используется существующая база")
    return 0


def _get_client():
    """Получить PlayaApiClient для VRPorn."""
    try:
        from matcher import PlayaApiClient
        return PlayaApiClient('vrporn.com', request_delay=0.3)
    except Exception as e:
        log.error("Failed to create VRPorn API client: %s", e)
        return None


def get_vrporn_videos(page_index=0, page_size=12, order="newest", **kwargs):
    """Получить список видео VRPorn."""
    cache_key = f"videos_{page_index}_{page_size}_{order}"
    if cache_key in _cache and time.time() - _cache[cache_key]['time'] < _cache_ttl:
        return _cache[cache_key]['data']
    
    client = _get_client()
    if not client:
        return {"error": "VRPorn API client not available"}
    
    try:
        # PLAY'A API использует page-index с 0
        params = {
            "page-index": page_index,
            "page-size": page_size,
            "order": order,
            "direction": "desc",
        }
        
        data = client.get_json("videos", params)
        
        # Преобразуем в формат PLAY'a — добавляем префикс vrporn_ к ID
        content = []
        for item in data.get("content", []):
            content.append({
                "id": f"vrporn_{item.get('id', '')}",
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
        log.error("VRPorn get_videos error: %s", e)
        return {"error": str(e)}


def get_video_details(video_id):
    """Получить детали видео VRPorn."""
    # Убираем префикс vrporn_ если есть
    if video_id.startswith("vrporn_"):
        video_id = video_id[7:]
    
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
        log.error("VRPorn get_video_details error for %s: %s", video_id, e)
        return None


def get_stream_for_video(video_id):
    """Получить ссылку на стриминг видео."""
    # Убираем префикс vrporn_ если есть
    if video_id.startswith("vrporn_"):
        video_id = video_id[7:]
    
    # VRPorn не поддерживает прямой стриминг через наш API
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


def get_vrporn_actors(page_index=0, page_size=50, **kwargs):
    """Получить список актёров VRPorn."""
    client = _get_client()
    if not client:
        return {"error": "VRPorn API client not available"}
    
    try:
        data = client.get_json("actors", {"page-index": page_index, "page-size": page_size})
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
        log.error("VRPorn get_actors error: %s", e)
        return {"error": str(e)}


def get_vrporn_actor(actor_id):
    """Получить детали актёра VRPorn."""
    client = _get_client()
    if not client:
        return None
    
    try:
        return client.get_json(f"actor/{actor_id}")
    except Exception as e:
        log.error("VRPorn get_actor error for %s: %s", actor_id, e)
        return None


def get_vrporn_studios(page_index=0, page_size=50, **kwargs):
    """Получить список студий VRPorn."""
    client = _get_client()
    if not client:
        return {"error": "VRPorn API client not available"}
    
    try:
        data = client.get_json("studios", {"page-index": page_index, "page-size": page_size})
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
        log.error("VRPorn get_studios error: %s", e)
        return {"error": str(e)}


def get_vrporn_studio(studio_id):
    """Получить детали студии VRPorn."""
    client = _get_client()
    if not client:
        return None
    
    try:
        return client.get_json(f"studio/{studio_id}")
    except Exception as e:
        log.error("VRPorn get_studio error for %s: %s", studio_id, e)
        return None
