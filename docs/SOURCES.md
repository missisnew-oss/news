# Реестр источников контент-пайплайна

> **СТАТУС НА 2026-09-20: НИ ОДИН ИСТОЧНИК НЕ ПРОВЕРЕН HTTP-ЗАПРОСОМ.**
> Реестр собирался в окружении без сетевого egress (прокси возвращал 403 на CONNECT
> для всех внешних хостов). Поэтому у всех 34 записей `verification.status: unverified`,
> все поля `http_code` / `content_type` / `items_found` / `latest_item_at` = `null`,
> и **все источники `enabled: false`**.
> Пайплайн сейчас не заберёт ничего. Перед первым запуском обязателен прогон верификации
> (раздел 1) и ручное переключение `enabled: true` у тех, кто отдал `status: ok`.

---

## 1. Как пользоваться реестром и как перепроверять

`config/sources.yml` — единственный источник правды о том, откуда пайплайн берёт данные.
Код читает `sources[]`, фильтрует по `enabled: true`, группирует по `category`
и считает итоговый вес как `categories[<category>].weight * source.weight`.

### Массовая перепроверка — одна команда

```bash
make verify-sources        # или: python -m pipeline.verify_sources
```

Скрипт `pipeline/verify_sources.py` проходит по всем `sources[]`, делает реальный
HTTP-запрос и **переписывает блок `verification` на месте**:

- `status: ok` — HTTP 200 **и** (для `rss`/`atom`) `len(entries) > 0`, (для `json_api`)
  тело парсится как JSON и `items_path` разрешается в непустой список;
- `status: failed` — HTTP >= 400, таймаут, сетевая ошибка, или фид пустой/не парсится;
  такой источник автоматически получает `enabled: false`;
- `status: unverified` — проверка не выполнялась (нет ключа, нет сети).

Результат дополнительно пишется в `state/sources_health.json`.
Еженедельно то же самое делает workflow `.github/workflows/verify-sources.yml`.

### Проверка одного источника руками

```bash
curl -sS -L -o /tmp/f -w '%{http_code} %{content_type}\n' --max-time 20 \
  -A 'Mozilla/5.0 (compatible; DubaiNewsBot/1.0)' '<URL>'
```

Для RSS/Atom дополнительно:

```bash
python3 -c "import feedparser;d=feedparser.parse('/tmp/f');print(d.bozo, len(d.entries), d.entries[0].title if d.entries else '')"
```

Для JSON API:

```bash
python3 -c "import json;d=json.load(open('/tmp/f'));print(type(d), list(d)[:10])"
```

Правило гигиены, которое проверяется тестом `tests/test_sources_config.py`:
**`enabled: true` допустим только при `status: ok`**. Источники, которые по факту
оказались нерабочими, из `sources.yml` удаляются целиком и переезжают в раздел 3.

Периодичность: полный прогон раз в неделю + быстрый прогон только по `enabled: true`
перед ежедневной сборкой.

### Про поле `extract`

Заполнено только для `type: html` и `type: json_api`. Для HTML это **гипотезы о селекторах**,
написанные вслепую (без доступа к разметке) — их нужно проверить и поправить при первом
успешном запросе. Для `json_api` пути записаны точечной нотацией (`_embedded.events`).

---

## 2. Источники по категориям

> «Рабочие» здесь = **отобранные и сверенные с поисковым индексом**, но
> **не** прошедшие HTTP-проверку. Колонка «Надёжн.» — оценка авторитетности издания
> (`reliability` 1–5), а не подтверждение доступности.

### 2.1 `realty_news` — Новости недвижимости ОАЭ

| id | Название | URL | Тип | Яз. | Частота | Надёжн. | Что брать | Ограничения |
|---|---|---|---|---|---|---|---|---|
| `gulf_news_rss` | Gulf News | `https://gulfnews.com/rss` | rss | en | hourly | 4 | Динамика цен, объёмы сделок, комментарии брокеров | Пересказ + ссылка |
| `khaleej_times_rss` | Khaleej Times | `https://www.khaleejtimes.com/rss` | rss | en | hourly | 4 | Аренда, Ejari, споры арендаторов, новые правила | Пересказ + ссылка |
| `arabian_business_realty_tag` | Arabian Business, тег real estate trends | `https://www.arabianbusiness.com/tags/dubai-real-estate-trends/feed` | rss | en | daily | 3 | Аналитика трендов, интервью | Пересказ + ссылка |
| `zawya_real_estate` | Zawya Real Estate | `https://www.zawya.com/en/business/real-estate` | html | en | daily | 3 | Пресс-релизы игроков рынка, отчёты консультантов | Пресс-материалы, со ссылкой |
| `property_finder_blog` | Property Finder blog | `https://www.propertyfinder.ae/blog/` | html | en | weekly | 3 | Квартальные market reports, средние цены по районам | Пересказ цифр + ссылка |

