
# -*- coding: utf-8 -*-
"""
Конфигурация моста "Форум -> DeoVR -> TorrServe".

Настроено под PornoLab.Net, раздел "3D & Virtual Reality Videos" (f=1823),
движок торрент-трекера семейства rutracker (тот же код, что и у rutracker.org).
"""

import os


def _load_local_env():
    """Минимальная загрузка .env без внешних зависимостей.

    Файл .env уже в .gitignore; удобно хранить там локальные API keys вроде
    ThePornDB/StashBox, не экспортируя переменные каждый запуск.
    """
    path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_local_env()

# ---------------------------------------------------------------------------
# ФОРУМ
# ---------------------------------------------------------------------------

FORUM_BASE_URL = os.environ.get("FORUM_BASE_URL", "https://pornolab.net/forum")

FORUM_SECTION_URLS = [
    os.environ.get("FORUM_SECTION_URL", "https://pornolab.net/forum/viewforum.php?f=1823"),
]

# Кодировка страниц форума (сайт отдаёт charset=windows-1251)
FORUM_ENCODING = "windows-1251"

# Пагинация: &start=N, N = (page-1)*50
FORUM_PAGE_PARAM = "start"
FORUM_TOPICS_PER_PAGE = 50

# ---------------------------------------------------------------------------
# ИНКРЕМЕНТАЛЬНЫЙ ОБХОД БОЛЬШОГО РАЗДЕЛА (782 страницы по 50 тем = ~39100 тем)
# ---------------------------------------------------------------------------
# Общее число страниц в разделе (можно подправить, если сайт покажет другое число -
# лишние "пустые" страницы просто будут пропущены автоматически).
FORUM_TOTAL_PAGES = int(os.environ.get("FORUM_TOTAL_PAGES", "782"))

# Каждый цикл обновления всегда проверяем самые первые страницы (там появляются
# новые раздачи - список отсортирован от новых к старым) - это "быстрый" проход.
FORUM_QUICK_SCAN_PAGES = int(os.environ.get("FORUM_QUICK_SCAN_PAGES", "2"))

# Плюс по чуть-чуть проходим вглубь архива каждый цикл (курсор сохраняется в БД,
# между запусками/перезапусками не теряется). 20 страниц ≈ 1000 тем за цикл -
# весь архив из 782 страниц соберётся примерно за REFRESH_INTERVAL_SEC * 39.
# Увеличьте, если хотите пройти архив быстрее (ценой нагрузки на форум), или
# уменьшите, если хотите быть к сайту повежливее.
FORUM_BACKFILL_PAGES_PER_CYCLE = int(os.environ.get("FORUM_BACKFILL_PAGES_PER_CYCLE", "20"))

# Темы, уже сохранённые в кэше (cache.sqlite3), повторно НЕ скачиваются и НЕ
# парсятся при обходе - запрашивается только сама страница списка (дёшево).
# Это и даёт ускорение: как только архив собран один раз, обычные обновления
# идут за секунды (видят только новые темы на первых 1-2 страницах).

# Авторизация нужна только для скачивания .torrent (сам список/темы доступны гостю)
#
# СПОСОБ 1 (рекомендуется, если сайт просит капчу при автологине):
# Войдите на сайт обычным браузером (капчу решите сами), скопируйте строку cookie
# из DevTools (см. README) и сохраните её ЦЕЛИКОМ в файл forum_cookies.txt рядом
# с этим config.py. Тогда логин/пароль ниже не нужны - скрипт возьмёт готовую сессию.
FORUM_COOKIE_FILE = os.environ.get("FORUM_COOKIE_FILE", os.path.join(os.path.dirname(__file__), "forum_cookies.txt"))

