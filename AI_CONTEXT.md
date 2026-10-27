# Контекст проекта для AI

Этот файл — карта проекта для разработки и передачи контекста другой модели. Он описывает наблюдаемое поведение кода, а не желаемую спецификацию. При расхождении ориентируйся на Python-код и актуальные шаблоны конфигурации. Не считай заметки о реализации гарантией, пока не проверил соответствующий handler и тест.

## 1. Назначение и предметная область

Закрытый Telegram-бот учебной группы RC-215. Основные возможности:

- расписание по чётным/нечётным неделям, фильтрация по подгруппам, замены уроков и настройка времени пар;
- просмотр и администрирование домашних заданий, включая вложения, дедлайны и историю изменений;
- заявки на отсутствие, отметка ложных заявок, санкции и ручное управление блокировками;
- каталог документов, события группы и напоминания о событиях;
- админ-панель: рассылки, управление доступом, аудит действий, активность пользователей и добавление студентов.

В конфигурации и локальных данных могут быть Telegram-токен, ID и персональные данные. Не открывай и не цитируй реальные `config/bot.yaml`, `config/roles.yaml`, `config/students.yaml`, `.env` или `data/bot.db` без явной необходимости. Для документации используй только `config/*.example`; не переносить секреты и персональные данные в ответы, тесты, коммиты или внешние системы.

## 2. Стек и команды разработчика

- Python 3.11; aiogram 3.x; SQLAlchemy 2 async; aiosqlite; PyYAML; python-dotenv.
- Точка входа: `main.py`; локальный запуск: `python main.py`.
- Тесты: `python -m unittest discover -s tests -v`.
- SQLite по умолчанию: `data/bot.db`; можно задать `DATABASE_URL`. Некоторые миграции, в частности перестройка уникального ключа ДЗ, поддерживают только SQLite.
- Логи: stdout и `logs/bot.log` с ротацией.
- Docker: `Dockerfile`, `docker-compose.yml`; `deploy.sh` требует явно заданный `DEPLOY_REMOTE`. Deployment overlay включает только Python-файлы из `bot/` и `database/`, `main.py`, `config_loader.py` и `requirements.txt`; конфиги, `.env`, roster, данные, логи и example-файлы не включаются. Перед overlay скрипт проверяет эффективные Compose env-файлы и bind-mount пути, затем сохраняет текущую конфигурацию, БД, roster, логи и файлы исходников, которые будут перекрыты.

## 3. Архитектура и поток update

### Запуск

1. `main.py` включает логирование, загружает/валидирует `AppConfig`, вызывает `init_db()`.
2. `database.engine.init_db()` выполняет `Base.metadata.create_all`, затем последовательность ручных миграций.
3. Создаётся `async_sessionmaker`. При старте просроченные активные заявки переводятся в `completed`; затем запускается очистка старых данных.
4. Создаются `Bot` и `Dispatcher`, конфиг передаётся через dispatcher workflow data.
5. Регистрируются middleware и роутеры; запускается long polling.
6. Параллельно работают ежедневная очистка и отправка в группу напоминаний о событиях на завтра в 08:00 в timezone бота.
7. В `finally` отменяются фоновые задачи, закрывается Telegram-сессия и освобождается SQLAlchemy engine.

### Обработка update

- Для сообщений и callback-ов подключены `ErrorMiddleware`, `DbSessionMiddleware`, `AccessMiddleware`, `RoleMiddleware`.
- `DbSessionMiddleware` создаёт одну SQLAlchemy AsyncSession на update и закрывает её после handler-а.
- `AccessMiddleware` проверяет allowlist, блокировку, права на команду/callback/FSM-состояние; синхронизирует roster-профиль и записывает активность пользователя. Отдельные handlers отвечают за изменения и commit.
- `RoleMiddleware` вычисляет и передаёт `role` в handler data.
- Необработанная ошибка логируется, FSM-состояние по возможности очищается, пользователю отправляется общее сообщение.
- FSM хранится в стандартном хранилище aiogram Dispatcher; отдельное постоянное FSM-хранилище не настраивается.

## 4. Доступ и роли

### Кто может пользоваться ботом

