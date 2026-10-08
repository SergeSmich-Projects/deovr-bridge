
# -*- coding: utf-8 -*-
"""
Парсер раздела(ов) форумов движка rutracker-family. Поддерживает НЕСКОЛЬКО
независимых источников одновременно (см. config.FORUM_SITES) - например,
основной форум PornoLab.Net и его "клон" на другом домене. У каждого
источника своя сессия, свои cookie (своя авторизация/antibot-защита) и свои
селекторы/регулярки под конкретную вёрстку URL, но общий алгоритм обхода.

Проверено на реальной верстке PornoLab.Net (сайт "pornolab" в FORUM_SITES):
  - список раздела: viewforum.php?f=1823, пагинация &start=N
  - тема: viewtopic.php?t=ID
  - теги раздачи: #tor-tags-list a (tracker.php?tg=...)
  - превью: <var class="postImg" title="URL">  (картинка в атрибуте title!)
  - метаданные поста: <span class="post-b">Жанр</span>: значение<br/>
  - скачивание: fieldset.attach h1.attach_link a -> dl.php?t=ID (нужен залогиненный cookie)

Сайт "xxxtorrent" (клон) использует тот же движок, но "человеческие" URL
(/123456-f.html, /page/N/) и собственную Antibot Cloud защиту поверх обычной
сессионной куки - см. комментарии у config.FORUM_SITES.
"""

import os
import re
import time
import logging
import threading
import urllib.parse

import requests
from bs4 import BeautifulSoup

import config
import store

log = logging.getLogger("scraper")

# Множитель "страница -> бакет ранга" для сортировки библиотеки В ТОЧНОСТИ как на
# трекере (см. items.source_rank в store.init_db()). Должен быть заведомо больше
# максимального числа раздач на одной странице списка (у pornolab их 50).
_RANK_PAGE_MULT = 1000
# Для источников с непрозрачным курсором пагинации (xxxclub) - "быстрый" проход
# (самая свежая страница) всегда получает маленький ранг (0..N), архивный обход
# вглубь - ранги начиная с этой базы (заведомо больше, чем максимум быстрого
# прохода, см. config.XXXCLUB_QUICK_SCAN_MAX_ITEMS).
_XXXCLUB_BACKFILL_RANK_BASE = 1_000_000

_DEGREES_RE = re.compile(r"\b(180|360)\b\s*°?")
_BRACKET_TAGS_RE = re.compile(r"\[([^\[\]]{2,60})\]")

# --- эвристика студии/актрисы из заголовка (для HereSphere: категории Studio/@Performer) ---
# Типичные форматы в этом разделе:
#   "[Studio1 / Studio2] Имя Фамилия (Название сцены) [2026, теги...]"
#   "[Studio] Имя Фамилия - Название сцены [теги...]"
_LEADING_BRACKET_RE = re.compile(r"^\[([^\[\]]+)\]\s*")
_PERFORMER_CHUNK_RE = re.compile(r"^(.*?)(?:\s[-–]\s|\s*\(|\s*\[|$)")
_PERFORMER_NAME_RE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ'’.\- ]{2,40}$")

_STEREO_MAP = {
    "sidebyside": "sbs",
    "side by side": "sbs",
    "sbs": "sbs",
    "topbottom": "tb",
    "top bottom": "tb",
    "overunder": "tb",
    "over under": "tb",
    "tb": "tb",
}

# --- реестр источников (по config.FORUM_SITES) и их состояние в рантайме ------------

_sites_by_id = {site["id"]: site for site in config.FORUM_SITES}
_sessions = {}          # site_id -> requests.Session
_cookie_lock = threading.Lock()
_last_persisted_cookie_str = {}  # site_id -> str


def _site(site_id: str) -> dict:
    if site_id not in _sites_by_id:
        raise KeyError(f"Неизвестный источник '{site_id}' - проверьте config.FORUM_SITES")
    return _sites_by_id[site_id]


def _session_for(site_id: str) -> requests.Session:
    sess = _sessions.get(site_id)
    if sess is None:
        impersonate = _site(site_id).get("impersonate")
        if impersonate:
            # сайт стоит за антибот-защитой, которая проверяет TLS/HTTP2-"отпечаток"
            # запроса - обычный requests тут не пройдёт даже с правильными cookie.
            # curl_cffi подделывает отпечаток под реальный браузер (libcurl-impersonate).
            try:
                from curl_cffi import requests as curl_requests
            except ImportError as e:
                raise RuntimeError(
                    f"[{site_id}] Для этого источника нужна библиотека curl_cffi "
                    f"(обходит антибот-защиту по TLS-отпечатку). Установите: "
                    f"pip install curl_cffi"
                ) from e
            sess = curl_requests.Session(impersonate=impersonate)
            # UA НЕ переопределяем - curl_cffi сам подставляет UA, согласованный
            # с выбранным TLS-отпечатком; рассинхрон UA/отпечатка сам по себе
            # может быть сигналом для антибота.
        else:
            sess = requests.Session()
            sess.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            })
        _sessions[site_id] = sess
    return sess


def get_forum_session(site_id: str = "pornolab"):
    return _session_for(site_id)


def _cookie_objs(sess) -> list:
    """
    Список cookie-объектов с атрибутами .name/.value - одинаково работает и для
    обычной requests.Session (там sess.cookies сам по себе - jar из таких объектов),
    и для curl_cffi Session (там sess.cookies - dict-like обёртка, а настоящие
    объекты с .name/.value лежат в sess.cookies.jar).
    """
    cookies = sess.cookies
    jar = getattr(cookies, "jar", None)
    return list(jar) if jar is not None else list(cookies)


def cookie_names(site_id: str) -> list:
    """Для debug-скриптов: имена текущих cookie сессии источника."""
    return [c.name for c in _cookie_objs(_session_for(site_id))]


