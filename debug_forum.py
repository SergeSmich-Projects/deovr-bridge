
# -*- coding: utf-8 -*-
"""
Диагностика: заходит на страницу раздела с текущими cookies/логином и печатает,
что реально пришло в ответ - без скачивания/записи в базу. Безопасно отправлять
мне ВЫВОД этого скрипта (пароли/cookies в нём не печатаются).

Запуск: python debug_forum.py [site_id]
  site_id - "pornolab" (по умолчанию) или "xxxtorrent" (клон), см. config.FORUM_SITES
"""

import sys
import config
import scraper

site_id = sys.argv[1] if len(sys.argv) > 1 else "pornolab"
site = scraper._site(site_id)

print(f"Источник: {site_id} ({site['base_url']})")
print("1) Логинимся...")
ok = scraper.login(site_id)
print("   login() вернул:", ok)
sess = scraper.get_forum_session(site_id)
print("   текущие cookies в сессии:", scraper.cookie_names(site_id))

url = site["section_urls"][0]
print(f"\n2) Запрашиваем раздел: {url}")

resp = sess.get(url, timeout=30)
print("   HTTP статус:", resp.status_code)
print("   Итоговый URL после редиректов:", resp.url)
resp.encoding = site["encoding"]
html = resp.text
print("   Длина HTML:", len(html), "символов")

out_file = f"debug_forum_{site_id}.html"
with open(out_file, "w", encoding=site["encoding"], errors="ignore") as f:
    f.write(html)
print(f"   Полный HTML сохранён в {out_file} (можно открыть в браузере)")

from bs4 import BeautifulSoup
soup = BeautifulSoup(html, "html.parser")
print("   <title>:", soup.title.string.strip() if soup.title and soup.title.string else None)

rows = soup.select(site["selectors"]["row"])
print("   Найдено строк по селектору 'row':", len(rows))

links = soup.select(site["selectors"]["topic_link"])
print("   Найдено ссылок по селектору 'topic_link':", len(links))

# ищем явные признаки проблем
lowered = html.lower()
for marker in ["неверн", "ошибка", "доступ закрыт", "подтвер", "забанен", "заблокирован",
               "скоро", "технические работы", "antibot", "denied", "captcha", "капч"]:
    if marker in lowered:
        idx = lowered.find(marker)
        print(f"   Найдено слово-маркер '{marker}' рядом: ...{html[max(0,idx-60):idx+120]}...")

if scraper._looks_like_antibot_cloud_challenge(html):
    print("\n3) Это JS-прослойка Antibot Cloud - пробуем пройти её автоматически (без браузера)...")
    solved = scraper._solve_antibot_cloud(site, html, url)
    print("   _solve_antibot_cloud() вернул:", solved)
    if solved:
        print("   Перезапрашиваем раздел ещё раз с новой cookie...")
        soup2 = scraper.fetch_page_soup(site_id, url)
        rows2 = soup2.select(site["selectors"]["row"])
        links2 = soup2.select(site["selectors"]["topic_link"])
        print("   После прохождения антибота: строк 'row' =", len(rows2),
              ", ссылок 'topic_link' =", len(links2))
        with open(f"debug_forum_{site_id}_after_antibot.html", "w", encoding=site["encoding"], errors="ignore") as f:
            f.write(str(soup2))
        print(f"   Итоговый HTML сохранён в debug_forum_{site_id}_after_antibot.html")

