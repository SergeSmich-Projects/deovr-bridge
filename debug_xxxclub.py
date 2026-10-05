# -*- coding: utf-8 -*-
"""
Быстрая проверка источника XXXClub.to без полного /refresh - читает первую
страницу категории VR, парсит первую раздачу целиком (magnet, теги, превью) и
печатает результат. Полезно прогнать один раз после установки, прежде чем
запускать app.py - чтобы сразу увидеть, если сайт поменял вёрстку.

Запуск:
    python debug_xxxclub.py
"""
import json

import config
import scraper

site = next(s for s in config.FORUM_SITES if s["id"] == "xxxclub")

print(f"Источник: {site['id']} ({site['base_url']}{site['category_path']})")
print()

url = scraper._xxxclub_listing_url(site, None)
print("1) Запрашиваем страницу списка:", url)
soup = scraper._get(site, url)
rows = scraper._xxxclub_parse_listing(soup)
print(f"   Найдено раздач на странице: {len(rows)}")

next_cursor = scraper._xxxclub_next_cursor(soup)
print(f"   Курсор следующей страницы: {next_cursor!r}")
print()

if not rows:
    print("!!! Раздач не найдено - вероятно, сайт поменял вёрстку списка "
          "(класс 'browsetableinside'/'catlabe' или структуру <li>). "
          "Пришлите HTML страницы для разбора.")
    raise SystemExit(1)

first = rows[0]
print("2) Первая раздача в списке:")
print(json.dumps(first, ensure_ascii=False, indent=2))
print()

print("3) Разбираем страницу самой раздачи:", first["path"])
details = scraper._xxxclub_parse_details(site, first["path"])
print(json.dumps(details, ensure_ascii=False, indent=2))
print()

if not details.get("magnet"):
    print("!!! Magnet-ссылка не найдена - вероятно, сайт поменял вёрстку страницы "
          "раздачи. Пришлите HTML этой страницы для разбора.")
    raise SystemExit(1)

print("ВСЁ OK - можно запускать app.py и жать /refresh.")
