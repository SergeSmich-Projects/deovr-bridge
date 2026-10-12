#!/usr/bin/env python3
"""
Загрузка каталога API vrporn.com и povr.com в локальную БД
"""
import os
import sys
import time
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

def load_catalog():
    log.info("=== Загрузка каталога PLAY'A API ===")
    
    try:
        from matcher import PlayaCatalogDB, PlayaApiClient
        
        # Путь к БД — рядом с этим скриптом
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playa_catalog.db")
        
        # 1. Создаём БД
        db = PlayaCatalogDB(db_path)
        log.info(f"Текущее количество видео: {db.count_videos()}")
        
        # 2. Создаём клиенты
        vrporn_client = PlayaApiClient('vrporn.com', request_delay=0.5)
        povr_client = PlayaApiClient('povr.com', request_delay=1.0)
        
        # 3. Загружаем vrporn.com
        log.info("\n--- Загрузка vrporn.com ---")
        start = time.time()
        try:
            vrporn_client.sync_catalog(db, max_pages=30)  # ~47000 видео
            log.info(f"✓ vrporn.com: загружено {db.count_videos('vrporn.com')} видео за {time.time() - start:.1f} сек")
        except Exception as e:
            log.error(f"Ошибка загрузки vrporn.com: {e}")
        
        # 4. Загружаем povr.com
        log.info("\n--- Загрузка povr.com ---")
        start = time.time()
        try:
            # POVR: загружаем по студиям
            povr_client.sync_catalog(db, by_studios_on_povr=True)
            log.info(f"✓ povr.com: загружено {db.count_videos('povr.com')} видео за {time.time() - start:.1f} сек")
        except Exception as e:
            log.error(f"Ошибка загрузки povr.com: {e}")
        
        # 5. Итого
        total = db.count_videos()
        log.info(f"\n=== Загрузка завершена ===")
        log.info(f"Всего видео в базе: {total}")
        
        return True
        
    except Exception as e:
        log.error(f"Критическая ошибка: {e}", exc_info=True)
        return False

if __name__ == "__main__":
    success = load_catalog()
    sys.exit(0 if success else 1)
