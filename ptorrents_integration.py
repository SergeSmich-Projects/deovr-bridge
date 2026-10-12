"""
PTorrents integration stub
"""
import logging

log = logging.getLogger(__name__)


def refresh_ptorrents():
    """Обновить список PTorrents."""
    log.info("PTorrents refresh not implemented")
    return []


def get_torrent_for_video(video_id):
    """Получить torrent для видео."""
    log.warning("PTorrents get_torrent_for_video not implemented for %s", video_id)
    return None


def get_stream_for_video(video_id):
    """Получить ссылку на стриминг видео."""
    log.warning("PTorrents get_stream_for_video not implemented for %s", video_id)
    return None
