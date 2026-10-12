#!/usr/bin/env python3
"""
Быстрый тест нового matcher.py
"""
import sys
import time
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

def test_matcher():
    log.info("=== Тест нового matcher.py ===")
    
    try:
        from matcher import PlayaCatalogDB, PlayaApiClient, PlayaMatcher, parse_pornolab_title
        
        # 1. Создаём/открываем локальную БД каталога
        log.info("1. Инициализация PlayaCatalogDB...")
        db = PlayaCatalogDB("playa_catalog.db")
        log.info(f"   Видео в базе: {db.count_videos()}")
        
        # 2. Создаём API клиенты
        log.info("2. Инициализация API клиентов...")
        clients = {
            'vrporn.com': PlayaApiClient('vrporn.com', request_delay=0.5),
            'povr.com': PlayaApiClient('povr.com', request_delay=1.0),
        }
        
        # 3. Создаём matcher
        log.info("3. Инициализация PlayaMatcher...")
        matcher = PlayaMatcher(db, api_clients=clients)
        
        # 4. Тестовое сопоставление
        log.info("4. Тестовое сопоставление...")
        test_titles = [
            "[18VR.com] Michy Perez - Mente Caliente [05.08.2024, Anal, 8K, 4096p]",
            "[BaDoinkVR] Elsa Jean - The Art of Seduction [2024, Blowjob, HD]",
        ]
        
        for title in test_titles:
            log.info(f"\n   Тест: {title}")
            start = time.time()
            result = matcher.match_one(title, top_k=3, verify_details=False)
            elapsed = time.time() - start
            
            if result.get('matches'):
                best = result['matches'][0]
                log.info(f"   ✓ Лучшее совпадение: {best.get('confidence', 0):.1f}% за {elapsed:.3f} сек")
                log.info(f"     Видео: {best.get('video', {}).get('title', 'N/A')}")
            else:
                log.info(f"   ✗ Совпадений не найдено за {elapsed:.3f} сек")
        
        log.info("\n=== Тест завершён успешно ===")
        return True
        
    except Exception as e:
        log.error(f"Ошибка: {e}", exc_info=True)
        return False

if __name__ == "__main__":
    success = test_matcher()
    sys.exit(0 if success else 1)
