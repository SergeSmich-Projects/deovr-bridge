# DeoVR ⇄ PornoLab (VR-раздел) ⇄ TorrServe — мост

Сервер, который:
1. Парсит раздел **PornoLab.Net → «Порноролики в 3D и Virtual Reality (VR)»** (`viewforum.php?f=1823`)
   и достаёт из каждой темы: заголовок, превью-картинку, официальные теги сайта, длительность,
   тип 3D (SBS/TB), 180°/360°, и ссылку на скачивание `.torrent`.
2. Отдаёт всё это в формате **DeoVR JSON API** — классический вид: сетка карточек, вкладки сверху
   = теги (плюс вкладка «Всё»).
3. При клике по карточке в DeoVR подтягивается `/video/<id>`, который на лету дёргает **TorrServe**
   и получает прямой HTTP-поток файла раздачи — его DeoVR отдаёт в выбранный VR-плеер.

Проверено на реальных страницах сайта (структура подтверждена, не теория):

| Что | Как устроено на сайте | Где в коде |
|---|---|---|
| Список раздела | `viewforum.php?f=1823`, страницы через `&start=N` (по 50 тем) | `scraper.list_section_topics` |
| Строка темы | `<tr id="tr-ID">...<a class="topictitle tt-text" href="./viewtopic.php?t=ID">` | `SELECTORS["row"]`, `SELECTORS["topic_link"]` |
| Теги раздачи (родные, с сайта) | `#tor-tags-list a` → `tracker.php?tg=...` | `SELECTORS["tags_list"]` |
| Превью | `<var class="postImg" title="URL картинки">` (ссылка в атрибуте `title`, не в `src`!) | `SELECTORS["preview_img"]` |
| Метаданные (Жанр/Продолжительность/Тип 3D/Тип устройства) | `<span class="post-b">Жанр</span>: значение<br/>` | `_extract_meta_fields` |
| Ссылка на скачивание (та самая «непрямая») | `fieldset.attach h1.attach_link a` → `dl.php?t=ID`, **требует авторизации** (гостю показывает ссылку на регистрацию) | `SELECTORS["attach_link"]` |
| Логин | `POST /forum/login.php`, поля `login_username`, `login_password`, `login=Вход`, форма в **windows-1251** | `scraper.login()` |

## Структура проекта

```
config.py                  — настройки (форум, TorrServe, сервер моста)
scraper.py                 — логин + парсинг раздела/тем pornolab.net
torrserve.py                — HTTP API TorrServe (magnet напрямую / upload .torrent по логину)
store.py                    — кэш карточек в sqlite
app.py                       — Flask: /deovr, /video/<id>, /refresh
seed_from_live_sample.py     — для проверки: тянет 2 реальные темы с сайта (без логина видно всё, кроме самой ссылки на torrent)
```

## Как это будет работать "в бою"

```
Meta Quest (DeoVR)  --Wi-Fi-->  ПК: этот сервер (:9999)  --скрейпинг-->  PornoLab
                                      |
                                      '--HTTP API-->  TorrServe (:8090)  --BitTorrent-->  раздача
```

## Установка и запуск (на ПК с TorrServe)

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Обязательно для реального скачивания: логин на сайте

Раздел и темы видны без логина, но сама ссылка на `.torrent` (`dl.php?t=ID`) требует авторизации.
Задайте свои данные (переменными окружения — так пароль не попадёт в код/git):

```bash
export FORUM_USERNAME="ваш_логин"
export FORUM_PASSWORD="ваш_пароль"
export TORRSERVE_HOST=127.0.0.1
export TORRSERVE_PORT=8090
```

Затем:

```bash
python app.py
```

При старте фоновое обновление (`REFRESH_INTERVAL_SEC`, по умолчанию раз в час) само залогинится
и соберёт карточки в `cache.sqlite3`. Можно также дёрнуть вручную: `curl http://127.0.0.1:9999/refresh`.

Проверка: `http://127.0.0.1:9999/deovr` должен вернуть JSON со сценами (вкладки = теги сайта:
`vr`, `hardcore`, `sidebyside`, `180°`-содержащие и т.д.) и карточками с живыми превью.

### Быстрая проверка без логина (увидите структуру, но source будет фиктивным)

```bash
python seed_from_live_sample.py
python app.py
```