def _load_manual_cookies(site: dict) -> bool:
    """
    Если у источника задан cookie_file и в нём есть содержимое - используем готовую,
    залогиненную/прошедшую антибот вручную в браузере сессию. Файл должен содержать
    ОДНУ строку вида "name1=value1; name2=value2; ...", скопированную из заголовка
    Cookie в DevTools браузера (ТОЛЬКО куки нужного домена - куки сторонних доменов,
    типа счётчиков статистики, включать не нужно).
    """
    path = site.get("cookie_file", "")
    sess = _session_for(site["id"])
    if not path:
        log.info("[%s] cookie_file не задан в config.py.", site["id"])
        return False
    if not os.path.exists(path):
        log.info("[%s] Файл с cookies не найден: %s (проверьте точный путь/имя файла).", site["id"], path)
        return False

    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        raw = f.read().strip()

    if not raw:
        log.warning("[%s] Файл с cookies %s пустой.", site["id"], path)
        return False

    host = urllib.parse.urlparse(site["base_url"]).hostname
    count = 0
    skipped = 0
    for part in raw.replace("\n", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            skipped += 1
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            sess.cookies.set(k, v, domain=host)
            count += 1

    if count:
        log.info("[%s] Загружено %s cookie(s) из %s: %s", site["id"], count, path,
                  [c.name for c in _cookie_objs(sess)])
        return True

    log.warning("[%s] В файле %s не нашлось ни одной пары name=value (прочитано %s символов, "
                "фрагментов без '=': %s). Содержимое должно быть вида "
                "'bb_data=...; bb_tz=...; plm=1'. Первые 120 символов файла: %r",
                site["id"], path, len(raw), skipped, raw[:120])
    return False


def login(site_id: str):
    """Логинимся на форум (нужно для скачивания .torrent). Форма в кодировке источника."""
    site = _site(site_id)
    if _load_manual_cookies(site):
        return True

    if not site.get("username"):
        log.info("[%s] username не задан - работаем как гость (список доступен, скачивание - нет).", site_id)
        return False

    sess = _session_for(site_id)
    fields = {
        site["login_field_user"]: site["username"],
        site["login_field_pass"]: site["password"],
        site["login_submit_field"]: site["login_submit_value"],
    }
    fields.update(site.get("login_extra_fields", {}))

    # кодируем тело вручную в кодировке источника, т.к. форма может быть не в utf-8
    body = urllib.parse.urlencode(fields).encode(site["encoding"], errors="ignore")
    headers = {"Content-Type": f"application/x-www-form-urlencoded; charset={site['encoding']}"}

    resp = sess.post(site["login_url"], data=body, headers=headers, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    resp.encoding = site["encoding"]

    ok = any(c.name.lower().startswith("bb_") or "uid" in c.name.lower() for c in _cookie_objs(sess))
    if ok:
        log.info("[%s] Логин выполнен успешно, куки: %s", site_id, [c.name for c in _cookie_objs(sess)])
        return ok

    text = resp.text
    reason = "неизвестно (см. login_debug.html)"
    lowered = text.lower()
    m = re.search(r'<h4[^>]*>(.*?)</h4>', text, re.S)
    if m:
        reason = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    elif "капч" in lowered or "captcha" in lowered:
        reason = "сайт требует капчу (автологином не обойти)"
    elif "robot" in lowered or "робот" in lowered:
        reason = "сайт считает запрос ботом"

    try:
        with open(f"login_debug_{site_id}.html", "w", encoding=site["encoding"], errors="ignore") as f:
            f.write(text)
    except OSError:
        pass

    log.warning("[%s] Логин не удался. Причина с сайта: %r. Куки: %s. Полный HTML-ответ сохранён в login_debug_%s.html",
                site_id, reason, [c.name for c in _cookie_objs(sess)], site_id)
    return ok


_ANTIBOT_MARKERS = ("antibot", "has been denied", "antibot-btn-success", "запрос отклонён")

# "Antibot Cloud" (используется на xxxtorrent) иногда вместо реального контента отдаёт
# страницу-прослойку: она выполняет JS-проверку (POST на /antibot/index.php с данными
# о "браузере"), получает в ответ cookie и САМА делает редирект на запрошенный URL.
# Без JS-движка эта проверка не выполняется - но, как видно по исходнику этой страницы,
# почти все значения ("test", "h1", "date", "cid", "ip", "country" и т.п.) сервер уже
# вписал в HTML как обычный текст - вычисляются в браузере только второстепенные поля
# (размер экрана, часовой пояс). Поэтому можно собрать тот же POST прямо в Python,
# подставив вместо них правдоподобные константы - настоящий рендеринг JS не нужен.
_ANTIBOT_CLOUD_DATA_RE = re.compile(r"\bdata\s*=\s*(.+?);\r?\n", re.S)
_ANTIBOT_CLOUD_POST_RE = re.compile(r'cloud\.open\(\s*"POST"\s*,\s*"([^"]+)"')
_ANTIBOT_CLOUD_SUFFIX_RE = re.compile(
    r"d\s*=\s*d\s*\+\s*'&(?P<f1>\w+)='\s*\+\s*s\s*\+\s*'&(?P<f2>\w+)='\s*\+\s*x\s*\+\s*'&rowid=4&gray=2';"
)
_ANTIBOT_CLOUD_COOKIE_RE = re.compile(
    r'document\.cookie\s*=\s*"(?P<name>\w+)="\s*\+\s*obj\.cookie\s*\+\s*"-(?P<date>\d+)'
)
# подмена JS-выражений, которые реально вычисляются в браузере, на правдоподобные
# константы обычного десктопного Chrome - сервер явно не сверяет их жёстко (это лишь
# вспомогательные метрики, а не крипто-доказательство), все "секретные" значения
# (test/h1/date/cid/ip/country) уже зашиты сервером в саму страницу как текст
_ANTIBOT_CLOUD_JS_SUBS = [
    (re.compile(r"screen\.width"), "'1920'"),
    (re.compile(r"screen\.height"), "'1080'"),
    (re.compile(r"document\.documentElement\.clientWidth"), "'1920'"),
    (re.compile(r"document\.documentElement\.clientHeight"), "'950'"),
    (re.compile(r"screen\.colorDepth"), "'24'"),
    (re.compile(r"screen\.pixelDepth"), "'24'"),
    (re.compile(r"encodeURIComponent\(document\.referrer\)"), "''"),
    (re.compile(r"Intl\.DateTimeFormat\(\)\.resolvedOptions\(\)\.timeZone"), "'Europe/Moscow'"),
    # эти переменные - голые JS-идентификаторы МЕЖДУ '+' (конкатенация) - заменяем
    # только такое вхождение, а не одноимённую подстроку внутри кавычек (там это
    # часть литерала вида "&ipdbc=", которую трогать нельзя)
    (re.compile(r"(?<=\+)adb(?=\+)"), "'1'"),
    (re.compile(r"(?<=\+)ipdbc(?=\+)"), "''"),
    (re.compile(r"(?<=\+)ipv4(?=\+)"), "''"),
    (re.compile(r"(?<=\+)rct(?=\+)"), "''"),
    (re.compile(r"(?<=\+)cookieoff(?=\+|;|$)"), "'0'"),
]
_SAFE_JS_EXPR_RE = re.compile(r"^(?:\s*'[^']*'\s*\+?)+$")

# визуальная капча ("кликните на похожую картинку") - показывается, когда авто-проверка
# не прошла. Эталон - картинка base64 прямо в HTML, варианты - 6 внешних картинок,
# у каждой в onclick зашит "токен ответа", который нужно отправить обратно, если
# кликнули по НЕЙ. Сами картинки отдаются без проверки антибота - их можно скачать
# напрямую. Распознавание делает человек (или модель с "глазами") один раз за решение.
_ANTIBOT_CLOUD_REF_IMG_RE = re.compile(r'data:image/(png|jpe?g);base64,([A-Za-z0-9+/=]+)')
_ANTIBOT_CLOUD_CANDIDATE_RE = re.compile(
    r'<img[^>]+src="([^"]+)"[^>]*onclick="\w+\(\'post\',\s*data,\s*\'([^\']*)\'\)"'
)


def _extract_antibot_cloud_fields(html: str):
    """Общий разбор страницы-прослойки Antibot Cloud. Возвращает dict с собранной
    строкой data, URL проверки, именами полей и cookie, либо None, если структура
    страницы не распознана (сайт изменил шаблон - нужно разбираться заново)."""
    m_data = _ANTIBOT_CLOUD_DATA_RE.search(html)
    m_post = _ANTIBOT_CLOUD_POST_RE.search(html)
    m_suffix = _ANTIBOT_CLOUD_SUFFIX_RE.search(html)
    m_cookie = _ANTIBOT_CLOUD_COOKIE_RE.search(html)
    if not (m_data and m_post and m_suffix and m_cookie):
        return None

    js_expr = m_data.group(1)
    for pattern, repl in _ANTIBOT_CLOUD_JS_SUBS:
        js_expr = pattern.sub(repl, js_expr)
    if not _SAFE_JS_EXPR_RE.match(js_expr):
        return None
    try:
        data_str = eval(js_expr, {"__builtins__": {}})  # noqa: S307 - вход уже проверен regex выше
    except Exception:
        return None

    return {
        "data_str": data_str,
        "post_path": m_post.group(1),
        "suffix_f1": m_suffix.group("f1"),
        "suffix_f2": m_suffix.group("f2"),
        "cookie_name": m_cookie.group("name"),
        "cookie_date": m_cookie.group("date"),
    }


def _apply_antibot_cookie(site: dict, fields: dict, cookie_val: str):
    host = urllib.parse.urlparse(site["base_url"]).hostname
    full_value = f"{cookie_val}-{fields['cookie_date']}"
    _session_for(site["id"]).cookies.set(fields["cookie_name"], full_value, domain=host)


def _solve_antibot_cloud(site: dict, html: str, page_url: str) -> bool:
    """
    Пытается пройти JS-прослойку Antibot Cloud без браузера - собирает тот же POST,
    что обычно отправляет её JS-проверка, из значений, уже вписанных сервером в HTML.
    Возвращает True, если получилось получить и выставить валидную cookie.
    """
    site_id = site["id"]
    fields = _extract_antibot_cloud_fields(html)
    if not fields:
        log.warning("[%s] Страница антибота не похожа на ожидаемый шаблон (JS-структура "
                    "не найдена) - автообход невозможен, нужен headless-браузер.", site_id)
        return False

    suffix = f"&{fields['suffix_f1']}=ab&{fields['suffix_f2']}=&rowid=4&gray=2"
    post_url = _abs_url(site, fields["post_path"])

    sess = _session_for(site_id)
    try:
        resp = sess.post(
            post_url,
            data=(fields["data_str"] + suffix).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded;",
                "Referer": page_url,
            },
            timeout=20,
        )
        resp.raise_for_status()
        obj = resp.json()
    except Exception as e:
        log.warning("[%s] Запрос проверки антибота не удался: %s", site_id, e)
        return False

    cookie_val = obj.get("cookie")
    if isinstance(cookie_val, str):
        _apply_antibot_cookie(site, fields, cookie_val)
        log.info("[%s] Антибот пройден автоматически, cookie %s обновлена.",
                  site_id, fields["cookie_name"])
        return True

    err = obj.get("error")
    if err:
        log.warning("[%s] Антибот отклонил авто-проверку для %s: %r "
                    "(похоже, тут нужна визуальная капча или это ограничение подписки "
                    "самого сайта на антибот-сервис, а не проблема наших cookies).",
                    site_id, page_url, err)
    else:
        log.warning("[%s] Антибот не вернул cookie в ответе: %r", site_id, obj)
    return False


def prepare_antibot_captcha(site_id: str, url: str) -> dict:
    """
    Для ручного решения визуальной капчи Antibot Cloud ("кликните похожую картинку").
    Скачивает страницу, сохраняет эталонную картинку и все варианты-кандидаты в папку
    antibot_captcha/, вместе со служебными данными, нужными чтобы потом отправить ответ
    (см. submit_antibot_captcha). Возвращает dict с путями к файлам и их количеством,
    либо {"solved": True}, если антибот вообще не потребовался/прошёл сам.
    """
    import json

    site = _site(site_id)
    soup_or_none = None
    try:
        soup_or_none = _get(site, url)
        return {"solved": True, "note": "Антибот не потребовался или был пройден автоматически."}
    except RuntimeError:
        pass

    sess = _session_for(site_id)
    resp = sess.get(url, timeout=30)
    resp.encoding = site["encoding"]
    html = resp.text

    fields = _extract_antibot_cloud_fields(html)
    if not fields:
        raise RuntimeError("Страница антибота не распознана - шаблон мог измениться.")

    # сама капча собирается через innerHTML = "<img ... onclick=\"...\" />" внутри JS-строки,
    # поэтому кавычки в разметке экранированы (\" и \') - для поиска проще их снять
    unescaped = html.replace('\\"', '"').replace("\\'", "'")
    m_ref = _ANTIBOT_CLOUD_REF_IMG_RE.search(unescaped)
    candidates = _ANTIBOT_CLOUD_CANDIDATE_RE.findall(unescaped)
    if not m_ref or not candidates:
        raise RuntimeError("Не нашёл картинки капчи на странице - шаблон мог измениться.")

    out_dir = os.path.join(os.path.dirname(__file__), "antibot_captcha")
    os.makedirs(out_dir, exist_ok=True)

    ext, b64 = m_ref.group(1), m_ref.group(2)
    ref_path = os.path.join(out_dir, f"reference.{ 'jpg' if ext in ('jpg','jpeg') else 'png' }")
    with open(ref_path, "wb") as f:
        import base64
        f.write(base64.b64decode(b64))

    saved = []
    for i, (src, token) in enumerate(candidates, start=1):
        img_url = _abs_url(site, src)
        try:
            r = sess.get(img_url, timeout=20)
            r.raise_for_status()
        except Exception as e:
            log.warning("[%s] Не удалось скачать картинку-кандидат %s: %s", site_id, img_url, e)
            continue
        cand_path = os.path.join(out_dir, f"candidate_{i}.jpg")
        with open(cand_path, "wb") as f:
            f.write(r.content)
        saved.append(cand_path)

    pending = {
        "url": url,
        "fields": fields,
        "tokens": [tok for _src, tok in candidates],
    }
    with open(os.path.join(out_dir, "pending.json"), "w", encoding="utf-8") as f:
        json.dump(pending, f, ensure_ascii=False)

    return {
        "solved": False,
        "reference": ref_path,
        "candidates": saved,
        "out_dir": out_dir,
    }


def submit_antibot_captcha(site_id: str, answer_index: int) -> bool:
    """Отправляет ответ (1..N) на капчу, подготовленную prepare_antibot_captcha(),
    и при успехе выставляет валидную cookie антибота."""
    import json

    site = _site(site_id)
    out_dir = os.path.join(os.path.dirname(__file__), "antibot_captcha")
    pending_path = os.path.join(out_dir, "pending.json")
    with open(pending_path, "r", encoding="utf-8") as f:
        pending = json.load(f)

    fields = pending["fields"]
    tokens = pending["tokens"]
    if not (1 <= answer_index <= len(tokens)):
        raise ValueError(f"Номер должен быть от 1 до {len(tokens)}")
    token = tokens[answer_index - 1]

    suffix = f"&{fields['suffix_f1']}=post&{fields['suffix_f2']}={urllib.parse.quote(token)}&rowid=4&gray=2"
    post_url = _abs_url(site, fields["post_path"])
    sess = _session_for(site_id)
    resp = sess.post(
        post_url,
        data=(fields["data_str"] + suffix).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded;",
            "Referer": pending["url"],
        },
        timeout=20,
    )
    resp.raise_for_status()
    obj = resp.json()

    cookie_val = obj.get("cookie")
    if isinstance(cookie_val, str):
        _apply_antibot_cookie(site, fields, cookie_val)
        persist_cookies(site_id)
        log.info("[%s] Капча решена, cookie %s обновлена и сохранена.", site_id, fields["cookie_name"])
        return True

    log.warning("[%s] Капча не принята, ответ сервера: %r", site_id, obj)
    return False