### 2.2 `official_data` — Официальные данные и регуляторы

| id | Название | URL | Тип | Яз. | Частота | Надёжн. | Что брать | Ограничения |
|---|---|---|---|---|---|---|---|---|
| `dubai_pulse_dld_transactions_api` | Dubai Pulse — DLD Transactions API | `https://www.dubaipulse.gov.ae/data/dld-transactions/dld_transactions-open-api` | json_api | en | daily | 5 | Сделки: дата, район, тип, площадь, сумма | Open data, **нужен OAuth** (API Key + Secret), атрибуция DLD |
| `dubai_pulse_dld_transactions_bulk` | Dubai Pulse — bulk CSV | `https://www.dubaipulse.gov.ae/data/dld-transactions/dld_transactions-open` | html→csv | en | daily | 5 | Полная история сделок для своих расчётов | Open data, атрибуция DLD |
| `dld_open_data_portal` | DLD Open Data | `https://dubailand.gov.ae/en/open-data/real-estate-data/` | html | en | weekly | 5 | Индексы, официальные отчёты DLD | Open data |
| `dld_api_gateway` | DLD API Gateway (Dubai REST) | `https://dubailand.gov.ae/en/eservices/api-gateway/` | html | en | irregular | 5 | Точка регистрации на официальные API (оценка, Ejari, проекты) | По договору / `api_terms` |
| `bayanat_ckan_api` | Bayanat.ae (CKAN) | `https://data.bayanat.ae/api/3/action/package_search?q=dubai&rows=20` | json_api | en | irregular | 4 | Федеральная статистика, демография, экономика | Open data |
| `cbuae_press_releases` | Central Bank UAE | `https://www.centralbank.ae/en/news-and-publications/...` | html | en | weekly | 5 | Ставки, ипотечные правила, LTV-лимиты, макро | Пресс-релизы регулятора, со ссылкой |

### 2.3 `developers` — Застройщики и лончи

| id | Название | URL | Тип | Яз. | Частота | Надёжн. | Что брать | Ограничения |
|---|---|---|---|---|---|---|---|---|
| `emaar_press_releases` | Emaar | `https://www.emaar.com/en/press-release-listing` | html | en | weekly | 5 | Новые лончи, сроки сдачи, финотчёты | Пресс-кит, можно фото со ссылкой |
| `emaar_ma_press_feed` | Emaar (ma.emaar.com), WP-фид | `https://ma.emaar.com/en/tag/press-release/feed/` | rss | en | weekly | 4 | То же, но машиночитаемо | Пресс-кит |
| `nakheel_media_centre` | Nakheel | `https://www.nakheel.com/en/media-centre` | html | en | weekly | 5 | Мастер-планы, острова, инфраструктура | Пресс-кит |
| `aldar_media_hub` | Aldar | `https://www.aldar.com/en/media` | html | en | weekly | 5 | Лончи (в основном Абу-Даби), финотчёты | Пресс-кит |
| `zawya_developer_press` | Zawya — пресс-релизы компаний | `https://www.zawya.com/en/press-release/companies-news` | html | en | daily | 3 | Лончи DAMAC / Sobha / Binghatti / Danube, у которых нет своих фидов | Пресс-материалы, со ссылкой |

**Важно:** у DAMAC, Sobha, Binghatti, Danube адрес пресс-центра не удалось подтвердить
даже по индексу — они в разделе 6. `zawya_developer_press` временно закрывает эту дыру.

### 2.4 `city_gov` — Город, законы, визы, транспорт