# СПОСОБ 2 (простой логин/паролем) - сработает, только если сайт НЕ требует капчу для вашего IP.
FORUM_LOGIN_URL = os.environ.get("FORUM_LOGIN_URL", "https://pornolab.net/forum/login.php")
FORUM_USERNAME = os.environ.get("FORUM_USERNAME", "")
FORUM_PASSWORD = os.environ.get("FORUM_PASSWORD", "")
FORUM_LOGIN_FIELD_USER = "login_username"
FORUM_LOGIN_FIELD_PASS = "login_password"
# Поле кнопки "Вход" - значение передаётся в кодировке windows-1251
FORUM_LOGIN_SUBMIT_FIELD = "login"
FORUM_LOGIN_SUBMIT_VALUE = "Вход"
FORUM_LOGIN_EXTRA_FIELDS = {"redirect": "index.php"}

# Селекторы под реальную верстку PornoLab / rutracker-engine
SELECTORS = {
    # --- страница списка раздела (viewforum.php) ---
    "row": "tr[id^='tr-']",
    # гость видит class="topictitle tt-text", залогиненный - class="torTopic bold tt-text t-vis"
    "topic_link": "a.topictitle.tt-text, a.torTopic.tt-text",
    "size_cell": "td.tCenter.med.nowrap",
    # сиды/личи прямо в списке раздела (статичный HTML, не JS/AJAX) -
    # <span class="seedmed" title="Seeders"><b>46</b></span> | <span class="leechmed" ...><b>9</b></span>
    "seeders": "span.seedmed b",
    "leechers": "span.leechmed b",

    # --- страница темы (viewtopic.php) ---
    "post_body": "div.post_body",
    "post_message": "div.post-user-message",
    "tags_list": "#tor-tags-list a",          # текст = тег, href содержит ?tg=...
    "meta_label": "span.post-b",              # пары "Жанр:", "Продолжительность:" и т.п.
    # гость: fieldset.attach h1.attach_link a (href = profile.php?mode=register)
    # залогинен: a.dl-stub.dl-link (href = dl.php?t=ID) - именно этот вариант рабочий
    "attach_link": "a.dl-stub.dl-link, fieldset.attach h1.attach_link a",
    "preview_img": "var.postImg",             # картинка лежит в атрибуте title, не в src
    "screenshots_wrap": "div.sp-wrap",        # блок "Скринлист" - превью НЕ оттуда берём
}

# Минимальная вертикальная "высота" из поля "Тип видео" (например "3072p" -> 3072),
# ниже которой раздача ВООБЩЕ не сохраняется в библиотеку (не просто прячется в
# интерфейсе - не тратим на неё ранг/место). По умолчанию отсекает и 1080p, и 1440p
# (1441 > 1440). Если нужно пускать 1440p - поставьте 1081. Если раздача не содержит
# поля "Тип видео" вовсе (video_height=None) - НЕ отсекаем (пропускаем проверку),
# чтобы не терять раздачи с нестандартным описанием из-за отсутствия этого поля.
MIN_VIDEO_HEIGHT_PX = int(os.environ.get("MIN_VIDEO_HEIGHT_PX", "1441"))

# ---------------------------------------------------------------------------
# TORRSERVE
# ---------------------------------------------------------------------------

# Адрес, по которому НАШ СЕРВЕР (app.py) сам обращается к TorrServe - они оба
# работают на одном ПК, поэтому тут всегда 127.0.0.1, трогать не нужно.
TORRSERVE_HOST = os.environ.get("TORRSERVE_HOST", "127.0.0.1")
TORRSERVE_PORT = int(os.environ.get("TORRSERVE_PORT", "8090"))
TORRSERVE_BASE_URL = f"http://{TORRSERVE_HOST}:{TORRSERVE_PORT}"
TORRSERVE_USER = os.environ.get("TORRSERVE_USER", "")
TORRSERVE_PASS = os.environ.get("TORRSERVE_PASS", "")