Update допускается, если Telegram ID есть в `config/students.yaml`, статических списках ролей в `config/roles.yaml` или таблице `dynamic_roles`. Обычные студенты, отсутствующие во всех этих источниках, блокируются.

- Для студента из roster значения `full_name` и `subgroup` в YAML авторитетны и обновляются в `users` на каждом разрешённом update.
- Сотрудник вне roster может пользоваться ботом; существующие ФИО и подгруппа из БД не перезаписываются данными roster.
- Для всех пользователей обновляется `username`, фиксируются `last_action` и `last_active_at`.
- Заблокированный через `users.is_banned` пользователь не может работать с ботом, кроме просмотра ДЗ (`/hw` и предназначенные для просмотра callback-и homework UI).

### Матрица прав

Права централизованы в `bot/utils/permissions.py`; middleware проверяет команды, меню, callback-и и состояния форм. Скрытия кнопки недостаточно — проверка должна оставаться и на серверном обработчике.

| Роль | Возможности |
| --- | --- |
| `user` | Пользовательские функции без административных permissions. |
| `moderator` | Админ-панель, создание/управление ДЗ, замены уроков. |
| `starosta` | Всё moderator + блокировки, настройка времени пар, документы, события, добавление студента, просмотр/разбор заявок. |
| `owner` | Всё starosta + рассылки, настройка чётности, управление администраторами, история действий, активность. |

Статические роли задаются в YAML. Для нескольких статических ролей приоритет `owner` → `starosta` → `moderator`. Динамически назначаются только `moderator` и `starosta`; назначение и снятие динамических ролей доступно владельцу. Динамические роли хранятся в `dynamic_roles`.

Санкции за ложные заявки и административная блокировка — разные механизмы: счётчик/`banned_until` сам по себе не устанавливает `is_banned`. `is_banned` включает отдельное ограничение доступа и переключается админом.

## 5. Карта файлов

### Верхний уровень

- `main.py` — composition root: запуск, middleware, роутеры, фоновые задачи и shutdown.
- `config_loader.py` — dataclass-конфигурация, валидация YAML, чётность по расписанию и запись нового студента в roster.
- `AI_CONTEXT.md` — этот ориентир по архитектуре и правилам проекта.
- `requirements.txt` — runtime-зависимости.
- `Dockerfile`, `docker-compose.yml`, `deploy.sh` — контейнеризация и развёртывание.

### Telegram-слой

- `bot/handlers/base.py` — `/start`, справка, главное/админ-меню, профиль, общая навигация и отмена форм.
- `bot/handlers/schedule.py` — расписание, текущая подгруппа, персонализация, карточки пар и weekend-навигация.
- `bot/handlers/replacements.py` — FSM для замены/отмены урока или изменения аудитории/преподавателя.
- `bot/handlers/hw.py` — просмотр/поиск ДЗ, навигация по датам и предметам, вывод текста и Telegram-вложений.
- `bot/handlers/absent.py` — создание/отмена заявок, отчёт, отметка ложных заявок и санкционные действия.
- `bot/handlers/docs.py` — каталог документов, получение документа по file_id/URL и добавление материала.
- `bot/handlers/events.py` — просмотр и создание событий.
- `bot/handlers/admin.py` — админские FSM/callback-и: добавление и замена ДЗ, roster, сокращённые пары, чётность, рассылка, блокировки, динамические роли, аудит и активность. Файл большой; перед изменением найди конкретный form/callback и соответствующие тесты.
- `bot/ui.py` — общие inline-клавиатуры и callback-типы; меню строится с учётом permissions.
- `bot/keyboards/inline.py`, `bot/keyboards/reply.py` — билдер inline-кнопок и постоянная reply-клавиатура.

### Middleware и утилиты