| id | Название | URL | Тип | Яз. | Частота | Надёжн. | Что брать | Ограничения |
|---|---|---|---|---|---|---|---|---|
| `wam_rss` | WAM (гос. информагентство) | `https://www.wam.ae/en/rss` | rss (хаб) | en | hourly | 5 | Указы, визовые правила, федеральные законы | Цитирование со ссылкой на WAM |
| `dubai_media_office_news` | Dubai Media Office | `https://mediaoffice.ae/en/news` | html | en | daily | 5 | Решения правительства Дубая, городские программы | Официальные пресс-релизы |
| `gulf_news_uae` | Gulf News — UAE | `https://gulfnews.com/uae` | html | en | hourly | 4 | Визы, штрафы, RTA, DEWA, тарифы — в бытовой подаче | Пересказ + ссылка |
| `emirates247_rss` | Emirates 24\|7 | `https://www.emirates247.com/rss` | rss (хаб) | en | daily | 3 | Экономика, сервисы, бытовые новости | Пересказ + ссылка |

RTA и DEWA напрямую подтвердить не удалось — см. раздел 6. Пока их новости приходят
через `wam_rss` и `gulf_news_uae`, которые их регулярно перепечатывают.

### 2.5 `events` — Афиша и события Дубая

| id | Название | URL | Тип | Яз. | Частота | Надёжн. | Что брать | Ограничения |
|---|---|---|---|---|---|---|---|---|
| `visitdubai_events_calendar` | Visit Dubai / Dubai Calendar | `https://www.visitdubai.com/en/festivals-and-events/dubai-events-calendar` | html (SPA) | en | weekly | 5 | Сезонные фестивали: DSF, Dubai Food Festival, Summer Surprises, Ramadan/Eid | Пересказ + ссылка |
| `dubai_culture_events` | Dubai Culture | `https://dubaiculture.gov.ae/en/events` | html | en | weekly | 5 | Выставки, культурные и бесплатные события | Гос. орган, цитирование со ссылкой |
| `platinumlist_dubai_calendar` | Platinumlist — календарь | `https://dubai.platinumlist.net/calendar/today` | html | en | daily | 4 | Всё подряд по датам, поддерживает `?date=YYYY-MM-DD` | Тикетинг: только анонс + ссылка |
| `platinumlist_arena_events` | Platinumlist — Coca-Cola Arena | `https://dubai.platinumlist.net/coca-cola-arena-events` | html | en | weekly | 4 | Концерты и шоу главной арены | Анонс + ссылка |
| `coca_cola_arena_events` | Coca-Cola Arena (офиц.) | `https://cocacolaarenadubai.com/events` | html | en | weekly | 5 | Первоисточник по арене, пресс-фото | Пресс-кит площадки |
| `dwtc_events` | Dubai World Trade Centre | `https://www.dwtc.com/en/events/` | html | en | weekly | 5 | Крупные выставки: GITEX, Big 5, Beautyworld, Arab Health | Официальный календарь, со ссылкой |
| `expocity_events` | Expo City Dubai | `https://www.expocitydubai.com/en/things-to-do/events-and-workshops/` | html | en | weekly | 4 | Семейные события, воркшопы, сезонка | Пресс-центр площадки |
| `eventbrite_dubai` | Eventbrite Dubai | `https://www.eventbrite.com/d/united-arab-emirates--dubai/events/` | html | en | daily | 3 | Комьюнити-события, нишевые митапы | Анонс + ссылка |
| `ticketmaster_discovery_ae` | Ticketmaster Discovery API v2 | `https://app.ticketmaster.com/discovery/v2/events.json?countryCode=AE` | json_api | en | daily | 4 | Концерты и спорт в чистом JSON | **Нужен бесплатный API-ключ**, `api_terms` |

### 2.6 `lifestyle` — Лайфстайл и сервисы

| id | Название | URL | Тип | Яз. | Частота | Надёжн. | Что брать | Ограничения |
|---|---|---|---|---|---|---|---|---|
| `whatson_ae_feed` | What's On Dubai | `https://whatson.ae/feed` | rss | en | daily | 3 | Рестораны, бранчи, новые открытия, ночная жизнь | Пересказ + ссылка |
| `timeout_dubai` | Time Out Dubai | `https://www.timeoutdubai.com/` | html | en | daily | 4 | Подборки «что делать», обзоры, скидки | Пересказ + ссылка, **фото не брать** |
| `lovin_dubai` | Lovin Dubai | `https://lovin.co/dubai/` | html | en | daily | 3 | Вирусные городские новости, бытовые лайфхаки | Пересказ + ссылка |
| `dubai_edited_rss_hub` | Dubai Edited (хаб фидов) | `https://dubaiedited.ae/rss-feeds/` | html | en | daily | 3 | Еда, бранчи, аттракционы — по рубрикам | Пересказ + ссылка |
| `dubai_city_guide_rss` | Dubai City Guide | `https://www.dubaicityguide.com/site/main/rss-news.asp` | rss | en | weekly | 2 | Практика для резидентов, справочная | Пересказ + ссылка |