# Порт TorrServe, который увидит САМ ПЛЕЕР (Quest) в итоговой ссылке потока -
# ОТДЕЛЬНО от TORRSERVE_HOST/PORT выше. Хост для этой ссылки app.py берёт
# автоматически из того же адреса, по которому Quest уже открывает /deovr
# (см. app.py:_torrserve_public_base_url) - его указывать не нужно. А вот ПОРТ
# меняйте здесь, только если проброс/прокси отдаёт TorrServe наружу под другим
# портом, чем 8090, на котором он слушает локально (в обычной связке "всё на
# одном ПК, Quest по Wi-Fi" - оставьте как есть, порты совпадают).
TORRSERVE_PUBLIC_PORT = int(os.environ.get("TORRSERVE_PUBLIC_PORT", str(TORRSERVE_PORT)))

# Важно: сам TorrServe должен слушать 0.0.0.0 (все интерфейсы), а не только
# 127.0.0.1 - иначе Quest физически не достучится до него по сети, какой адрес
# ему ни дай. Обычно это настраивается при запуске TorrServe (например, флагом
# --host 0.0.0.0 или в его собственном settings.json) - это НЕ настройка этого
# моста, проверьте по документации вашей сборки TorrServe, если поток всё равно
# не открывается при верном адресе.

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".ts", ".mov", ".m4v", ".wmv")

# ---------------------------------------------------------------------------
# СЕРВЕР МОСТА
# ---------------------------------------------------------------------------

BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "0.0.0.0")
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "9999"))
BRIDGE_PUBLIC_URL = os.environ.get("BRIDGE_PUBLIC_URL", "http://127.0.0.1:9999")

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "cache.sqlite3"))

# По умолчанию для контента без явных признаков (конкретный тег/заголовок переопределяет)
DEFAULT_SCREEN_TYPE = "dome"    # "dome" = 180°, самый частый формат в этом разделе
DEFAULT_STEREO_MODE = "sbs"

MAX_TAG_SCENES = int(os.environ.get("MAX_TAG_SCENES", "15"))

# Классический вид библиотеки DeoVR получает ВЕСЬ список одним JSON-ответом
# (нет серверной пагинации) - если какая-то вкладка ("Всё" или конкретный тег)
# разрастается до многих тысяч карточек (например, после полного обхода
# архива pornolab - до ~39000 тем), ответ /deovr может вырасти до десятков
# мегабайт и зависает браузер/роняет сам плеер на Quest. Поэтому каждая
# вкладка ограничивается этим числом САМЫХ СВЕЖИХ карточек (остальные никуда
# не пропадают - они по-прежнему в cache.sqlite3, просто не показываются
# одним списком; попадут в поле зрения через другие теги/со временем, когда
# более старые карточки сместятся).
MAX_SCENE_ITEMS = int(os.environ.get("MAX_SCENE_ITEMS", "400"))

# Источники, которые уже можно продолжать скрапить/держать в БД, но временно НЕ
# показывать в пользовательских библиотеках (/deovr, /heresphere, PLAYA API).
# Сейчас xxxclub и knaben скрыты по просьбе пользователя:
# - xxxclub: вернём выдачу карточек позже, когда отдельно проверим качество метаданных
# - knaben: нужен только для поиска альтернативных раздач (дублей) если на основном
#   источнике (pornolab) не хватает сидов, а не для показа в общем списке
HIDDEN_LIBRARY_SITE_IDS = {
    part.strip()
    for part in os.environ.get("HIDDEN_LIBRARY_SITE_IDS", "xxxclub,knaben").split(",")
    if part.strip()
}