## Подключение в DeoVR на Quest

Library → Internet browser (или аналогичный пункт "ввести URL") → ввести:

```
http://<LAN-IP-вашего-ПК>:9999/deovr
```

IP компьютера смотрите через `ipconfig` (Windows) / `ip a` (Linux). Либо положите на сервере
файл `deovr` (без расширения, отдельным роутом) в корень — тогда в DeoVR достаточно ввести просто
`http://<LAN-IP>:9999` (уже сделано: `/` отдаёт то же самое, что и `/deovr`).

**Важно:** TorrServe должен быть запущен и доступен по `TORRSERVE_HOST:PORT` ещё до клика по
видео в DeoVR — именно в момент клика наш сервер просит TorrServe начать раздавать файл и
возвращает DeoVR прямую ссылку на поток.

## Подключение в PLAY'A VR

Реализован начальный PLAY'A API v2 рядом с DeoVR:

```
http://<LAN-IP-вашего-ПК>:9999/api/playa/v2/version
http://<LAN-IP-вашего-ПК>:9999/api/playa/v2/config
http://<LAN-IP-вашего-ПК>:9999/api/playa/v2/videos
http://<LAN-IP-вашего-ПК>:9999/api/playa/v2/video/<id>
```

В PLAY'A VR: Web → Add Website → ввести:

```
http://<LAN-IP-вашего-ПК>:9999
```

PLAY'A сам добавит базовый путь `/api/playa/v2`. Аутентификация отключена (`auth=false`),
поэтому логин/JWT не нужны. Для воспроизведения используется тот же ленивый `/stream/<id>`:
TorrServe реально дёргается только при открытии видео, не при просмотре списка.