def _looks_like_antibot_cloud_challenge(text: str) -> bool:

    return "/antibot/index.php" in text and "data = '" in text


def _get(site: dict, url: str) -> BeautifulSoup:
    sess = _session_for(site["id"])
    resp = sess.get(url, timeout=30)
    resp.raise_for_status()
    resp.encoding = site["encoding"]
    text = resp.text

    if _looks_like_antibot_cloud_challenge(text):
        if _solve_antibot_cloud(site, text, url):
            resp2 = sess.get(url, timeout=30)
            resp2.raise_for_status()
            resp2.encoding = site["encoding"]
            text2 = resp2.text
            if not _looks_like_antibot_cloud_challenge(text2):
                return BeautifulSoup(text2, "html.parser")
        raise RuntimeError(
            f"[{site['id']}] Не удалось автоматически пройти антибот-проверку для {url}. "
            f"Нужен headless-браузер (Playwright) или свежая cookie-сессия из браузера."
        )

    low = text.lower()
    if len(text) < 4000 and any(marker in low for marker in _ANTIBOT_MARKERS):
        # похоже на страницу антибот-защиты (капча/отказ), а не на реальный контент -
        # дальше парсить бессмысленно, явно сообщаем об этом, а не молча возвращаем 0 тем
        raise RuntimeError(
            f"[{site['id']}] Сайт вернул страницу антибот-защиты вместо контента "
            f"({url}) - вероятно, cookie-сессия устарела. Нужно заново пройти проверку "
            f"в обычном браузере и обновить {site.get('cookie_file')}."
        )
    return BeautifulSoup(text, "html.parser")