---

## 3. Проверено, не работает

**Раздел пуст, и это не хорошая новость.** Проверить не удалось вообще ничего —
исходящая сеть в окружении сборки заблокирована:

| Что проверялось | Результат | Причина |
|---|---|---|
| `https://gulfnews.com/rss/property` (пример из ТЗ) | `curl` exit 56, `http_code=000` | `CONNECT tunnel failed, response 403` — egress-прокси отклонил соединение по политике организации |
| `emaar.com`, `dubailand.gov.ae`, `wam.ae`, `u.ae`, `timeoutdubai.com`, `cocacolaarenadubai.com`, `dubai.platinumlist.net`, `khaleejtimes.com` | все `000` | то же самое, `connect_rejected` |
| Те же хосты через инструмент `WebFetch` | `EGRESS_BLOCKED` | тот же прокси |
| `pypi.org`, `api.github.com` | `200` | в allowlist (инфраструктура сборки) |

Диагностика прокси: `curl -sS "$HTTPS_PROXY/__agentproxy/status"` →
`recentRelayFailures: [{kind: "connect_rejected", detail: "gateway answered 403 to CONNECT (policy denial or upstream failure)"}]`.

**Чем заменили:** ничем — заменить нечем. Вместо HTTP-проверки каждый URL сверен
с поисковым индексом (инструмент `WebSearch` работает через отдельный канал).
В `verification.note` каждого источника указано, что именно подтверждено индексом
(URL встречается в выдаче) и что достроено по паттерну сайта. Это снижает риск
выдуманных адресов, но **не заменяет проверку**.

Единственные известные заведомо мёртвые эндпоинты (по документации вендора в поисковой
выдаче, а не по нашему запросу) — их нет в `sources.yml`:

| URL | Причина | Чем заменили |
|---|---|---|
| `https://www.eventbriteapi.com/v3/events/search/` | Публичный поисковый API Eventbrite отключён с 2020 года | Парсинг JSON-LD со страницы `eventbrite.com/d/united-arab-emirates--dubai/events/` |
| Ticketmaster **International** Discovery API | Новые ключи не выдаются | Обычный Discovery API v2 с `countryCode=AE` |

---

## 4. События Дубая: что реально машиночитаемо

Короткий ответ: **готового RSS/ICS по афише Дубая практически нет.** Ни один из
крупных дубайских календарей, судя по всему, не публикует ни RSS, ни ICS. Реально
машиночитаемыми остаются два пути: **официальный JSON API тикетинга (Ticketmaster)**
и **structured data (JSON-LD / `__NEXT_DATA__`) на HTML-страницах**. Все эндпоинты ниже —
**кандидаты**, проверить первым делом.

### 4.1 Что даёт настоящий JSON

**Ticketmaster Discovery API v2** — единственный публичный, документированный,
ключ-по-запросу JSON API с заявленным покрытием ОАЭ:

```
GET https://app.ticketmaster.com/discovery/v2/events.json?countryCode=AE&size=100&apikey=KEY
GET https://app.ticketmaster.com/discovery/v2/events.json?city=Dubai&classificationName=Music&apikey=KEY
```

Структура: `_embedded.events[]`, поля `name`, `url`, `dates.start.dateTime`,
`_embedded.venues[].name`, `classifications[].segment.name`, `priceRanges[]`.
Ключ бесплатный, регистрация на `developer.ticketmaster.com`.
**Что перекрывает:** концерты, международные туры, спорт.
**Чего не перекрывает:** сезонные фестивали DET, выставки DWTC, культурную программу
Dubai Culture — их там просто нет.
**Риск:** фактическая плотность по Дубаю не измерена — возможно, там единицы событий.
**Это надо проверить до того, как закладываться.**

**Bayanat / Dubai Pulse CKAN** — JSON есть, но событий в нём нет, это статистика.
Для афиши бесполезны.

### 4.2 Что отдаёт только HTML (но со structured data)