По умолчанию карточки источника `xxxclub` временно скрыты из пользовательской выдачи
(`/deovr`, `/heresphere`, PLAY'A API), но остаются в БД. Управляется переменной:

```bash
HIDDEN_LIBRARY_SITE_IDS=xxxclub
```

Чтобы снова показать все источники, можно запустить с пустым значением:

```bash
HIDDEN_LIBRARY_SITE_IDS=
```

### PLAY'A: постеры актёров и логотипы студий

Для страниц Actors/Studios мост лениво пробует подтягивать публичные картинки:

- основной простой источник — PLAY'A API сайта `https://vrporn.com`: `/api/playa/v2/actors`, `/actor/<id>`, `/studios`, `/studio/<id>` уже отдают готовые `preview` для актёров и логотипов студий;
- если задан `STASHBOX_METADATA_PROVIDERS`, дополнительно можно пробовать структурированные Stash-Box endpoints (например ThePornDB/StashDB) и брать `images` у performer/studio;
- fallback для актёров: `https://www.vrspy.com/stars` / `https://www.vrspy.com/star/<actor-slug>`;
- fallback для студий: `https://povr.com/studios` / `https://povr.com/studios/<studio-slug>`;
- если на странице удаётся найти трейлер одного из роликов, его URL сохраняется только как служебная ссылка в debug/metadata.

Stash-Box provider включается через переменную окружения или локальный `.env` файл (он в `.gitignore`, ключи не коммитить), но для VRPorn PLAY'A API ключ не нужен:

```bash
STASHBOX_METADATA_PROVIDERS="https://theporndb.net/graphql|TPDB_API_KEY;https://stashdb.org/graphql|STASHDB_API_KEY"
```

Списки Actors/Studios больше не ждут сетевых lookup'ов синхронно: сначала быстро отдаются названия с PNG-заглушками, а недостающие URL обновляются в фоне и попадают в `cache.sqlite3` для следующего refresh в PLAY'A. Кэшируются только URL и текстовые метаданные, сами изображения/видео не сохраняются в БД. Удачно найденные картинки считаются свежими `EXTERNAL_ENTITY_META_TTL_SEC` секунд (по умолчанию 30 дней); актёры ищутся точечно по title, а студии берутся пачкой из `https://vrporn.com/api/playa/v2/studios?page-size=1000`, чтобы не делать запрос на каждую студию.
Отключить внешний lookup можно так:

```bash
ENABLE_EXTERNAL_ENTITY_META=0
```

Правая панель PLAY'A сейчас отдаёт только группу `Tags` с обычными категориями/тегами. Нативное
избранное видео PLAY'A не дублируется на стороне моста.

Для актёров/студий, где у PLAY'A нет documented write-favorites endpoint, есть локальный список:

```text
GET /entity-lists          # простая web-страница с кнопками добавить/убрать
GET /entity-lists.json     # JSON содержимого списка
GET /entity-list/<actor|studio>/<id>/toggle?title=<title>
POST/DELETE /entity-list/<actor|studio>/<id>
```

Отмеченные актёры/студии поднимаются вверх в PLAY'A Actors/Studios и получают префикс `★`.

### Агрегированный поиск по актёру/студии

Сырые карточки источников из `HIDDEN_LIBRARY_SITE_IDS` (по умолчанию XXXClub) по-прежнему не
попадают в общую ленту. Но Actors/Studios строятся по полному локальному индексу, и запрос
`/api/playa/v2/videos?actor=<id>` или `?studio=<id>` возвращает совпавшие релизы сразу из
PornoLab и XXXClub. Такая карточка открывается и воспроизводится через обычный `/stream/<id>` +
TorrServe. Имена сравниваются без регистра, пробелов, пунктуации и доменных суффиксов, поэтому
`VR Bangers`, `VRBangers` и `VRBangers.com` считаются одной сущностью.

При открытии актёра выдача также дополняется trailer-карточками внешнего PLAY'A-каталога.
Список трейлеров кэшируется в `external_scenes` на 24 часа, но временные подписанные MP4 URL
никогда не сохраняются: `/api/playa/v2/video/trailer__<provider>__<id>` запрашивает свежий URL
при открытии карточки. Сейчас первым provider подключён VRPorn; карточка содержит только
бесплатный `trailer` detail, premium/full URL внешнего сайта не проксируется. Отключение:

```bash
ENABLE_EXTERNAL_TRAILERS=0
```

Для настоящих псевдонимов, которые нельзя определить нормализацией, есть постоянная таблица aliases:

```text
GET  /catalog/aliases?kind=actor
POST /catalog/aliases
Content-Type: application/json

{"kind":"actor","alias":"Blake B.","canonical_title":"Blake Blossom"}
```

Карточки PLAY'A: крупная строка — студия, мелкая строка — актёры. Сиды/пиры, разрешение и короткая
дата (`06.10.26`) убраны из видимого текста карточки и отдаются только как вспомогательные `tooltip` /
`description` поля для клиентов, которые умеют показывать их при наведении. HTML/CSS в PLAY'A API не
поддерживается, поэтому цветовой акцент сидов/пиров сделан unicode-индикаторами (`🟢S7 🔴P2`).

## Что ещё можно настроить

- **`config.FORUM_MAX_PAGES`** — сколько страниц раздела (по 50 тем) парсить за раз. На старте
  стоит `3`, чтобы проверка была быстрой; потом можно увеличить или поставить `None` (весь раздел,
  782+ страницы на момент проверки — это долго, лучше ограничивать и обновлять порциями/по кэшу).
- **`config.MAX_TAG_SCENES`** — сколько самых частых тегов превращать в отдельные вкладки DeoVR.
- **`config.DEFAULT_SCREEN_TYPE/STEREO_MODE`** — выставлены "180° / SBS" как самый частый формат
  в этом разделе; если у конкретной раздачи в заголовке/метаданных указано иное (360°, TB/OverUnder),
  это считывается автоматически (`_guess_screen_stereo`).
- Теги сайта уже достаточно чистые (`vr`, `hardcore`, `180°`, `8k`, `oculus rift / vive`, ...),
  поэтому парсинг тегов из квадратных скобок заголовка используется только как запасной вариант,
  если у темы почему-то нет штатных тегов.

## Ограничения / на что обратить внимание

- Контент 18+ — убедитесь, что раздача доступна по условиям сайта и в рамках локального
  законодательства; сервис предназначен для личного некоммерческого использования.
- "Теги" в DeoVR реализованы как отдельные вкладки (scenes) сверху экрана — это и есть
  классический способ показа тегов в DeoVR, галочек на каждой карточке нативно нет.
- Если возникнут проблемы с логином — проверьте вручную в браузере, не просит ли сайт капчу
  при входе с нового IP/User-Agent; при необходимости можно один раз залогиниться руками в
  браузере, выгрузить куки и подставить их в `scraper._session.cookies` вместо автологина.