def fetch_page_soup(site_id: str, url: str) -> BeautifulSoup:
    """Публичная обёртка над _get() - для debug-скриптов (чтобы они тоже проходили
    через автообход антибот-прослойки, а не просто падали на сырой странице)."""
    return _get(_site(site_id), url)


def persist_cookies(site_id: str = None):
    """
    Сохраняет ТЕКУЩИЕ cookies сессии источника (включая всё, что сервер успел
    обновить через Set-Cookie за время работы) обратно в её cookie_file. Если
    site_id не задан - делает это для ВСЕХ известных источников сразу.
    """
    if site_id is None:
        for sid in _sites_by_id:
            persist_cookies(sid)
        return

    site = _site(site_id)
    path = site.get("cookie_file", "")
    if not path:
        return
    sess = _session_for(site_id)
    with _cookie_lock:
        cookie_str = "; ".join(f"{c.name}={c.value}" for c in _cookie_objs(sess))
        if not cookie_str or cookie_str == _last_persisted_cookie_str.get(site_id):
            return  # ничего не изменилось - не дёргаем диск зря
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(cookie_str)
            _last_persisted_cookie_str[site_id] = cookie_str
            log.info("[%s] Cookies обновлены и сохранены в %s", site_id, path)
        except OSError as e:
            log.warning("[%s] Не удалось сохранить обновлённые cookies в %s: %s", site_id, path, e)


def touch_topic(site_id: str, topic_id: str):
    """
    Заходит на страницу темы ЕЩЁ РАЗ той же сессией прямо перед скачиванием .torrent.
    На движках семейства rutracker при просмотре темы форум ставит анти-лич куку
    (у pornolab это 'bb_dl') со значением ID последней открытой темы, и dl.php сверяет
    её с запрашиваемым t=ID. Если скачивание происходит намного позже обхода (а в нашем
    случае так и есть - карточка открывается в VR спустя время после краулинга, который
    уже успел уйти на другие страницы архива), кука будет указывать на другую тему, и
    dl.php вместо файла вернёт страницу-предупреждение. Поэтому перед каждым резолвом
    потока "освежаем" именно нужную тему.
    """
    site = _site(site_id)
    try:
        url = site["topic_url_template"].format(base=site["base_url"].rstrip("/"), id=topic_id)
        _session_for(site_id).get(url, timeout=20)
    except Exception as e:
        log.warning("[%s] Не удалось обновить анти-лич cookie для темы %s: %s", site_id, topic_id, e)