Правильная стратегия для всех остальных — **не писать хрупкие CSS-селекторы, а тянуть
`<script type="application/ld+json">` со `@type: Event`**. Тикетинг и площадки почти
всегда его ставят ради Google Rich Results, и он стабильнее вёрстки.

| Площадка / календарь | URL | Что ожидать | Покрытие |
|---|---|---|---|
| Platinumlist Dubai | `https://dubai.platinumlist.net/calendar/today` | JSON-LD `Event` + микроразметка `itemprop`; поддерживает `?date=YYYY-MM-DD` | Концерты, шоу, стендап, вечеринки — самое полное коммерческое покрытие |
| Platinumlist — арена | `https://dubai.platinumlist.net/coca-cola-arena-events` | то же | Coca-Cola Arena |
| Coca-Cola Arena (офиц.) | `https://cocacolaarenadubai.com/events` | JSON-LD `Event` (проверить), `/sitemap.xml` | Концерты, спорт, комедия — первоисточник |
| Visit Dubai / Dubai Calendar | `https://www.visitdubai.com/en/festivals-and-events/dubai-events-calendar` | SPA: искать `__NEXT_DATA__` или `/_next/data/<buildId>/...json` | **Сезонные фестивали**: DSF, Dubai Food Festival, Summer Surprises, Ramadan/Eid |
| Dubai Culture | `https://dubaiculture.gov.ae/en/events` | HTML-листинг | Культура, выставки, бесплатные события |
| DWTC | `https://www.dwtc.com/en/events/` | HTML-листинг | **Выставки**: GITEX, Big 5, Beautyworld, Arab Health |
| Expo City Dubai | `https://www.expocitydubai.com/en/things-to-do/events-and-workshops/` | HTML-листинг | Семейное, воркшопы, сезонка |
| Eventbrite Dubai | `https://www.eventbrite.com/d/united-arab-emirates--dubai/events/` | JSON-LD `Event` | Комьюнити, митапы, нишевое |

### 4.3 Практический вывод по типам событий

- **Концерты и спорт** → Ticketmaster Discovery API (JSON) как основа + Coca-Cola Arena
  и Platinumlist как дополнение и как первоисточник для ссылок.
- **Выставки и B2B** → только DWTC HTML. API нет. Зато DWTC публикует квартальный
  календарь пресс-релизом (через `mediaoffice.ae` и Gulf News) — это можно ловить
  текстовым источником и раз в квартал получать сразу всю сетку.
- **Сезонные фестивали (DSF, DFF, DSS, Ramadan/Eid)** → Visit Dubai. Это почти всегда
  анонс раз в сезон, а не поток. Здесь достаточно раз в неделю парсить `__NEXT_DATA__`
  (или вообще вести вручную — объём мизерный, а точность критична).
- **Культура и бесплатное** → Dubai Culture HTML.
- **Резервный текстовый канал:** `wam_rss` + `dubai_media_office_news` анонсируют
  все крупные городские события пресс-релизом. Если парсеры афиш падают, канал
  всё равно не останется без событийного контента.

### 4.4 Чего НЕ нашлось (и, вероятно, не существует)

- Публичного RSS или ICS у Dubai Calendar / Visit Dubai.
- Публичного API у Platinumlist.
- Публичного API или ICS у DWTC и Expo City.
- Рабочего поискового API у Eventbrite (отключён в 2020).

Если нужен по-настоящему надёжный событийный поток без парсинга — единственный
кандидат это Ticketmaster, и его покрытие по Дубаю надо измерить до того, как на него
закладываться.

---

## 5. Правовые ограничения по источникам

### 5.1 `summary_with_link` — только пересказ своими словами + ссылка

Коммерческие СМИ и порталы: Gulf News, Khaleej Times, Arabian Business, Property Finder,
Time Out Dubai, What's On, Lovin, Dubai Edited, Dubai City Guide, Emirates 24|7,
Platinumlist, Eventbrite.

Правила для канала:
- не копировать абзацы дословно, писать свой текст;
- обязательная ссылка на первоисточник в посте;
- **не переиспользовать фотографии** — у СМИ они лицензированы отдельно (стоки, агентства);
- цифры и факты (цены, проценты, объёмы) использовать можно — факты не охраняются,
  но источник цифры указывать обязательно.

### 5.2 `press_release` — пресс-материалы, разрешено цитировать

