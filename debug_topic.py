
# -*- coding: utf-8 -*-
"""
Диагностика ОДНОЙ темы с текущим логином - смотрим, как выглядит блок скачивания,
теги и превью у авторизованного пользователя. Ничего не пишет в базу.

Запуск: python debug_topic.py [ID_ТЕМЫ] [site_id]
  site_id - "pornolab" (по умолчанию) или "xxxtorrent" (клон), см. config.FORUM_SITES
  Для клона ID темы берите из её URL вида https://xxxtorrent.top/3292516-f.html -> 3292516
"""

import sys
import config
import scraper
from bs4 import BeautifulSoup

topic_id = sys.argv[1] if len(sys.argv) > 1 else "3313474"
site_id = sys.argv[2] if len(sys.argv) > 2 else "pornolab"
site = scraper._site(site_id)

topic_url = site["topic_url_template"].format(base=site["base_url"].rstrip("/"), id=topic_id)

print(f"Источник: {site_id}, тема: {topic_id}")
print("Логинимся...")
scraper.login(site_id)
sess = scraper.get_forum_session(site_id)
print("Cookies:", scraper.cookie_names(site_id))

print(f"\nЗапрашиваем тему {topic_url}")
resp = sess.get(topic_url, timeout=30)
resp.encoding = site["encoding"]
html = resp.text
print("Длина HTML:", len(html))

out_file = f"debug_topic_{site_id}.html"
with open(out_file, "w", encoding=site["encoding"], errors="ignore") as f:
    f.write(html)
print(f"Сохранено в {out_file} - этот файл можно прислать мне целиком, если селекторы не совпадут")

if scraper._looks_like_antibot_cloud_challenge(html):
    print("\nЭто JS-прослойка Antibot Cloud - пробуем пройти её автоматически (без браузера)...")
    solved = scraper._solve_antibot_cloud(site, html, topic_url)
    print("_solve_antibot_cloud() вернул:", solved)
    if solved:
        print("Перезапрашиваем тему ещё раз с новой cookie...")
        resp = sess.get(topic_url, timeout=30)
        resp.encoding = site["encoding"]
        html = resp.text
        print("Длина HTML после прохождения антибота:", len(html))
        with open(f"debug_topic_{site_id}_after_antibot.html", "w", encoding=site["encoding"], errors="ignore") as f:
            f.write(html)
        print(f"Сохранено в debug_topic_{site_id}_after_antibot.html")

soup = BeautifulSoup(html, "html.parser")
selectors = site["selectors"]

print("\nТекущий селектор attach_link:", repr(selectors["attach_link"]))
attach = soup.select_one(selectors["attach_link"])
print("select_one(attach_link) нашёл:", attach)

all_dl_stub = soup.select("a.dl-stub")
print("\nВсего a.dl-stub на странице:", len(all_dl_stub))
for a in all_dl_stub:
    print("  class=", a.get("class"), "href=", a.get("href"), "text=", a.get_text(strip=True))

all_dl_link = soup.select("a.dl-link")
print("\nВсего a.dl-link на странице:", len(all_dl_link))
for a in all_dl_link:
    print("  class=", a.get("class"), "href=", a.get("href"), "text=", a.get_text(strip=True))

import re
m = re.search(r'<a[^>]+href="([^"]*dl[^"]*\.php\?t=\d+)"[^>]*>', html)
print("\nRegex по сырому HTML нашёл href с dl.php (формат pornolab):", m.group(1) if m else None)

# на клоне (xxxtorrent) ссылка на сам .torrent ведёт на ДРУГОЙ домен
# (cdncontent.top/opentorrent/torrent.php?id=...), а не на dl.php - ищем и такой вариант
m2 = re.search(r'href="([^"]*torrent\.php\?id=\d+[^"]*)"', html)
print("Regex по сырому HTML нашёл href с torrent.php?id= (формат xxxtorrent):", m2.group(1) if m2 else None)
m3 = re.findall(r'href="(https?://[^"]*(?:cdncontent|opentorrent)[^"]*)"', html)
print("Все href, содержащие cdncontent/opentorrent:", m3 if m3 else None)

# покажем окрестности характерных слов в сыром HTML, чтобы увидеть реальную разметку
for marker in ["dl.php", "torrent.php", "cdncontent", "Скачать", "attach_link", "magnet:", "-f.html"]:
    idx = html.find(marker)
    if idx != -1:
        snippet = html[max(0, idx - 150):idx + 250]
        print(f"\n--- контекст вокруг '{marker}' ---\n{snippet}")
    else:
        print(f"\n--- '{marker}' не найдено в HTML ---")

tags = soup.select(selectors["tags_list"])
print("\nТегов найдено:", len(tags), [a.get_text(strip=True) for a in tags[:10]])

post_body = soup.select_one(selectors["post_body"])
print("\npost_body найден:", post_body is not None)
post_message = post_body.select_one(selectors["post_message"]) if post_body else None
print("post_message найден:", post_message is not None)

if post_message:
    imgs = post_message.select(selectors["preview_img"])
    print("preview_img найдено:", len(imgs), [img.get("title", "")[:80] for img in imgs[:3]])