- `bot/middlewares/access.py` — allowlist, ограничения `is_banned`, permissions для команд/меню/callback/FSM, синхронизация профиля и activity tracking.
- `bot/middlewares/role.py` — разрешение статической/динамической роли.
- `bot/middlewares/db.py` — одна AsyncSession на update.
- `bot/middlewares/error.py` — логирование исключений и безопасный ответ пользователю.
- `bot/utils/permissions.py` — матрица permissions.
- `bot/utils/schedule_parser.py` — typed schedule facade и сопоставление времени пары с номером слота.
- `bot/utils/time_utils.py` — вычисление времён уроков, локальной учебной даты, чётности и чтение сохранённой опорной недели.
- `bot/utils/homework_dates.py` — предметы фактического расписания с учётом замен, ближайшие занятия и целевая дата ДЗ.
- `bot/utils/subjects.py` — канонизация/поиск предметов, языковая подгруппа, предметы по расписанию.
- `bot/utils/admin_audit.py` — добавление записи в журнал админ-действий.
- `bot/utils/emojis.py` — проверка YAML-эмодзи, Unicode fallback, Telegram custom emoji и подписи кнопок.

### Данные и тесты

- `database/models.py` — ORM-сущности.
- `database/engine.py` — SQLite/`DATABASE_URL`, engine, создание схемы и вызов миграций.
- `database/migrations.py` — ручные эволюционные миграции схемы; Alembic не используется.
- `database/cleanup.py` — retention для history, overrides и старых заявок.
- `config/*.yaml.example` — безопасные шаблоны конфигурации; реальные YAML-файлы не должны попадать в документацию.
- `config/schedule.yaml` — расписание; `config/emojis.yaml` — эмодзи. Файл эмодзи загружается отдельным модулем.
- `tests/` — `unittest`-проверки доменной логики, middleware, миграций и UI. Перед правкой найди релевантные тесты; для полного прогона используй команду выше.

## 6. Хранение данных

| Таблица | Роль |
| --- | --- |
| `users` | Telegram ID, профиль, подгруппа, активность, `is_banned`, счётчик ложных заявок и `banned_until`. |
| `homework` | Текущая запись ДЗ: предмет + дата занятия, текст/вложения, проектный deadline, автор и timestamps. Уникальность `(subject, target_date)`. |
| `homework_history` | Предыдущие значения записи ДЗ при изменении/архивации дублей. |
| `schedule_overrides` | Изменение урока/аудитории/преподавателя или отмена пары на конкретную дату и номер слота. |
| `schedule_time_overrides` | Продолжительность всех/отдельной пары и длительность большой перемены для даты. |
| `bot_settings` | Сохранённые настройки, включая опорные дату-понедельник и чётность. |
| `dynamic_roles` | Дополнительная роль `moderator` или `starosta` для Telegram ID. |
| `admin_action_logs` | Администратор, действие, детали и время. |
| `absences` | Владелец заявки, дата, список номеров пар либо `["all"]`, статус и отметивший ложность. |
| `class_documents` | Категория, заголовок, Telegram file_id или URL, автор и время. |
| `class_events` | Название, дата, категория, автор и время. |

Telegram-медиа не копируется локально: сохраняются `file_id`/описания вложений. Связи авторов, кроме явно объявленных FK (например, absence → user, history → homework), не обеспечиваются FK в БД.

## 7. Основные бизнес-потоки

### Расписание, чётность и замены

- Расписание хранится по будням и отдельно для `odd`/`even`; у занятия есть время, предмет, аудитория, преподаватель и опциональная подгруппа.
- Начальные значения чётности заданы в YAML. `/set_parity` сохраняет в `bot_settings` опорный понедельник и его чётность; недельная чётность считается от понедельников. Сохранённая настройка имеет приоритет над YAML.
- Подгруппа может быть указана у урока явно или определена по маркерам языка в названии. Персонализированный вывод скрывает занятие другой языковой подгруппы; общие занятия остаются видны.
- `/schedule` принимает дату `YYYY-MM-DD` или `DD.MM.YYYY`. Выбор учебной даты без аргумента зависит от локального timezone и cutoff 15:00: в пятницу после cutoff и в выходные выбирается ближайший понедельник.
- Номер пары определяется по уникальным временам начала в конфигурации расписания. Замена/отмена хранится отдельной строкой; если для того же слота есть несколько записей, актуальна последняя по ID.
- Замена может отменить пару, сменить урок либо изменить связанные данные; уведомление об изменении отправляется в группу. Отменённые уроки участвуют в отображении как отменённые и исключаются из ряда вычислений ближайших занятий.
- `schedule_time_overrides` задаёт длительность всех или конкретной пары и большой перемены; расчёт времени сохраняет стандартные промежутки между парами.