def _abs_url(site: dict, href: str) -> str:
    if href.startswith("http"):
        return href
    return urllib.parse.urljoin(site["base_url"].rstrip("/") + "/", href.lstrip("./"))


def _parse_duration_to_seconds(text: str) -> int:
    m = re.search(r"(\d+):(\d{2}):(\d{2})", text)
    if m:
        h, mi, s = map(int, m.groups())
        return h * 3600 + mi * 60 + s
    m = re.search(r"(\d+):(\d{2})", text)
    if m:
        mi, s = map(int, m.groups())
        return mi * 60 + s
    return 0


def _extract_meta_fields(post_message, selectors) -> dict:
    """Парсит пары 'Жанр: значение' и т.п. из тела поста."""
    meta = {}
    if not post_message:
        return meta
    for span in post_message.select(selectors["meta_label"]):
        label = span.get_text(strip=True).rstrip(":").strip().lower()
        value_parts = []
        node = span.next_sibling
        # собираем текст до следующего тега (обычно это и есть значение поля)
        while node is not None and getattr(node, "name", None) is None:
            value_parts.append(str(node))
            node = node.next_sibling
        value = "".join(value_parts).lstrip(":").strip()
        if label and value:
            meta[label] = value
    return meta


def extract_studio_and_performers(title: str) -> tuple:
    """
    ЭВРИСТИКА (не 100% точная!) - у форума нет отдельного поля "актриса", имя
    обычно идёт в заголовке сразу после первой [квадратной скобки-студии].
    Используется только для доп. категорий в HereSphere - на парсинг тегов/
    ссылки на скачивание никак не влияет, в худшем случае просто не найдёт
    актрису/студию для конкретного заголовка.
    """
    if not getattr(config, "ENABLE_PERFORMER_PARSING", True):
        return [], []

    studios = []
    rest = title
    m = _LEADING_BRACKET_RE.match(title)
    if m:
        rest = title[m.end():]
        for part in re.split(r"\s*/\s*", m.group(1)):
            part = part.strip(" .")
            if part and len(part) <= 50:
                studios.append(part)

    performers = []
    pm = _PERFORMER_CHUNK_RE.match(rest)
    if pm:
        chunk = pm.group(1).strip(" .")
        if chunk and len(chunk) <= 60 and not chunk.startswith("["):
            for part in re.split(r"\s*(?:,|&|\band\b)\s*", chunk, flags=re.I):
                part = part.strip(" .")
                if _PERFORMER_NAME_RE.match(part) and part[0].isupper():
                    performers.append(part)

    return studios, performers


def _guess_screen_stereo(title: str, tags: list, meta: dict) -> tuple:
    hay_title = title or ""
    stereo = config.DEFAULT_STEREO_MODE
    screen = config.DEFAULT_SCREEN_TYPE

    stereo_raw = ""
    for key in meta:
        if "3d" in key or "тип 3d" in key:
            stereo_raw = meta[key]
            break
    norm = re.sub(r"[\s_-]", "", stereo_raw.lower())
    if norm in _STEREO_MAP:
        stereo = _STEREO_MAP[norm]
    elif re.search(r"\btb\b|top.?bottom|overunder", hay_title, re.I):
        stereo = "tb"
    elif re.search(r"\bsbs\b|side.?by.?side", hay_title, re.I):
        stereo = "sbs"

    m = _DEGREES_RE.search(hay_title)
    if m:
        screen = "sphere" if m.group(1) == "360" else "dome"
    elif "fisheye" in hay_title.lower():
        screen = "fisheye"

    return screen, stereo


def parse_topic(site_id: str, topic_id: str, topic_url: str, title: str) -> dict:
    site = _site(site_id)
    selectors = site["selectors"]
    soup = _get(site, topic_url)

    post_body = soup.select_one(selectors["post_body"])
    post_message = post_body.select_one(selectors["post_message"]) if post_body else None

    native_tags = [a.get_text(strip=True) for a in soup.select(selectors["tags_list"]) if a.get_text(strip=True)]
    tags = sorted(dict.fromkeys(native_tags))  # сайт уже даёт нормальные теги - доверяем им в первую очередь

    if not tags:
        # запасной вариант (если у темы почему-то нет штатных тегов) - достаём из скобок заголовка,
        # делим только по запятой (студия/девайс через "/" остаются одной фразой)
        seen_lower = set()
        fallback = []
        for m in _BRACKET_TAGS_RE.findall(title):
            for part in m.split(","):
                part = part.strip(" .")
                if not part or len(part) > 40:
                    continue
                if re.fullmatch(r"\d{4}\s*г?\.?", part, re.I):
                    continue  # год выпуска - не тег
                key = part.lower()
                if key not in seen_lower:
                    seen_lower.add(key)
                    fallback.append(part)
        tags = fallback

    meta = _extract_meta_fields(post_message, selectors)

    thumb = ""
    screenshots = []
    if post_message:
        for img in post_message.select(selectors["preview_img"]):
            if img.find_parent(class_="sp-wrap") or img.find_parent(class_="sp-body"):
                continue  # это уже скриншот из спойлера, а не главное превью
            thumb = img.get("title", "").strip()
            if thumb:
                break

        # скриншоты из спойлера "Скриншоты" (div.sp-wrap) - часто в стерео-картинке
        # (та же сетка, что и у видео, например SideBySide) - картинка лежит в
        # атрибуте title у <var class="postImg">, как и у главного превью выше
        wrap_sel = selectors.get("screenshots_wrap")
        if wrap_sel:
            for wrap in post_message.select(wrap_sel):
                for var in wrap.select("var.postImg"):
                    url = (var.get("title") or "").strip()
                    if url and url not in screenshots:
                        screenshots.append(url)

    # "Тип видео" обычно содержит вертикальное разрешение, например "3072p" -
    # используется, чтобы отсеивать низкое разрешение (см. config.MIN_VIDEO_HEIGHT_PX)
    video_height = None
    for key, val in meta.items():
        if "тип видео" in key:
            m = re.search(r"(\d+)\s*p\b", val, re.I) or re.search(r"\d+", val)
            if m:
                video_height = int(m.group(1) if m.lastindex else m.group(0))
            break

    attach_a = soup.select_one(selectors["attach_link"])
    source_type, source = None, None
    if attach_a and attach_a.get("href"):
        href = attach_a["href"]
        if "profile.php" in href and "register" in href:
            log.warning("[%s] Тема %s: ссылка на скачивание недоступна без логина", site_id, topic_id)
        else:
            source_type, source = "torrent_url", _abs_url(site, href)

    video_length = 0
    for key, val in meta.items():
        if "продолжительность" in key or "duration" in key:
            video_length = _parse_duration_to_seconds(val)
            break

    screen, stereo = _guess_screen_stereo(title, tags, meta)
    studio, performers = extract_studio_and_performers(title)

    item_id = f"{site['id_prefix']}{topic_id}"

    return {
        "id": item_id,
        "topic_id": topic_id,
        "title": title,
        "tags": tags,
        "thumbnail_url": thumb,
        "source_type": source_type or "torrent_url",
        "source": source or "",
        "screen_type": screen,
        "stereo_mode": stereo,
        "video_length": video_length,
        "studio": studio,
        "performers": performers,
        "site": site_id,
        "screenshots": screenshots,
        "video_height": video_height,
    }


