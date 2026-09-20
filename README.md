# Telegram-канал о Дубае и недвижимости ОАЭ

Механика русскоязычного Telegram-канала частного специалиста по недвижимости
в Дубае: от сбора источников до публикации и аналитики.

Робот сам собирает новости, пишет посты, рисует к ним карточки и присылает
владельцу превью с кнопками. **Без нажатия «Опубликовать» в канал не уходит
ничего.** Публикация — Python 3.11 + GitHub Actions, без сервера и без базы
данных: состоянием служит сам репозиторий.

> **Прежде чем запускать:** источники в `config/sources.yml` собраны, но
> **ни один из них не проверен HTTP-запросом** и все выключены — в окружении
> сборки не было доступа в интернет. Первым делом выполните шаг 7 в
> [`docs/SETUP.md`](docs/SETUP.md). Подробности — в [`docs/SOURCES.md`](docs/SOURCES.md).

---

## Быстрый старт

Для владельца канала — [`docs/SETUP.md`](docs/SETUP.md): пошагово, от @BotFather
до первого поста, без опыта разработки.

Для разработчика:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

make dry-run          # весь контур без единого обращения в Telegram
make test             # тесты
make verify-sources   # реальная HTTP-проверка реестра источников
```

`make dry-run` кладёт результат в `out/drafts.json` и `assets/generated/`.

---

## Как это работает

```
[1 COLLECT]  источники из config/sources.yml → сырые items
[2 NORMALIZE + DEDUPE]  единая схема, хэш-дедупликация → state/seen_items.json
[3 SCORE]  свежесть × вес источника × ключевые слова × веса рубрик из аналитики
[4 GENERATE]  промпт рубрики из prompts/ → Claude → JSON → факт-чек-гейт
[5 ILLUSTRATE]  фирменная карточка на Pillow, фолбэк — Unsplash/Pexels
[6 QUEUE]  слот публикации по времени Asia/Dubai → state/queue.json
[7 APPROVE]  превью владельцу, кнопки, getUpdates long-poll
[8 PUBLISH]  идемпотентная отправка в канал → state/published.json
[9 ANALYTICS]  недельный отчёт, пересчёт весов рубрик → обратно в SCORE
```

Три вещи, которые отличают этот пайплайн от «скрипта, который постит»:

- **Факт-чек-гейт.** Любая цифра в посте обязана встречаться во входных
  материалах или быть объявлена в блоке `facts` со ссылкой. Пост с выдуманным
  числом, чужой ссылкой или обещанием гарантированной доходности в очередь
  не попадает.
- **Идемпотентность.** Ключ публикации пишется на диск до обращения в Telegram,
  поэтому повторный запуск workflow не создаёт дубль, а сбой — не теряет пост.
- **Честный сухой прогон.** `DRY_RUN=1` прогоняет всё целиком, не открывая
  ни одного сокета; тест подменяет `socket.socket` и это проверяет.

Подробный разбор решений — [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Карта репозитория

### Код пайплайна

| Файл | Что делает |
|---|---|
| `pipeline/run.py` | CLI: `python -m pipeline.run --dry-run`, `--stage <имя>` |
| `pipeline/config.py` | пути, настройки из окружения, реестр рубрик, валидация `sources.yml` |
| `pipeline/collect.py` | загрузка источников (rss/atom/json_api/html), в DRY_RUN — офлайн-фикстура |
| `pipeline/normalize.py` | приведение к единой схеме, дедупликация, очистка от prompt injection |
| `pipeline/score.py` | скоринг материалов и выбор рубрик на прогон |
| `pipeline/prompts.py` | загрузка промптов из `prompts/` и подстановка плейсхолдеров |
| `pipeline/llm.py` | абстракция провайдера (Anthropic / OpenAI / офлайн), разбор JSON-ответа |
| `pipeline/offline_draft.py` | детерминированная заглушка модели для DRY_RUN и тестов |
| `pipeline/generate.py` | генерация поста, вторая попытка по замечаниям гейта |
| `pipeline/factcheck.py` | факт-чек-гейт: цифры, ссылки, юридические формулировки, лимиты |
| `pipeline/illustrate.py` | фирменная карточка на Pillow + фолбэк на стоки с фиксацией лицензии |
| `pipeline/postqueue.py` | очередь, слоты публикации по Asia/Dubai, статусы |
| `pipeline/approve.py` | превью владельцу, кнопки, long-poll `getUpdates`, проверка user_id |
| `pipeline/publish.py` | публикация по слотам, ключ идемпотентности |
| `pipeline/analytics.py` | недельный отчёт и пересчёт весов рубрик |
| `pipeline/telegram.py` | клиент Bot API: ретраи, rate limit, полная блокировка сети в DRY_RUN |
| `pipeline/textutil.py` | лимиты Telegram, разбиение текста, санитайз HTML, хэши и канонизация URL |
| `pipeline/state.py` | атомарное чтение/запись `state/*.json` |
| `pipeline/logging_setup.py` | логирование с вымарыванием секретов |
| `pipeline/verify_sources.py` | HTTP-проверка всего реестра источников, `make verify-sources` |

### Конфигурация и данные

| Файл | Что это |
|---|---|
| `config/sources.yml` | реестр источников: id, URL, тип, рубрика, вес, лицензия, статус проверки |
| `config/settings.yml` | несекретные настройки: число постов за прогон, слоты, параметры скоринга |
| `config/sample_items.json` | офлайн-фикстура для сухого прогона (домены `example.com` — намеренно) |
| `assets/brand.yml` | цвета и подпись фирменной карточки |
| `state/*.example.json` | образцы структуры файлов состояния; реальные — в `.gitignore` |
| `.env.example` | список переменных окружения с плейсхолдерами |

### Промпты

`prompts/system_tone.md` — системный промпт с tone of voice и анти-галлюцинационными
правилами. `prompts/<rubric_id>.md` — шаблон задачи для каждой из десяти рубрик.
Промпты не захардкожены в коде: добавить рубрику = добавить файл.

### Тесты

| Файл | Что проверяет |
|---|---|
| `tests/test_dedupe.py` | дедупликация, канонизация URL, очистка от инъекций |
| `tests/test_telegram_limits.py` | 1024/4096, разбиение текста, санитайз HTML |
| `tests/test_idempotency.py` | повторная публикация, освобождение ключа при ошибке |
| `tests/test_llm_parsing.py` | разбор JSON-ответа модели во всех кривых формах |
| `tests/test_factcheck.py` | цифры без источника, чужие ссылки, запрещённые формулировки |
| `tests/test_dry_run_no_network.py` | в DRY_RUN не открывается ни один сокет |
| `tests/test_sources_config.py` | схема реестра, покрытие категорий, запрет включать непроверенное |
| `tests/test_prompts_rubrics.py` | согласованность рубрик между кодом, промптами и документацией |

### Автоматизация

| Workflow | Расписание (UTC → Дубай) |
|---|---|
| `.github/workflows/collect-generate.yml` | `0 3`, `0 12` → 07:00 и 16:00 |
| `.github/workflows/approve-poll.yml` | `*/30 4-18` → каждые 30 мин, 08:00–22:30 |
| `.github/workflows/publish.yml` | `25,35 5` и `25,35 15` → 09:25/09:35, 19:25/19:35 |
| `.github/workflows/analytics-weekly.yml` | `0 4 * * 1` → пн 08:00 |
| `.github/workflows/verify-sources.yml` | `0 2 * * 0` → вс 06:00 |
| `.github/workflows/ci.yml` | тесты, сухой прогон и поиск секретов на каждый push |

`scripts/commit_state.sh` коммитит `state/` обратно в репозиторий.
`scripts/check_no_secrets.py` ищет в репозитории похожее на живые ключи.

---

## Документация

| Документ | О чём |
|---|---|
| [`PRODUCT_BRIEF.md`](PRODUCT_BRIEF.md) | продуктовый бриф — источник правды по целям и ограничениям |
| [`docs/SETUP.md`](docs/SETUP.md) | пошаговый запуск с нуля для человека без опыта разработки |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | как устроен пайплайн и почему приняты такие решения |
| [`docs/SOURCES.md`](docs/SOURCES.md) | реестр источников, что машиночитаемо по афише Дубая, правовые ограничения |
| [`docs/CONTENT_STRATEGY.md`](docs/CONTENT_STRATEGY.md) | стратегия канала, доли рубрик, воронка до заявки |
| [`docs/RUBRICS.md`](docs/RUBRICS.md) | десять рубрик: цель, формат, длина, частота, пример поста |
| [`docs/TONE_OF_VOICE.md`](docs/TONE_OF_VOICE.md) | голос канала, словарь терминов, «плохо → хорошо» |
| [`docs/CONTENT_CALENDAR.md`](docs/CONTENT_CALENDAR.md) | сетка на 4 недели по дням и слотам |
| [`docs/PERSONAL_POSTS.md`](docs/PERSONAL_POSTS.md) | 8 брифов личных постов — текст пишет владелец сама |
| [`docs/LEAD_MAGNETS.md`](docs/LEAD_MAGNETS.md) | лид-магниты и сбор контактов на лончи |
| [`docs/ANALYTICS.md`](docs/ANALYTICS.md) | что Bot API реально отдаёт, а что снимается руками |
| [`docs/METRICS.md`](docs/METRICS.md) | справочник метрик с ориентирами |
| [`docs/SECURITY.md`](docs/SECURITY.md) | секреты, права бота, защита апрува и от инъекций |
| [`docs/LEGAL.md`](docs/LEGAL.md) | пересказ новостей, лицензии картинок, формулировки про доходность |

---

## Рубрики

`market_pulse` · `new_launch` · `investor_math` · `rules_and_laws` · `area_guide` ·
`dubai_life` · `events_afisha` · `faq_answer` · `case_story` · `personal`

Идентификаторы рубрик — контракт между `pipeline/config.py`, папкой `prompts/`
и `docs/RUBRICS.md`; расхождение ловится тестом.
Продающих рубрик три (`new_launch`, `investor_math`, `case_story`) — не более
четверти ленты, как требует бриф.

---

## Секреты

В репозитории их нет и быть не должно. Всё берётся из окружения, в CI — из
GitHub Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `TELEGRAM_OWNER_ID`,
`ANTHROPIC_API_KEY`, опционально `UNSPLASH_ACCESS_KEY` и `PEXELS_API_KEY`.
Локально — в `.env` (он в `.gitignore`). Логи вымарывают значения секретов,
а `scripts/check_no_secrets.py` падает в CI, если ключ всё-таки попал в коммит.