### Домашние задания

- Идентичность текущего ДЗ — `(subject, target_date)`, где target date — дата соответствующего занятия.
- Форма добавления предлагает предметы ближайших учебных дней, позволяет выбрать предмет из расписания или вручную подтвердить предмет, которого там нет.
- Расчёт ближайшего урока ограничен горизонтом 35 дней; учитывает фактические занятия/замены там, где функция поиска использует `subjects_for_date`.
- `/hw` открывает ДЗ на выбранный системой ближайший учебный день; аргумент команды используется для поиска предмета. UI позволяет выбирать дату и предмет.
- Форма поддерживает текст, фото/документ и несколько вложений; поддерживается deadline для проектного задания.
- При конфликте текущую запись не перезаписывают молча: нужно подтверждение; прежняя версия попадает в `homework_history`.
- Миграция дублей сохраняет самую свежую строку по subject/date и архивирует остальные; уникальный индекс защищает от повторного ключа.
- Вывод длинного текста делится на несколько сообщений, вложения отправляются через Telegram file_id.

### Отсутствия и ограничения

- Пользователь выбирает дату (быстрый вариант или `ДД.ММ`), затем конкретные пары или весь день.
- Заявки привязаны к базовым урокам, а не к фактическому составу пар после замены. `["all"]` конфликтует с любым выбором пар; повторные активные заявки с пересечением отклоняются.
- Доступны список собственных заявок и админский список активных заявок группы; заявка может быть отмечена ложной, отменена автором в разрешённых состояниях или завершена системой при старте.
- На превышении `ban_threshold` увеличивается счётчик ложных заявок и записывается `banned_until`; в текущем коде это не тождественно `users.is_banned` и само по себе не запускает middleware-блокировку.
- Ручной `is_banned` переключается в админском интерфейсе. Владелец/староста могут менять блокировки и журналируемые админ-действия; модератор не имеет этих прав.
- `/unban_absent` и соответствующий UI сбрасывают счётчик ложных заявок и `banned_until`.

### Документы, события и рассылки

- Документы группируются по категориям `cloud`, `labs`, `manuals`, `templates`; выдаются как URL либо отправляются из Telegram по сохранённому file_id.
- События имеют категории `exam`, `coursework`, `holiday`, `other`, выводятся по временным интервалам от текущей даты; фоновая задача отправляет в групповой чат события на завтра ежедневно в 08:00 timezone бота.
- Админская рассылка поддерживает текст и поддерживаемые Telegram-вложения, показывает подтверждение до отправки.
- Добавление студента меняет `students.yaml` и обновляет roster текущего процесса. Файл должен быть доступен контейнеру для записи.

### UI и callback-и

- Основное взаимодействие построено на inline-кнопках с типизированными callback data; у некоторых пользователей также есть постоянная reply-клавиатура.
- `bot/utils/emojis.py` берёт Unicode/custom emoji из `config/emojis.yaml`; fallback нужен для мест, где Telegram custom emoji не поддерживается (например, подписи кнопок).
- При добавлении UI/действия учитывай три слоя: построение клавиатуры (`ui.py`/handler), callback handler и серверную permission-проверку. FSM-состояния чувствительных форм также сопоставлены с permissions.
- Общая кнопка отмены сбрасывает FSM. Переход в другой раздел также должен корректно завершать текущий процесс формы.

## 8. Конфигурация

`load_config()` ожидает 5 основных YAML-файлов и поддерживает необязательный `ai.yaml`; поля валидируются до старта. Ошибка или неправильный тип — ошибка запуска. `.env` загружается рядом с `config_loader.py`. Не выводить фактические значения.