# ---------------------------------------------------------------------------
# Движок "xxxclub" (XXXClub.to) - отдельный торрент-каталог, НЕ форум семейства
# rutracker. Логин и антибот не нужны, но своя (курсорная) пагинация и ловушки
# для ботов в HTML - см. комментарии в config.py у XXXCLUB_*.
# ---------------------------------------------------------------------------

_XXXCLUB_DEGREES_RE = re.compile(r"\bVR\s*?(180|360)\b|\b(180|360)\b\s*°?", re.I)


def _guess_screen_stereo_xxxclub(title: str) -> tuple:
    t = title or ""
    screen = config.DEFAULT_SCREEN_TYPE
    stereo = config.DEFAULT_STEREO_MODE
    m = _XXXCLUB_DEGREES_RE.search(t)
    if m:
        deg = m.group(1) or m.group(2)
        screen = "sphere" if deg == "360" else "dome"
    if re.search(r"\bTB\b|top.?bottom|over.?under", t, re.I):
        stereo = "tb"
    elif re.search(r"\bSBS\b|side.?by.?side", t, re.I):
        stereo = "sbs"
    return screen, stereo


def _xxxclub_listing_url(site: dict, cursor: str = None) -> str:
    base = site["base_url"].rstrip("/")
    path = site["category_path"]
    if not path.startswith("/"):
        path = "/" + path
    path = path.rstrip("/")
    if cursor:
        return f"{base}{path}/{cursor}"
    return f"{base}{path}/"


def _xxxclub_parse_listing(soup: BeautifulSoup) -> list:
    """Разбирает одну страницу списка (раздачи, НЕ блок 'Recommended' вверху -
    у него другая вёрстка, этим селектором не захватывается)."""
    items = []
    container = soup.select_one("div.browsetableinside")
    if not container:
        return items
    for li in container.select("ul > li"):
        if not li.find("span", class_="catlabe"):
            continue  # строка-заголовок таблицы ("Category | Name | ..."), не раздача
        a = li.find("a", href=re.compile(r"^/torrents/details/"))
        if not a:
            continue  # на всякий случай - вдруг это одна из "ловушечных" ссылок
        img = li.find("img", class_="floaterimg")
        size_span = li.find("span", class_="siz")
        items.append({
            "path": a.get("href", ""),
            "title": a.get_text(strip=True),
            "thumb": img.get("src", "") if img else "",
            "size_text": size_span.get_text(strip=True) if size_span else "",
        })
    return items


def _xxxclub_next_cursor(soup: BeautifulSoup) -> str:
    """Настоящая ссылка 'Next Page' - единственная БЕЗ стиля, прячущего её за
    экран (position:absolute; left:-99999px). Остальные - ловушки для ботов,
    ведущие на отдельный 'honeypot'-URL (у всех одинаковый)."""
    for a in soup.find_all("a", title="Next Page"):
        style = (a.get("style") or "").replace(" ", "")
        if "-99999" in style:
            continue
        href = a.get("href", "")
        m = re.search(r"/torrents/browse/\d+/([^/\"'?]+)", href)
        if m:
            return m.group(1)
    return None


def _xxxclub_parse_details(site: dict, detail_path: str) -> dict:
    url = _abs_url(site, detail_path)
    soup = _get(site, url)

    magnet = ""
    a_magnet = soup.find("a", href=re.compile(r"^magnet:"))
    if a_magnet:
        magnet = a_magnet["href"]

    collection = ""
    a_coll = soup.find("a", href=re.compile(r"/torrents/search/.*tag:"))
    if a_coll:
        collection = a_coll.get_text(strip=True)

    poster = ""
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        poster = og["content"]

    return {"magnet": magnet, "collection": collection, "poster": poster}


def _xxxclub_item_id(site: dict, path: str) -> str:
    raw_id = path.rstrip("/").rsplit("/", 1)[-1]
    return f"{site['id_prefix']}{raw_id}"


def _parse_and_store_xxxclub_item(site: dict, row: dict, source_rank: int = 999999999) -> bool:
    site_id = site["id"]
    path, title = row["path"], row["title"]
    try:
        details = _xxxclub_parse_details(site, path)
    except Exception as e:
        log.exception("[%s] Ошибка парсинга страницы раздачи %s (%s): %s", site_id, path, title[:60], e)
        return False

    if not details.get("magnet"):
        log.warning("[%s] %s (%s): не нашли magnet-ссылку - пропуск", site_id, path, title[:60])
        return False

    tag = details.get("collection") or ""
    tags = [tag] if tag else []
    screen, stereo = _guess_screen_stereo_xxxclub(title)
    raw_id = path.rstrip("/").rsplit("/", 1)[-1]

    item = {
        "id": f"{site['id_prefix']}{raw_id}",
        "topic_id": raw_id,
        "title": title,
        "tags": tags,
        "thumbnail_url": details.get("poster") or row.get("thumb") or "",
        "source_type": "magnet",
        "source": details["magnet"],
        "screen_type": screen,
        "stereo_mode": stereo,
        "video_length": 0,
        "studio": tags,
        "performers": [],
        "site": site_id,
        "source_rank": source_rank,
    }
    store.upsert_item(item)
    return True