# Внешние постеры актёров/логотипы студий для PLAY'A. По умолчанию пробуем
# лениво подтягивать публичные изображения с VRSpy (актёры) и POVR (студии)
# и кэшировать только URL в БД. Если внешние сайты недоступны или хочется
# полностью офлайн-режим - поставьте 0.
ENABLE_EXTERNAL_ENTITY_META = os.environ.get("ENABLE_EXTERNAL_ENTITY_META", "1").lower() not in ("0", "false", "no")
EXTERNAL_ENTITY_META_TTL_SEC = int(os.environ.get("EXTERNAL_ENTITY_META_TTL_SEC", str(60 * 24 * 3600)))
EXTERNAL_ENTITY_META_TIMEOUT_SEC = int(os.environ.get("EXTERNAL_ENTITY_META_TIMEOUT_SEC", "8"))
POVR_BASE_URL = os.environ.get("POVR_BASE_URL", "https://pornhubvr.com")
VRSPY_BASE_URL = os.environ.get("VRSPY_BASE_URL", "https://www.vrspy.com")
VRPORN_BASE_URL = os.environ.get("VRPORN_BASE_URL", "https://vrporn.com")
# Трейлерные карточки внешних PLAY'A-каталогов. Списки сцен кэшируются локально,
# но подписанные media URL всегда запрашиваются заново при открытии карточки.
ENABLE_EXTERNAL_TRAILERS = os.environ.get("ENABLE_EXTERNAL_TRAILERS", "1").lower() not in ("0", "false", "no")

# Автоматическое сопоставление карточек
ENABLE_AUTO_MATCH = os.environ.get("ENABLE_AUTO_MATCH", "1").lower() not in ("0", "false", "no")
ENABLE_DAILY_MATCH = os.environ.get("ENABLE_DAILY_MATCH", "0").lower() not in ("0", "false", "no")
EXTERNAL_TRAILER_CATALOG_TTL_SEC = int(os.environ.get("EXTERNAL_TRAILER_CATALOG_TTL_SEC", str(24 * 3600)))
# Опциональные структурированные metadata-провайдеры Stash-Box (например,
# ThePornDB/StashDB). Формат env:
# STASHBOX_METADATA_PROVIDERS="https://theporndb.net/graphql|API_KEY;https://stashdb.org/graphql|API_KEY"
# Ключи не коммитим; переменная пустая по умолчанию.
STASHBOX_METADATA_PROVIDERS = []
for _raw_provider in os.environ.get("STASHBOX_METADATA_PROVIDERS", "").split(";"):
    _raw_provider = _raw_provider.strip()
    if not _raw_provider:
        continue
    if "|" in _raw_provider:
        _endpoint, _api_key = _raw_provider.split("|", 1)
    else:
        _endpoint, _api_key = _raw_provider, ""
    if _endpoint.strip():
        STASHBOX_METADATA_PROVIDERS.append({"endpoint": _endpoint.strip(), "api_key": _api_key.strip()})

# Локальный список актёров/студий (web UI + звёздочки/сортировка в PLAY'A).
ENTITY_LIST_DEFAULT_NAME = os.environ.get("ENTITY_LIST_DEFAULT_NAME", "watchlist")

# Публичный Knaben API используется как дополнительный meta-index: берём только
# VR-похожие magnet/infohash с недавним lastSeen, реальными S>=2 и разумным
# размером. Запросы разделяются точкой с запятой, чтобы названия могли содержать
# обычные пробелы и запятые.
ENABLE_KNABEN = os.environ.get("ENABLE_KNABEN", "1").lower() not in ("0", "false", "no")
KNABEN_API_URL = os.environ.get("KNABEN_API_URL", "https://api.knaben.org/v1")
KNABEN_QUERIES = [
    value.strip()
    for value in os.environ.get("KNABEN_QUERIES", "VR180").split(";")
    if value.strip()
]
KNABEN_RESULTS_PER_QUERY = int(os.environ.get("KNABEN_RESULTS_PER_QUERY", "150"))
KNABEN_MAX_IMPORT_ITEMS = int(os.environ.get("KNABEN_MAX_IMPORT_ITEMS", "150"))
KNABEN_MIN_SEEDERS = int(os.environ.get("KNABEN_MIN_SEEDERS", "2"))
KNABEN_MIN_SIZE_BYTES = int(os.environ.get("KNABEN_MIN_SIZE_BYTES", str(1024 ** 3)))
KNABEN_MAX_SIZE_BYTES = int(os.environ.get("KNABEN_MAX_SIZE_BYTES", str(500 * 1024 ** 3)))
KNABEN_LAST_SEEN_SEC = int(os.environ.get("KNABEN_LAST_SEEN_SEC", str(30 * 24 * 3600)))
KNABEN_TIMEOUT_SEC = int(os.environ.get("KNABEN_TIMEOUT_SEC", "20"))

