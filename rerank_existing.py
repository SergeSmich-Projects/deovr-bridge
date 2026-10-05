# -*- coding: utf-8 -*-
"""
Разовый скрипт: пересчитывает source_rank для УЖЕ существующих в базе карточек,
заново проходя списки разделов (ТОЛЬКО страницы-списки, без захода в темы -
дёшево и быстро). Нужен один раз после обновления до версии с источником
сортировки = source_rank, чтобы старые ~12000+ карточек, попавшие в базу ДО
этой миграции (и получившие "заглушку" source_rank=999999999), встали в
правильное место, а не болтались все вперемешку в хвосте списка.

Дальше, по мере обычной работы краулера (refresh_all), новые карточки и так
получают правильный source_rank сразу при обнаружении - этот скрипт НЕ нужно
гонять регулярно, только один раз (и опционально - если закрадутся сомнения
в порядке после каких-то сбоев).

ВАЖНО: перед запуском ОСТАНОВИТЕ app.py (во избежание одновременной записи
в один и тот же sqlite-файл с разных процессов).

Запуск:  python rerank_existing.py
"""

import time
import logging

import config
import store
import scraper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("rerank")


def _rerank_pornolab_like(site: dict):
    site_id = site["id"]
    updated = 0
    checked = 0
    for section_url in site["section_urls"]:
        total_pages = site.get("total_pages") or 1
        log.info("[%s] Раздел %s: проходим %s страниц списка (только список, без тем)...",
                 site_id, section_url, total_pages)
        for page in range(total_pages):
            try:
                topics = scraper.fetch_listing_page(site_id, section_url, page)
            except Exception as e:
                log.warning("[%s] Страница %s: ошибка чтения (%s) - пропускаем", site_id, page, e)
                continue
            if not topics:
                log.info("[%s] Страница %s пустая - считаем, что раздел закончился.", site_id, page)
                break
            for row_index, row in enumerate(topics):
                item_id = f"{site['id_prefix']}{row['topic_id']}"
                checked += 1
                if store.item_exists(item_id):
                    rank = page * scraper._RANK_PAGE_MULT + row_index
                    store.set_source_rank(item_id, rank)
                    updated += 1
            if page % 20 == 0:
                log.info("[%s] ...страница %s/%s обработана (обновлено рангов: %s)",
                         site_id, page, total_pages, updated)
            time.sleep(0.3)
    log.info("[%s] Готово. Проверено строк списка: %s, обновлено рангов существующих карточек: %s",
              site_id, checked, updated)


def _rerank_xxxclub(site: dict):
    site_id = site["id"]
    updated = 0
    checked = 0
    cursor = None
    page_num = 0
    while True:
        try:
            soup = scraper._get(site, scraper._xxxclub_listing_url(site, cursor))
        except Exception as e:
            log.warning("[%s] Ошибка чтения страницы (курсор=%s): %s - останавливаемся", site_id, cursor, e)
            break
        rows = scraper._xxxclub_parse_listing(soup)
        if not rows:
            log.info("[%s] Страница (курсор=%s) пустая - архив закончился.", site_id, cursor)
            break
        for row_index, row in enumerate(rows):
            item_id = scraper._xxxclub_item_id(site, row["path"])
            checked += 1
            if store.item_exists(item_id):
                if page_num == 0:
                    rank = row_index  # первая страница - всегда маленький ранг
                else:
                    rank = scraper._XXXCLUB_BACKFILL_RANK_BASE + page_num * scraper._RANK_PAGE_MULT + row_index
                store.set_source_rank(item_id, rank)
                updated += 1
        log.info("[%s] Страница #%s (курсор=%s): %s раздач, обновлено рангов всего: %s",
                 site_id, page_num, cursor, len(rows), updated)
        new_cursor = scraper._xxxclub_next_cursor(soup)
        if not new_cursor:
            log.info("[%s] Дальше страниц нет - архив пройден полностью.", site_id)
            break
        cursor = new_cursor
        page_num += 1
        time.sleep(0.4)
    log.info("[%s] Готово. Проверено строк списка: %s, обновлено рангов существующих карточек: %s",
              site_id, checked, updated)


def main():
    store.init_db()
    for site in config.FORUM_SITES:
        log.info("=== Источник: %s ===", site["id"])
        try:
            scraper.login(site["id"])
        except Exception as e:
            log.warning("[%s] Логин не удался (%s) - для некоторых источников это нормально (xxxclub без логина)",
                        site["id"], e)
        if site["id"] == "xxxclub":
            _rerank_xxxclub(site)
        else:
            _rerank_pornolab_like(site)


if __name__ == "__main__":
    main()