def _refresh_site_xxxclub(site: dict) -> int:
    site_id = site["id"]
    section_url = site["base_url"].rstrip("/") + site["category_path"]
    seen = 0

    # 1) "Быстрый" проход - самые новые раздачи на первой странице (список
    #    отсортирован от новых к старым). Как только встретили уже известную
    #    раздачу - дальше можно не смотреть, новее неё быть не может.
    log.info("[%s] Быстрый проход: читаем первую страницу %s", site_id, _xxxclub_listing_url(site, None))
    try:
        soup = _get(site, _xxxclub_listing_url(site, None))
        rows = _xxxclub_parse_listing(soup)
    except Exception as e:
        log.exception("[%s] Ошибка чтения первой страницы: %s", site_id, e)
        rows = []
    log.info("[%s] Быстрый проход: найдено %s раздач на первой странице, проверяем до %s из них",
             site_id, len(rows), config.XXXCLUB_QUICK_SCAN_MAX_ITEMS)

    for n, row in enumerate(rows[: config.XXXCLUB_QUICK_SCAN_MAX_ITEMS], start=1):
        item_id = _xxxclub_item_id(site, row["path"])
        if store.item_exists(item_id):
            log.info("[%s] Быстрый проход: раздача #%s (%s) уже известна - дальше не смотрим", site_id, n, item_id)
            break
        log.info("[%s] Быстрый проход: раздача #%s/%s - %s", site_id, n,
                 min(len(rows), config.XXXCLUB_QUICK_SCAN_MAX_ITEMS), row["title"][:70])
        # Первая страница всегда "самая свежая" - маленький ранг (0..N), независимо
        # от того, сколько циклов обхода уже прошло (см. items.source_rank).
        if _parse_and_store_xxxclub_item(site, row, source_rank=n - 1):
            seen += 1
        time.sleep(0.3)

    # 2) Постепенный обход архива вглубь, курсор хранится в БД между циклами
    #    (переиспользуем столбец backfill_cursor_page - курсор здесь всегда
    #    число, просто непрозрачное, а не порядковая страница).
    state = store.get_crawl_state(section_url)
    cursor = state.get("backfill_cursor_page") or 0
    complete = bool(state.get("backfill_complete", 0))
    # Монотонный счётчик "сколько страниц архива вообще когда-либо видели" -
    # переживает между циклами, используется как "бакет" ранга (чем больше,
    # тем глубже в архиве = старше), т.к. у xxxclub сам курсор непрозрачный
    # и не годится напрямую как номер страницы.
    pages_seq = state.get("pages_fetched_seq") or 0
    log.info("[%s] Архивный проход: курсор=%s, завершён=%s, страниц за цикл=%s, всего страниц пройдено=%s",
             site_id, cursor, complete, config.XXXCLUB_BACKFILL_PAGES_PER_CYCLE, pages_seq)

    if not complete:
        next_cursor = str(cursor) if cursor else None
        for page_num in range(1, config.XXXCLUB_BACKFILL_PAGES_PER_CYCLE + 1):
            log.info("[%s] Архивный проход: страница %s/%s (курсор=%s)",
                     site_id, page_num, config.XXXCLUB_BACKFILL_PAGES_PER_CYCLE, next_cursor)
            try:
                soup = _get(site, _xxxclub_listing_url(site, next_cursor))
            except Exception as e:
                log.exception("[%s] Ошибка чтения страницы архива (курсор %s): %s", site_id, next_cursor, e)
                break

            pages_seq += 1  # новая, ещё не виденная страница архива
            page_rows = _xxxclub_parse_listing(soup)
            log.info("[%s] Архивный проход: на странице %s найдено %s раздач", site_id, page_num, len(page_rows))
            for row_index, row in enumerate(page_rows):
                item_id = _xxxclub_item_id(site, row["path"])
                if store.item_exists(item_id):
                    continue
                source_rank = _XXXCLUB_BACKFILL_RANK_BASE + pages_seq * _RANK_PAGE_MULT + row_index
                if _parse_and_store_xxxclub_item(site, row, source_rank=source_rank):
                    seen += 1
                time.sleep(0.3)

            new_cursor = _xxxclub_next_cursor(soup)
            time.sleep(0.5)
            if not new_cursor:
                store.save_crawl_state(section_url, 0, complete=True, pages_fetched_seq=pages_seq)
                log.info("[%s] Архив полностью пройден (дальше страниц нет).", site_id)
                break
            next_cursor = new_cursor

            store.save_crawl_state(section_url, int(next_cursor), complete=False, pages_fetched_seq=pages_seq)

    return seen


def _build_listing_url(site: dict, section_url: str, page: int) -> str:
    if site["pagination"] == "path":
        # страница 0 (внутренняя нумерация с 0) = сама section_url без суффикса,
        # дальше .../page/2/, .../page/3/, ... (у сайта нумерация страниц с 1)
        if page == 0:
            return section_url
        return f"{section_url.rstrip('/')}/page/{page + 1}/"

    # "query": &start=N (классика движков rutracker)
    start = page * site["topics_per_page"]
    sep = "&" if "?" in section_url else "?"
    if page == 0:
        return section_url
    return f"{section_url}{sep}{site['page_param']}={start}"


def _parse_int(text) -> int:
    if not text:
        return None
    m = re.search(r"\d+", str(text).replace("\xa0", "").replace(" ", ""))
    return int(m.group(0)) if m else None


def fetch_listing_page(site_id: str, section_url: str, page: int):
    """Список строк с ОДНОЙ страницы раздела (page с 0) - topic_id/topic_url/title,
    плюс сиды/личи, если движок их показывает прямо в списке (статичный HTML,
    не JS/AJAX - проверено на реальной вёрстке pornolab)."""
    site = _site(site_id)
    url = _build_listing_url(site, section_url, page)
    soup = _get(site, url)
    rows = soup.select(site["selectors"]["row"])
    topic_id_re = re.compile(site["topic_id_re"])
    topics = []
    for row in rows:
        a = row.select_one(site["selectors"]["topic_link"])
        if not a:
            continue
        href = a.get("href", "")
        m = topic_id_re.search(href)
        if not m:
            continue
        topic_id = m.group(1)
        title = a.get_text(strip=True)
        topic_url = _abs_url(site, href)

        seeders, leechers = None, None
        sel = site["selectors"]
        if "seeders" in sel:
            seeders = _parse_int(row.select_one(sel["seeders"]) and row.select_one(sel["seeders"]).get_text())
        if "leechers" in sel:
            leechers = _parse_int(row.select_one(sel["leechers"]) and row.select_one(sel["leechers"]).get_text())

        topics.append({
            "topic_id": topic_id,
            "topic_url": topic_url,
            "title": title,
            "seeders": seeders,
            "leechers": leechers,
        })
    return topics


