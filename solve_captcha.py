
# -*- coding: utf-8 -*-
"""
Ручное решение визуальной капчи Antibot Cloud ("кликните похожую картинку") на xxxtorrent.
Нужна, когда автоматическая проверка (внутри scraper._solve_antibot_cloud) не прошла.

Шаг 1 - подготовка (скачивает эталон + 6 картинок-вариантов в antibot_captcha/):
    python solve_captcha.py prepare <URL_страницы_раздела_или_темы>

  Пришлите мне файлы antibot_captcha/reference.png и antibot_captcha/candidate_1..6.jpg -
  я скажу номер правильного варианта (тот, что ПОХОЖ по смыслу на эталон).

Шаг 2 - отправка ответа (N - номер картинки, который я назвал, 1..6):
    python solve_captcha.py answer N

  При успехе cookie антибота обновится и сохранится в forum_cookies_xxxtorrent.txt -
  дальше scraper будет работать как обычно, пока эта cookie не протухнет (~7 дней).
"""

import sys
import scraper

if len(sys.argv) < 2 or sys.argv[1] not in ("prepare", "answer"):
    print(__doc__)
    sys.exit(1)

cmd = sys.argv[1]

if cmd == "prepare":
    if len(sys.argv) < 3:
        print("Нужен URL: python solve_captcha.py prepare <URL>")
        sys.exit(1)
    url = sys.argv[2]
    scraper.login("xxxtorrent")
    result = scraper.prepare_antibot_captcha("xxxtorrent", url)
    if result.get("solved"):
        print("Антибот не потребовался / прошёл сам:", result.get("note"))
    else:
        print("Капча сохранена в папку:", result["out_dir"])
        print("Эталон:", result["reference"])
        print("Варианты (в порядке показа на странице, 1..%d):" % len(result["candidates"]))
        for i, path in enumerate(result["candidates"], start=1):
            print(f"  {i}: {path}")
        print("\nПришлите мне все эти файлы - я назову номер правильного варианта.")
        print("Затем запустите: python solve_captcha.py answer <НОМЕР>")

elif cmd == "answer":
    if len(sys.argv) < 3 or not sys.argv[2].isdigit():
        print("Нужен номер: python solve_captcha.py answer <НОМЕР>")
        sys.exit(1)
    idx = int(sys.argv[2])
    ok = scraper.submit_antibot_captcha("xxxtorrent", idx)
    print("Результат:", "УСПЕХ, cookie обновлена" if ok else "НЕ ПРИНЯТО сервером")