REFRESH_INTERVAL_SEC = int(os.environ.get("REFRESH_INTERVAL_SEC", "3600"))

# Сколько секунд переиспользовать уже полученный у TorrServe stream_url, не
# дёргая TorrServe заново (он остаётся рабочим всё это время - раздача не
# "отваливается"). Снижает нагрузку при повторных заходах/обновлении списка.
STREAM_CACHE_TTL_SEC = int(os.environ.get("STREAM_CACHE_TTL_SEC", str(6 * 3600)))

# ---------------------------------------------------------------------------
# ЗАЩИТА ОТ ЛИШНИХ СКАЧИВАНИЙ .torrent (форум даёт ограниченный суточный лимит)
# ---------------------------------------------------------------------------
# Раньше при каждом открытии карточки (в т.ч. повторном показе той же "сломанной"
# раздачи, автоповторах плеера и т.п.) мы заново дёргали dl.php, даже если
# последняя попытка только что провалилась - это и "съедало" суточный лимит
# скачиваний форума без реального просмотра. Теперь после неудачи карточка на
# время "замораживается" и следующая попытка резолва идёт из кэша ошибки, без
# обращения к форуму/TorrServe.

# Первая пауза после неудачи резолва одной конкретной карточки, секунд. Дальше
# при повторных неудачах пауза удваивается (экспоненциально) до RESOLVE_FAIL_COOLDOWN_MAX_SEC.
RESOLVE_FAIL_COOLDOWN_SEC = int(os.environ.get("RESOLVE_FAIL_COOLDOWN_SEC", str(5 * 60)))
RESOLVE_FAIL_COOLDOWN_MAX_SEC = int(os.environ.get("RESOLVE_FAIL_COOLDOWN_MAX_SEC", str(60 * 60)))

# Если форум в ответе явно сказал "суточный лимит скачиваний исчерпан" - это
# НЕ ошибка конкретной раздачи, а общий бан до конца суток. Дальнейшие попытки
# по ЛЮБОЙ карточке до этого времени только тратят оставшийся лимит впустую,
# поэтому резолв полностью отключается на этот срок (по умолчанию 8 часов).
DAILY_LIMIT_COOLDOWN_SEC = int(os.environ.get("DAILY_LIMIT_COOLDOWN_SEC", str(8 * 3600)))

# Подстроки (нижний регистр), по которым текст ошибки TorrServe/форума распознаётся
# как "суточный лимит скачиваний", а не что-то другое.
DAILY_LIMIT_MARKERS = ("суточн", "лимит скач", "превышен")

# Ограничение ЧАСТОТЫ новых (ещё не закэшированных) резолвов на один источник -
# защита от того, что сам плеер (DeoVR/HereSphere) при обычном пролистывании
# библиотеки может дёрнуть /video/<id> для ДЕСЯТКОВ разных карточек подряд
# (похоже на предзагрузку метаданных/превью при скролле - это поведение самого
# плеера, не пользователь специально нажимает play на каждой). Без этой защиты
# КАЖДЫЙ такой запрос реально скачивает .torrent с форума - суточный лимит
# сгорает за секунды просто от просмотра списка. Не более
# RESOLVE_RATE_LIMIT_COUNT новых (не из кэша) резолвов за скользящее окно
# RESOLVE_RATE_LIMIT_WINDOW_SEC секунд на источник - сверх этого дальнейшие
# запросы сразу отклоняются понятной ошибкой, БЕЗ обращения к форуму/TorrServe
# (сам путь карточки при этом не "замораживается" надолго, как при реальной
# ошибке - как только освободится окно, резолв снова попробует пройти).
RESOLVE_RATE_LIMIT_COUNT = int(os.environ.get("RESOLVE_RATE_LIMIT_COUNT", "6"))
RESOLVE_RATE_LIMIT_WINDOW_SEC = int(os.environ.get("RESOLVE_RATE_LIMIT_WINDOW_SEC", str(5 * 60)))