def list_section_topics(site_id: str, section_url: str, max_pages=None):
    """Старый простой обход (используется в debug-скриптах) - читает страницы подряд,
    начиная с первой, пока они не закончатся или не достигнут max_pages."""
    topics = []
    page = 0
    while max_pages is None or page < max_pages:
        found = fetch_listing_page(site_id, section_url, page)
        if not found:
            break
        topics.extend(found)
        page += 1
        time.sleep(0.5)
    return topics


def _parse_and_store_topic(site_id: str, topic_id: str, topic_url: str, title: str, source_rank: int = 999999999,
                            seeders: int = None, leechers: int = None) -> bool:
    """Парсит одну тему и сохраняет в кэш. True, если реально добавлена карточка.

    source_rank - позиция темы в разделе на МОМЕНТ обнаружения (страница*множитель +
    позиция в списке на странице) - меньше = новее/выше в разделе. Используется для
    сортировки библиотеки ТОЧНО как на трекере, а не по времени, когда мы её нашли
    (см. комментарий у items.source_rank в store.init_db()).

    seeders/leechers - со страницы СПИСКА раздела (дёшево, уже есть при обходе).
    """
    try:
        item = parse_topic(site_id, topic_id, topic_url, title)
        item["source_rank"] = source_rank
        item["seeders"] = seeders
        item["leechers"] = leechers
        if not item["source"]:
            log.warning("[%s] Тема %s (%s): нет доступной ссылки на скачивание - пропуск "
                        "(возможно, нужен логин)", site_id, topic_id, title[:60])
            return False
        min_h = config.MIN_VIDEO_HEIGHT_PX
        video_height = item.get("video_height")
        if min_h and video_height and video_height < min_h:
            log.info("[%s] Тема %s (%s): разрешение %sp ниже порога %sp - пропуск",
                      site_id, topic_id, title[:60], video_height, min_h)
            return False
        store.upsert_item(item)
        return True
    except Exception as e:
        log.exception("[%s] Ошибка парсинга темы %s (%s): %s", site_id, topic_id, topic_url, e)
        return False


def _refresh_site(site: dict) -> int:
    site_id = site["id"]
    try:
        login(site_id)
    except Exception as e:
        log.exception("[%s] Ошибка логина: %s", site_id, e)

    seen = 0
    for section_url in site["section_urls"]:
        state = store.get_crawl_state(section_url)
        cursor = state.get("backfill_cursor_page", 0) or 0
        complete = bool(state.get("backfill_complete", 0))

        pages = set(range(0, config.FORUM_QUICK_SCAN_PAGES))
        backfill_pages = []
        if not complete:
            end = min(cursor + config.FORUM_BACKFILL_PAGES_PER_CYCLE, site["total_pages"])
            backfill_pages = list(range(cursor, end))
            pages.update(backfill_pages)

        new_in_section = 0
        any_page_had_rows = False
        for page in sorted(pages):
            try:
                topics = fetch_listing_page(site_id, section_url, page)
            except Exception as e:
                log.exception("[%s] Ошибка чтения страницы %s раздела %s: %s", site_id, page, section_url, e)
                continue
            if topics:
                any_page_had_rows = True
            for row_index, row in enumerate(topics):
                topic_id, topic_url, title = row["topic_id"], row["topic_url"], row["title"]
                item_id = f"{site['id_prefix']}{topic_id}"
                if store.item_exists(item_id):
                    continue  # уже знаем эту тему - не скачиваем её страницу заново
                # номер страницы у pornolab УЖЕ порядковый (0 = первая/самая новая
                # страница раздела) - используем его напрямую как "бакет" ранга
                source_rank = page * _RANK_PAGE_MULT + row_index
                if _parse_and_store_topic(site_id, topic_id, topic_url, title, source_rank,
                                           seeders=row.get("seeders"), leechers=row.get("leechers")):
                    seen += 1
                    new_in_section += 1
                time.sleep(0.3)
            time.sleep(0.5)

        if not complete and backfill_pages:
            new_cursor = backfill_pages[-1] + 1
            if new_cursor >= site["total_pages"]:
                store.save_crawl_state(section_url, 0, complete=True)
                log.info("[%s] Раздел %s: полный обход архива (%s страниц) завершён!",
                         site_id, section_url, site["total_pages"])
            else:
                store.save_crawl_state(section_url, new_cursor, complete=False)
                log.info("[%s] Раздел %s: архив пройден до страницы %s из %s (новых тем за цикл: %s)",
                         site_id, section_url, new_cursor, site["total_pages"], new_in_section)
        elif not any_page_had_rows and not complete:
            # страницы вернулись пустыми раньше, чем ожидалось - видимо, раздел короче
            store.save_crawl_state(section_url, cursor, complete=True)
            log.info("[%s] Раздел %s: дальше страницы пустые, считаем архив собранным.", site_id, section_url)

    persist_cookies(site_id)  # за время обхода сессия могла обновиться - сохраняем на диск
    return seen


def refresh_all():
    """
    Инкрементальное обновление ПО ВСЕМ источникам из config.FORUM_SITES:
      1) "Быстрый" проход первых FORUM_QUICK_SCAN_PAGES страниц - там форум
         показывает самые новые раздачи (список отсортирован от новых к старым).
      2) Небольшой шаг вглубь архива (FORUM_BACKFILL_PAGES_PER_CYCLE страниц),
         начиная с сохранённого курсора - так весь раздел постепенно соберётся
         за много циклов обновления, не блокируя обычную работу надолго.
    Темы, уже сохранённые в кэше (по id с учётом префикса источника), НЕ
    скачиваются повторно - запрашивается только страница списка (дёшево).
    """
    total_seen = 0
    for site in config.FORUM_SITES:
        try:
            if site.get("engine") == "xxxclub":
                total_seen += _refresh_site_xxxclub(site)
            elif site.get("engine") == "knaben":
                # Ленивый import не создаёт циклическую зависимость при обычном
                # импорте app/scraper и не загружает provider, когда он выключен.
                import knaben  # noqa: PLC0415
                total_seen += knaben.refresh()
            else:
                total_seen += _refresh_site(site)
        except Exception as e:
            log.exception("[%s] Ошибка обновления источника: %s", site["id"], e)
    log.info("Обновление завершено по всем источникам, новых тем добавлено: %s", total_seen)
    return total_seen