| Файл | Поля и смысл |
| --- | --- |
| `bot.yaml` | `token`, IANA `timezone`, `group_chat_id`; `BOT_TOKEN` из окружения/`.env` переопределяет YAML-токен. |
| `roles.yaml` | Списки целочисленных ID `owner`, `starosta`, `moderators`. |
| `students.yaml` | Список `students`, у каждой записи — положительный уникальный `telegram_id`, непустой `full_name`, `subgroup` (`english`/`french`). |
| `sanctions.yaml` | `ban_threshold`, `ban_days`. |
| `schedule.yaml` | `week_parity.reference_date` (понедельник), `reference_parity` (`odd`/`even`), пары `monday`…`friday` с `odd`/`even`; у пары обязательны непустые `time`, `subject`, `room`, `teacher`, опциональна `subgroup`. `lesson_duration_minutes` положительный, по умолчанию 90. |
| `emojis.yaml` | Отдельный mapping ключей на Unicode emoji и необязательные числовые custom emoji ID. Не входит в `AppConfig`, загружается `bot/utils/emojis.py`. |
| `ai.yaml` (optional) | Global AI/reaction switches and validated request, estimated token, cache, retention, and project quota limits. `GEMINI_API_KEY` remains an environment-only secret. |

Для новых тестов не использовать реальные конфиги. Создавай временную конфигурацию/БД или строй `AppConfig` напрямую из безопасных тестовых значений.

## 9. Команды и поверхности

Основные команды, зарегистрированные handlers:

| Команда | Действие |
| --- | --- |
| `/start`, `/help`, `/whoami`, `/chatid` | Стартовое меню, справка, роль, ID чата. |
| `/schedule [дата]`, `/расписание [дата]`, `/my_group` | Расписание и подгруппа. |
| `/hw [предмет]` | Просмотр и поиск ДЗ. |
| `/hw_add` | FSM создания ДЗ; permission `homework_management`. |
| `/replace` | FSM замены; permission `replacements`. |
| `/docs`, `/docs_add` | Каталог материалов и добавление документа. |
| `/events`, `/event_add` | Просмотр событий доступен пользователям; создание через `/event_add` требует `events_manage`. |
| `/absent`, `/my_absences`, `/absences` | Заявки текущего пользователя и группы; просмотр/разбор группы ограничен permissions. |
| `/unban_absent <telegram_id>` | Сброс санкционного счётчика; требует права разбора заявок. |
| `/admin` | Админ-панель, доступные элементы зависят от роли. |
| `/broadcast`, `/activity`, `/set_parity` | Расссылка, активность, настройка чётности; соответствующие permissions. |

Управление динамическими ролями, банами, сокращёнными уроками и историей действий в основном доступно через админ-меню/callback-и, а не отдельные slash-команды.

## 10. Миграции, очистка и эксплуатационные детали

- Схема создаётся из SQLAlchemy metadata, затем запускаются ручные идемпотентные проверки/миграции. Отдельных версий схемы/Alembic нет.
- Миграции дополняют поля `users`, историю ДЗ, `target_date`/timestamps ДЗ, уникальность ДЗ, отмену расписания, динамические роли, аудит и настройки времени пар.
- Миграция уникальности ДЗ перестраивает SQLite-таблицу и явно отказывается от перестройки на других dialect-ах.
- Очистка запускается сразу после старта и затем по циклу `sleep(24h)` (не в фиксированное время). History и overrides хранятся 90 дней; завершённые/ложные отсутствия — 30 дней. Активные старые заявки отмечаются завершёнными при старте.
- `database.cleanup` вычисляет дату retention в переданном timezone; время создания history сравнивается в UTC.
- Ежедневная отправка напоминаний берёт события на завтра и отправляет их в `group_chat_id`; ошибки логируются, не останавливая бесконечный цикл.
- Добавление roster через бота требует writable `config/students.yaml`; остальные конфиги в Compose монтируются read-only.
- Скрипт деплоя сначала делает резервные копии базы и roster, исключает чувствительные/генерируемые данные из исходного архива и пересоздаёт контейнер.

## 11. Тестирование и рабочие правила для следующей модели

Перед правкой:

1. Найди точный handler/модель/permission и связанный тест; не делай вывод о поведении только по тексту этого файла.
2. Для доступа проверяй команду, callback и FSM-state отдельно: все должны иметь согласованные permissions.
3. Для расписания/ДЗ учитывай timezone, чётность, номер слота, замены и `schedule_time_overrides`; не дублируй парсинг расписания, используй `bot/utils`.
4. Для транзакционных изменений сохраняй текущие паттерны AsyncSession/commit, добавляй audit log для админ-действий там, где это уже принято.
5. Не читай и не перезаписывай локальные секреты, roster, database или чужие незакоммиченные изменения; изменения рабочей копии могут быть пользовательскими.
6. Добавляй тест на изменение поведения, затем запускай сначала релевантный тест, при необходимости полный набор: `python -m unittest discover -s tests -v`.
7. После изменения документации не утверждай результаты тестов, если тесты не запускались. Не храни в этом файле временные результаты прогонов: они быстро устаревают.