# Экспериментально: пытаться вытащить студию/актрису(ы) из заголовка темы
# (единого поля "актриса" на форуме нет - только теги жанров). Эвристика не
# идеальна, но для раздела HereSphere даёт отдельную категорию "Performers".
# Если будет давать слишком много мусора - поставьте False.
ENABLE_PERFORMER_PARSING = True

# Прокси превью через наш сервер (на случай хотлинк-защиты fastpic/картинок) - обычно не требуется
PROXY_THUMBNAILS = False

# ---------------------------------------------------------------------------
# ВТОРОЙ ИСТОЧНИК: XXXClub.to (отдельный торрент-индексатор, раздел VR)
# ---------------------------------------------------------------------------
# Раньше вторым источником был "клон" xxxtorrent.top (тот же движок, что и
# pornolab) - пришлось от него отказаться: его защита Antibot Cloud требует
# решать визуальную капчу практически на КАЖДУЮ отдельную загрузку страницы
# (не один раз за сессию), что на сотнях страниц раздела невозможно
# автоматизировать без постоянно работающего headless-браузера.
#
# XXXClub.to - СОВСЕМ ДРУГОЙ движок (не форум, а отдельный торрент-каталог):
#   - логин НЕ нужен вообще - список раздела и прямая magnet-ссылка открыты
#     всем, без сессии/cookie
#   - никакой антибот-защиты (TLS-отпечаток не проверяется, обычный requests
#     работает без curl_cffi)
#   - пагинация - НЕ номера страниц, а непрозрачный "курсор" в URL вида
#     /torrents/browse/<категория>/<курсор> - заранее неизвестный, переходить
#     можно только последовательно по найденной на странице ссылке "Next Page"
#   - на странице списка есть ловушка для ботов: несколько ПОДДЕЛЬНЫХ ссылок
#     "Next Page" со стилем position:absolute;left:-99999px (невидимы в
#     обычном браузере) - настоящая ссылка на странице всегда одна, без этого
#     стиля (см. scraper._xxxclub_next_cursor)
# Раздачи сохраняются в общей БД с префиксом id "xc_", чтобы не пересекаться
# с id основного форума. Показывается в DeoVR отдельной вкладкой "XXXClub: ...".
XXXCLUB_BASE_URL = os.environ.get("XXXCLUB_BASE_URL", "https://xxxclub.to")
# "6" = категория VR/VirtualReality на сайте
XXXCLUB_CATEGORY_PATH = os.environ.get("XXXCLUB_CATEGORY_PATH", "/torrents/browse/6/")
# Список отсортирован от новых к старым - "быстрый" проход каждый цикл смотрит
# не больше этого числа самых новых раздач и останавливается, как только
# встретит уже известную (новее неё быть не может)
XXXCLUB_QUICK_SCAN_MAX_ITEMS = int(os.environ.get("XXXCLUB_QUICK_SCAN_MAX_ITEMS", "60"))
# Сколько страниц архива проходить вглубь за один цикл обновления (курсор
# запоминается в БД между перезапусками, как и у остальных источников)
XXXCLUB_BACKFILL_PAGES_PER_CYCLE = int(os.environ.get("XXXCLUB_BACKFILL_PAGES_PER_CYCLE", "5"))