Застройщики (Emaar, Nakheel, Aldar), Zawya press-release, Central Bank UAE,
WAM, Dubai Media Office, Dubai Culture, Coca-Cola Arena, DWTC, Expo City.

Пресс-релиз публикуется ради распространения, поэтому:
- цитирование и прямые выдержки допустимы;
- изображения из официального пресс-кита обычно разрешены для редакционного использования —
  **но проверить условия на самой странице media centre**, у застройщиков часто есть
  оговорка «for editorial use only»;
- ссылка на первоисточник всё равно обязательна;
- **нельзя** подавать пресс-релиз застройщика как независимую оценку — для канала
  частного специалиста это репутационно важно: маркировать как «застройщик сообщает».

### 5.3 `open_data` — открытые данные, свободное использование

Dubai Pulse (DLD transactions), DLD Open Data, Bayanat.ae.

- Данные можно свободно использовать, переупаковывать, строить на них графики и
  собственную аналитику — это главная ценность канала;
- требуется **атрибуция источника** (DLD / Dubai Pulse / Bayanat);
- для Dubai Pulse API нужна регистрация и OAuth (client_credentials, API Key + Secret) —
  это условие доступа, а не лицензии;
- собственные расчёты на открытых данных (средняя цена за м² по району, динамика) —
  ваш контент, его можно публиковать как есть.

### 5.4 `api_terms` — по условиям API

DLD API Gateway, Ticketmaster Discovery API.

- Использование ограничено соглашением, которое принимается при получении ключа.
  У Ticketmaster обычно требуется атрибуция и запрещён ресейл данных;
- лимиты запросов соблюдать, ключи не коммитить в репозиторий — только через окружение.

### 5.5 Общее правило для канала

Так как канал ведёт частный специалист, а не СМИ, самый безопасный формат —
**«факт + цифра + ссылка + мой комментарий»**. Комментарий эксперта — это ваш
оригинальный контент, он же снимает большую часть претензий к объёму заимствования.

Полные правила по цитированию, изображениям и маркировке рекламы — в `docs/LEGAL.md`.

---

## 6. Кандидаты, требующие ручной проверки

Формально в этом статусе находятся **все 34 источника** (сеть была недоступна).
Ниже — те, у которых проблема не только в отсутствии HTTP-проверки, но и в том,
что сам URL не удалось подтвердить даже косвенно. **Их нет в `sources.yml`.**

### 6.1 Застройщики без подтверждённого адреса пресс-центра

Обязательные по брифу, но адрес не подтвердился — нужно открыть сайт руками и найти
раздел media/news, затем добавить в `sources.yml`:

| Застройщик | Предполагаемый вход | Что проверить |
|---|---|---|
| DAMAC | `damacproperties.com` | Есть ли `/en/media-centre` или `/en/news`; DAMAC публичная компания — искать также Investor Relations |
| Sobha Realty | `sobharealty.com` | Раздел News / Media |
| Binghatti | `binghatti.com` | Раздел News |
| Danube Properties | `danubeproperties.com` | Раздел Media / Press |
| Meraas | `meraas.com` | Раздел Media |
| Dubai Properties / Dubai Holding | `dubaiholding.com` | Раздел Media Centre |

Для каждого: проверить, не WordPress ли сайт — если да, `/<раздел>/feed/` часто даёт
готовый RSS, и это сильно лучше HTML-парсинга.

### 6.2 Городские сервисы без подтверждённого адреса

| Орган | Предполагаемый вход | Что проверить |
|---|---|---|
| RTA (транспорт) | `rta.ae` | Раздел News & Media; URL длинные и нестабильные — искать стабильную точку входа |
| DEWA (электричество/вода) | `dewa.gov.ae` | Раздел About us → Media Publications → Latest News |
| u.ae (федеральный портал) | `u.ae/en` | Есть ли новостная лента или только справочные страницы (визы, резиденция) |
| Dubai REST (приложение DLD) | — | Есть ли публичный API вне `api-gateway`; вероятно, только мобильное приложение |

### 6.3 Хабы, которые нужно разобрать на конкретные фиды

Три источника в реестре — это **страницы со списком лент**, а не сами ленты. После
первой успешной загрузки их надо распарсить и завести отдельными записями:

- `wam_rss` → `https://www.wam.ae/en/rss`
- `emirates247_rss` → `https://www.emirates247.com/rss`
- `dubai_edited_rss_hub` → `https://dubaiedited.ae/rss-feeds/`

### 6.4 URL с недостаточной уверенностью (в реестре, но проверить в первую очередь)

| id | В чём сомнение |
|---|---|
| `gulf_news_rss` | Подтверждён только корень `/rss`. Путь `/rss/property` **не подтверждён** — возможно, придётся фильтровать общую ленту по категории |
| `khaleej_times_rss` | Путь `/rss` не подтверждён индексом, только раздел `/business/property` |
| `whatson_ae_feed` | `whatson.ae/feed` встречается в выдаче как упоминание, прямой ссылки в индексе нет |
| `emaar_ma_press_feed` | Суффикс `/feed/` достроен по WordPress-паттерну |
| `bayanat_ckan_api` | Путь `/api/3/action/package_search` — стандартный CKAN; версия API может отличаться |
| `lovin_dubai` | Подтверждён домен `lovin.co`, путь `/dubai/` — нет |
| `timeout_dubai` | Наличие RSS не подтверждено; если `/feed` нет, останется HTML |
| `ticketmaster_discovery_ae` | Покрытие Дубая заявлено, но фактически не измерено |

## 7. Telegram-каналы, указанные владельцем

Добавлены 21.09.2026 по списку владельца. Тип источника `telegram`, парсер — `pipeline/telegram_source.py`.

### 7.1 Как это работает

Bot API не умеет читать чужие каналы, поэтому пайплайн читает **публичное веб-превью** канала —
страницу `https://t.me/s/<username>`. Она открывается без логина, отдаёт последние ~20 постов
с текстом, датой, постоянной ссылкой и фото. Логин, `api_id` и Telethon не нужны.

Ограничения:
- работает только для публичных каналов с включённым превью. Приватный канал (`t.me/+…`) или канал,
  у которого превью выключено, отдаёт страницу «Preview channel» без постов — `verify_sources`
  помечает такой канал `failed` с понятной причиной;
- посты только с фото/стикером без текста пропускаются — пересказывать нечего;
- текст поста — недоверенный ввод: маркеры инструкций вычищаются в `normalize.scrub_untrusted`,
  цифры без первоисточника режет факт-чек-гейт.

### 7.2 Реестр

| id | Канал | Категория | Роль | Что берём |
|---|---|---|---|---|
| `tg_uaegeneralnews` | @uaegeneralnews | city_gov | news | новости ОАЭ: законы, визы, транспорт |
| `tg_russianemiratesnews` | @russianemiratesnews | city_gov | news | новости ОАЭ для русскоязычных |
| `tg_dubaimap` | @dubaimap | lifestyle | news | город, районы, места |
| `tg_offplanmariya` | @offplanmariya | developers | signal | какие лончи обсуждают |
| `tg_eltsovairina_80` | @eltsovairina_80 | realty_news | signal | темы рынка |
| `tg_neginskiuae` | @NeginskiUAE | realty_news | signal | темы рынка |
| `tg_burjuyinvest` | @burjuyinvest | realty_news | signal | инвест-повестка |
| `tg_dubai_invest1` | @dubai_invest1 | realty_news | signal | инвест-повестка |

**Роль `news`** — пост может стать основой пересказа своими словами со ссылкой на исходный пост.
**Роль `signal`** — каналы коллег и конкурентов. Они показывают, *о чём говорит рынок* (какой лонч
все обсуждают, какая тема на слуху), но факты и цифры для нашего поста берутся только из
первоисточника: застройщика, регулятора, СМИ. Переписывать чужой продающий пост — нельзя ни по
этике, ни по праву. Веса у `signal` ниже (0.6–0.7), надёжность 2/5.

### 7.3 Не добавлены — нужен адрес

Два канала из списка названы без `@username`, а без сети найти их нельзя:
- **Good Realtor Dubai**
- **homis и Дубай by Nastya Docs**

Чтобы добавить: открыть канал → «Информация о канале» → скопировать ссылку `t.me/…` и добавить
запись по образцу выше (`type: telegram`, `url: https://t.me/s/<username>`).

### 7.4 Статус проверки

Как и остальной реестр — **не проверено**: сеть в среде сборки заблокирована. Workflow
«Verify Sources» проверит все восемь за один запуск и включит те, у кого превью отдаёт посты.