## 12. AI Companion privacy and operation

- AI configuration is optional in `config/ai.yaml`; copy the safe template from `config/ai.yaml.example`. Gemini, humor, local personalization, and reactions have independent global switches, all off by default. Per-user switches are also off by default and are managed privately through `/ai`.
- `GEMINI_API_KEY` is read only from the process environment (or the existing dotenv loading); never put it in YAML. Gemini remains unavailable unless the global switch, explicit project/model quota verification switch, and key are all present. Before enabling, verify current RPM/TPM/RPD in Google AI Studio for the exact model and project.
- The AI Companion default model is `gemini-3.5-flash-lite`; `config/ai.yaml` may override it. The model choice does not enable AI or confirm project quotas.
- SQLite stores only `AIUserSettings`, allowlisted daily activity counters, quota bucket counters, and short-lived recent-joke fingerprints. Activity categories and fingerprints do not contain message text. No private message content, prompt history, profile, roster, or group activity is included in AI-specific records or sent to Gemini. Existing `users.last_action` and `users.last_active_at` remain the bot's generic activity record and are not duplicated as AI content.
- Activity-based text selection is local only. It can select a cached greeting template from coarse section counts; those counts are never included in an external request. The in-memory greeting cache expires after the configured interval and is cleared by preference reset.
- `/ai` lets the user explicitly switch Gemini, humor, personalization, and reactions independently and select a humor style. Reset returns those user settings to off/default and deletes only that user's AI activity aggregates, recent-joke fingerprint, and greeting cache. It does not remove quota counters or any educational data.
- Gemini input is capped at 500 characters, structured output is locally validated, user text is untrusted, and no model tools are registered. User and project minute/day/estimated TPM buckets are reserved together with SQLite `BEGIN IMMEDIATE` and conditional upserts; a failed provider attempt still consumes its reservation, including each permitted retry. TPM reservation conservatively includes UTF-8 input bytes, a configurable system-prompt allowance, and maximum output tokens. Reservation refuses non-SQLite dialects rather than silently using a non-atomic fallback.
- User daily quotas use the bot timezone. The project daily bucket uses `America/Los_Angeles` to align with Google's documented RPD reset. Project request and conservative estimated-token limits in the example are placeholders, not a claim about a specific Google project; set them no higher than its verified current model limits.
- Activity aggregate and quota-counter retention values come from `activity_retention_days` and `quota_retention_days` in `config/ai.yaml`; defaults remain 30 and 31 days respectively.
- Local Telegram reactions use exact, bounded thank-you/congratulations allowlists in private chat only, require both global and user opt-in, and permit at most one reaction per user every five minutes. They do not invoke Gemini. The cooldown is process-local; run one bot process if the configured cooldown must apply globally across all workers.
- Activity counters are retained for 30 days, quota buckets for 31 days, and recent-joke fingerprints for one day; the existing daily cleanup removes each independently. No manual schema rewrite or Alembic migration is used: new tables are additive SQLAlchemy metadata tables created by the existing `init_db()` path.
- Recent joke repetition protection stores only per-user SHA-256 fingerprints and timestamps (no response text), for a configurable 30-minute window; cleanup removes stale fingerprints after one day, and personal reset clears those fingerprints without changing quota counters.
- SHA-256 joke fingerprints are pseudonymous, not guaranteed anonymous: a guessable short response may be matched by hashing candidate phrases. They remain linked to the user's Telegram ID and are retained only for the documented short window.
- Model-proposed navigation never opens a section. Only a clear locally recognized request can invoke an existing section handler; ambiguous/model-only navigation asks the user to name the section explicitly. Bot capability questions use a fixed local description, and model-generated responses referring to the bot are rejected rather than trusted as a feature catalog. Humor is returned only when the user explicitly asks for a joke/meme and the input is not locally classified as serious.