# PTorrents.com settings
PTORRENTS_ENABLED = os.environ.get("PTORRENTS_ENABLED", "1").lower() in ("1", "true", "yes")
POVR_ENABLED = os.environ.get("POVR_ENABLED", "0").lower() in ("1", "true", "yes")
VRPORN_ENABLED = os.environ.get("VRPORN_ENABLED", "0").lower() in ("1", "true", "yes")
PTORRENTS_BASE_URL = os.environ.get("PTORRENTS_BASE_URL", "https://ptorrents.com")
PTORRENTS_MAX_PAGES = int(os.environ.get("PTORRENTS_MAX_PAGES", "340"))
PTORRENTS_ITEMS_PER_PAGE = int(os.environ.get("PTORRENTS_ITEMS_PER_PAGE", "50"))

# Список "источников" (сайтов), которые обходит scraper.py. Каждый - независимая
# сессия/куки/селекторы. Порядок = порядок вкладок в DeoVR.
FORUM_SITES = [
    {
        "id": "pornolab",
        "name": "",  # основной источник - без приставки в названиях вкладок (как было раньше)
        "base_url": FORUM_BASE_URL,
        "section_urls": FORUM_SECTION_URLS,
        "encoding": FORUM_ENCODING,
        "cookie_file": FORUM_COOKIE_FILE,
        "login_url": FORUM_LOGIN_URL,
        "username": FORUM_USERNAME,
        "password": FORUM_PASSWORD,
        "login_field_user": FORUM_LOGIN_FIELD_USER,
        "login_field_pass": FORUM_LOGIN_FIELD_PASS,
        "login_submit_field": FORUM_LOGIN_SUBMIT_FIELD,
        "login_submit_value": FORUM_LOGIN_SUBMIT_VALUE,
        "login_extra_fields": FORUM_LOGIN_EXTRA_FIELDS,
        "selectors": SELECTORS,
        # viewtopic.php?t=12345
        "topic_id_re": r"[?&]t=(\d+)",
        "topic_url_template": "{base}/viewtopic.php?t={id}",
        "pagination": "query",      # &start=N
        "page_param": FORUM_PAGE_PARAM,
        "topics_per_page": FORUM_TOPICS_PER_PAGE,
        "total_pages": FORUM_TOTAL_PAGES,
        "id_prefix": "",
        # обычный requests - антибот-проверки на этом сайте нет, только логин
        "impersonate": None,
    },
    {
        "id": "xxxclub",
        "name": "XXXClub",  # приставка к названиям вкладок: "XXXClub: Всё", "XXXClub: <тег>"
        "base_url": XXXCLUB_BASE_URL,
        "category_path": XXXCLUB_CATEGORY_PATH,
        "encoding": "utf-8",
        "cookie_file": "",          # логин не нужен - cookie сохранять незачем
        "id_prefix": "xc_",
        "engine": "xxxclub",        # отдельный движок - обходится по-другому,
                                     # см. scraper._refresh_site_xxxclub()
        "impersonate": None,        # антибота нет - обычный requests, без curl_cffi
    },
    {
        "id": "ptorrents",
        "name": "PTorrents",
        "base_url": PTORRENTS_BASE_URL,
        "engine": "ptorrents",
        "cookie_file": "",
        "id_prefix": "pt_",
        "max_pages": PTORRENTS_MAX_PAGES,
        "impersonate": "chrome131",
    },
    {
        "id": "knaben",
        "name": "Knaben",
        "base_url": KNABEN_API_URL,
        "engine": "knaben",
        "cookie_file": "",
        "id_prefix": "kn_",
    },
]

# AstalaVR Cloudflare cf_clearance cookie
# Значение из DevTools браузера (Application → Cookies → astalavr.com → cf_clearance)
# Без этой переменной CDN будет возвращать 403 на всех видео
import os as _os
ASTALAVR_CF_CLEARANCE = _os.environ.get("ASTALAVR_CF_CLEARANCE", "")
