# Трекер здоровья — дизайн-документ

Версия документа: 1.0. Стек зафиксирован: Python + FastAPI + SQLAlchemy 2 + Alembic + psycopg3; React + Vite + TypeScript; PostgreSQL 17 (localhost:5432, БД `healthtracker_v2` и `healthtracker_v2_test`, роль `ht2`). Docker нет. LLM в рекомендациях нет.

Соглашения: время хранится в UTC (`timestamptz`); «локальный день» — календарная дата в часовом поясе пользователя (раздел 5); «минута» — UTC-минута (`date_trunc('minute', ts)`); все длительности в БД и API — минуты, целые.

---

## 1. Архитектура

Три процесса Python + Vite-фронтенд. Единственная точка общения между процессами — PostgreSQL.

```
 Браузер A ─┐                              ┌──────────────────────────────┐
 Браузер B ─┼─ HTTP/JSON, cookie ─────────►│ api  (uvicorn, :8000)        │
            │◄─ SSE /api/events ───────────│  REST, auth, приём файла,    │
            │                              │  чтение сводок, SSE-fan-out  │
 Vite dev   │  (:5173, proxy /api → :8000) └───────┬──────────────▲───────┘
 server     │                                      │ SQL          │ LISTEN ht_events
            │                                      ▼              │
            │                              ┌──────────────────────┴───────┐
            │                              │ PostgreSQL 17 (:5432)        │
            │                              │ данные, jobs (очередь),      │
            │                              │ outbox, events, mail_inbox   │
            │                              └───────▲──────────────┬───────┘
            │                       SKIP LOCKED    │ SQL, NOTIFY  │
            │                              ┌───────┴──────────────▼───────┐
            │                              │ worker (python -m app.worker)│
            │                              │ разбор файла, пересчёт,      │
            │                              │ правила, дайджест, отправка  │
            │                              └───────────────┬──────────────┘
            │                                   SMTP :2525 │
            │                              ┌───────────────▼──────────────┐
            │                              │ smtp-sink (aiosmtpd, :2525)  │
            │                              │ пишет письма в mail_inbox    │
            │                              └──────────────────────────────┘
```

| Процесс | Что делает | Чего НЕ делает |
|---|---|---|
| **api** | Аутентификация, REST, потоковая запись загруженного файла на диск (`data/uploads/<id>.ndjson`) и постановка job `process_upload`; чтение сводок/рекомендаций; SSE | Не парсит файл, не считает сводки, не отправляет почту |
| **worker** | Выбирает jobs (`FOR UPDATE SKIP LOCKED`), парсит NDJSON чанками, пишет данные, пересчитывает дневные сводки, оценивает правила, формирует дайджест, шлёт письма через SMTP, тик-планировщик раз в 15 с | Не обслуживает HTTP |
| **smtp-sink** | Принимает SMTP на `127.0.0.1:2525` (aiosmtpd), сохраняет письмо в `mail_inbox`; идемпотентен по `Message-ID` | Не пересылает наружу |

Связь:
- **Очередь**: таблица `jobs`; `NOTIFY ht_jobs` будит воркеров, плюс опрос раз в 2 с (страховка).
- **SSE**: воркер и api пишут строку в `events(id, user_id, type, payload)` и делают `NOTIFY ht_events, '<id>'`. api держит одно выделенное соединение с `LISTEN ht_events`, читает события по id и раздаёт подписчикам этого `user_id`. Почему через таблицу: NOTIFY ограничен 8000 байт и не переживает обрыв; таблица даёт `Last-Event-ID` и догонку после реконнекта (хранение 24 ч).
- **Почта**: воркер → SMTP `localhost:2525` → sink → `mail_inbox`. Заголовок `Message-ID: <outbox_id@healthtracker.local>`; sink отбрасывает дубль → повтор после таймаута не даёт второго письма.
- **Конкурентность**: любая запись, меняющая агрегаты пользователя (чанк, пересчёт, оценка правил, дайджест), берёт `pg_advisory_xact_lock(user_id)`. Два клиента одного пользователя и два воркера не гоняются.
- **Время**: везде `clock.now()`; в тестах подменяется переменной `HT_FAKE_NOW` / параметром.

Порты и конфиг (env, `.env.example`): `DATABASE_URL`, `API_PORT=8000`, `SMTP_SINK_PORT=2525`, `UPLOAD_DIR=data/uploads`, `CHUNK_LINES=2000`, `DIGEST_HOLD_SECONDS=60`, `WORKER_CONCURRENCY=2`.

---

## 2. Формат данных (NDJSON, версия 1)

Файл — UTF-8, одна JSON-запись на строку (`\n`), пустые строки игнорируются, строка ≤ 4 КБ, файл ≤ 500 МБ. Порядок строк произвольный (сервер не требует сортировки и не требует «device» первым). Одна запись = один объект.

Общие поля всех записей:

| Поле | Тип | Обязательно | Описание |
|---|---|---|---|
| `v` | int | да | версия формата, сейчас `1`. Иное значение → строка отвергается (`unsupported_version`) |
| `type` | string | да | `device` \| `sleep` \| `steps` \| `heart_rate` |
| `device_id` | string 1..64 `[A-Za-z0-9._:-]` | да | стабильный идентификатор источника, уникален в пределах пользователя; сервер сопоставляет `(user, device_id)` с внутренним `devices.id` |

Времена — RFC 3339 строкой в UTC с суффиксом `Z`, `YYYY-MM-DDTHH:MM:SSZ`. `offset_min` — целое, смещение местного времени устройства относительно UTC в минутах на момент `start`/`ts` (−720..840), например Москва `180`.

**device** (метаданные; опционально, но рекомендуется):
`{"v":1,"type":"device","device_id":"watch-01","kind":"watch","model":"Acme W2","priority":20}` — `kind` ∈ `watch|phone|other`; `model` string ≤ 64, необязательно; `priority` int 0..100, необязательно (по умолчанию: watch=20, phone=10, other=5). Повторная строка device обновляет `kind/model`; `priority` обновляется, только если пользователь не менял его вручную через API. Если device-строки нет, устройство создаётся неявно с `kind=other`.

**sleep** (интервал сна):
`{"v":1,"type":"sleep","device_id":"watch-01","start":"2026-03-01T22:40:00Z","end":"2026-03-02T05:25:00Z","stage":"asleep","offset_min":180}`
- `stage` ∈ `asleep|light|deep|rem|awake`, по умолчанию `asleep`. Учитываются все, кроме `awake`.
- `start` и `end` выровнены по минуте (секунды = 00), `start < end`, длина ≤ 16 ч.

**steps** (шаги за интервал):
`{"v":1,"type":"steps","device_id":"phone-01","start":"2026-03-01T09:00:00Z","end":"2026-03-01T09:15:00Z","count":1420,"offset_min":180}`
- `count` int ≥ 0; `start`/`end` выровнены по минуте, `start < end`, длина 1..360 мин. Интервал с `count=0` — это **данные** («устройство было и шагов не было»), не пропуск.

**heart_rate** (точка пульса):
`{"v":1,"type":"heart_rate","device_id":"watch-01","ts":"2026-03-01T09:03:20Z","bpm":72,"offset_min":180}`
- `bpm` int 25..250; `ts` с точностью до секунды.

Пример файла:
```
{"v":1,"type":"device","device_id":"watch-01","kind":"watch","model":"Acme W2"}
{"v":1,"type":"device","device_id":"phone-01","kind":"phone","model":"Pixel"}
{"v":1,"type":"sleep","device_id":"watch-01","start":"2026-03-01T22:40:00Z","end":"2026-03-02T05:25:00Z","stage":"asleep","offset_min":180}
{"v":1,"type":"steps","device_id":"watch-01","start":"2026-03-02T06:00:00Z","end":"2026-03-02T06:30:00Z","count":2100,"offset_min":180}
{"v":1,"type":"steps","device_id":"phone-01","start":"2026-03-02T06:05:00Z","end":"2026-03-02T06:25:00Z","count":1900,"offset_min":180}
{"v":1,"type":"heart_rate","device_id":"watch-01","ts":"2026-03-02T06:10:15Z","bpm":96,"offset_min":180}
```

Валидация построчная: плохая строка → запись в `upload_errors(line_no, code, message)` (хранятся первые 100, счётчик — полный), остальные строки обрабатываются. Коды: `bad_json`, `unsupported_version`, `unknown_type`, `missing_field`, `bad_value`, `bad_time`, `bad_interval`. Итоговый статус файла: `done` (0 ошибок), `done_with_errors`, `failed` (нечитаем файл целиком / нет прав на запись / исключение вне строки).

`offset_min` НЕ определяет день (день определяется таймлайном поясов, раздел 5); он нужен для обнаружения смены пояса (раздел 5.4) и хранится на уровне минуты/интервала.

---

## 3. Схема БД

Миграции — Alembic. Ниже эскиз (типы упрощены; `user_id` во всех пользовательских таблицах, везде FK `ON DELETE CASCADE`).

```sql
CREATE TABLE users (
  id bigserial PRIMARY KEY,
  login citext UNIQUE NOT NULL,
  password_hash text NOT NULL,                 -- argon2id
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE auth_sessions (
  id bigserial PRIMARY KEY,
  user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  token_hash bytea UNIQUE NOT NULL,            -- sha256 токена из cookie
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL              -- скользящие 14 дней
);
CREATE TABLE user_settings (
  user_id bigint PRIMARY KEY REFERENCES users ON DELETE CASCADE,
  email text, email_enabled boolean NOT NULL DEFAULT true,
  quiet_enabled boolean NOT NULL DEFAULT false,
  quiet_start time NOT NULL DEFAULT '22:00', quiet_end time NOT NULL DEFAULT '08:00',
  follow_device_tz boolean NOT NULL DEFAULT true,
  tz_version int NOT NULL DEFAULT 0            -- ++ при любой правке таймлайна
);
-- таймлайн поясов: зона, действующая с момента valid_from_utc
CREATE TABLE tz_periods (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  valid_from timestamptz NOT NULL,             -- первая запись: '-infinity'
  tz_name text,                                -- IANA, например 'Europe/Moscow'
  fixed_offset_min int,                        -- для периодов, выведенных из устройства
  source text NOT NULL,                        -- 'manual' | 'device' | 'initial'
  CHECK ((tz_name IS NULL) <> (fixed_offset_min IS NULL)),
  UNIQUE (user_id, valid_from)
);
CREATE TABLE devices (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  external_id text NOT NULL, kind text NOT NULL DEFAULT 'other', model text, name text,
  priority int NOT NULL DEFAULT 5, priority_manual boolean NOT NULL DEFAULT false,
  UNIQUE (user_id, external_id)
);

CREATE TABLE uploads (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL REFERENCES users ON DELETE CASCADE,
  filename text, size_bytes bigint, path text NOT NULL,
  status text NOT NULL DEFAULT 'queued',       -- queued|processing|done|done_with_errors|failed|cancelled
  byte_offset bigint NOT NULL DEFAULT 0,       -- чекпоинт: возобновление после падения воркера
  lines_total bigint, lines_done bigint NOT NULL DEFAULT 0,
  rows_inserted bigint NOT NULL DEFAULT 0, rows_updated bigint NOT NULL DEFAULT 0,
  rows_duplicate bigint NOT NULL DEFAULT 0, rows_rejected bigint NOT NULL DEFAULT 0,
  min_ts timestamptz, max_ts timestamptz, error text,
  created_at timestamptz NOT NULL DEFAULT now(), started_at timestamptz, finished_at timestamptz
);
CREATE TABLE upload_errors (
  upload_id bigint REFERENCES uploads ON DELETE CASCADE, line_no bigint, code text, message text,
  PRIMARY KEY (upload_id, line_no)
);

-- СЫРЬЁ (после нормализации). Ключи уникальности = идемпотентность загрузки.
CREATE TABLE sleep_intervals (
  user_id bigint NOT NULL, device_id bigint NOT NULL REFERENCES devices ON DELETE CASCADE,
  start_ts timestamptz NOT NULL, end_ts timestamptz NOT NULL,
  stage text NOT NULL, offset_min int, upload_id bigint,
  PRIMARY KEY (device_id, start_ts, end_ts)
);
CREATE INDEX ON sleep_intervals (user_id, end_ts);

CREATE TABLE steps_minute (                    -- интервал разложен по минутам
  user_id bigint NOT NULL, device_id bigint NOT NULL REFERENCES devices ON DELETE CASCADE,
  minute_ts timestamptz NOT NULL, steps int NOT NULL CHECK (steps >= 0),
  offset_min int, upload_id bigint,
  PRIMARY KEY (device_id, minute_ts)
);
CREATE INDEX ON steps_minute (user_id, minute_ts);

CREATE TABLE hr_points (
  user_id bigint NOT NULL, device_id bigint NOT NULL REFERENCES devices ON DELETE CASCADE,
  ts timestamptz NOT NULL, bpm smallint NOT NULL, offset_min int, upload_id bigint,
  PRIMARY KEY (device_id, ts)
);
CREATE INDEX ON hr_points (user_id, ts);

-- ПРОИЗВОДНЫЕ (материализованные дневные сводки; недельные считаются из них на чтении)
CREATE TABLE daily_summary (
  user_id bigint NOT NULL, local_date date NOT NULL,
  day_start timestamptz NOT NULL, day_end timestamptz NOT NULL, day_length_min int NOT NULL, -- 1380/1440/1500
  wear_minutes int NOT NULL, coverage text NOT NULL,       -- none|partial|complete
  steps int, steps_status text NOT NULL,                   -- steps NULL при no_data
  active_minutes int,
  sleep_minutes int, sleep_start timestamptz, sleep_end timestamptz, sleep_status text NOT NULL,
  resting_hr int, peak_hr int, avg_hr int, hr_minutes int, hr_status text NOT NULL,
  tz_version int NOT NULL, computed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, local_date)
);

CREATE TABLE goals (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL,
  kind text NOT NULL CHECK (kind IN ('steps_per_day','sleep_minutes_per_day')),
  target int NOT NULL CHECK (target > 0), active boolean NOT NULL DEFAULT true,
  UNIQUE (user_id, kind)
);
CREATE TABLE goal_achievements (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL, goal_id bigint NOT NULL REFERENCES goals,
  local_date date NOT NULL, target_at_time int NOT NULL, value int NOT NULL,
  achieved_at timestamptz NOT NULL DEFAULT now(), revoked_at timestamptz,
  UNIQUE (goal_id, local_date)                              -- не дублируется
);

CREATE TABLE rule_settings (                               -- каталог правил в коде, здесь только включённость
  user_id bigint NOT NULL, rule_id text NOT NULL, enabled boolean NOT NULL DEFAULT true,
  changed_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (user_id, rule_id)
);
CREATE TABLE recommendations (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL, rule_id text NOT NULL, dedup_key text NOT NULL,
  status text NOT NULL CHECK (status IN ('open','resolved')),
  resolution_reason text,                                  -- auto_cleared|rule_disabled|expired
  title text NOT NULL, body text NOT NULL, evidence jsonb NOT NULL,
  opened_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(), resolved_at timestamptz,
  feedback text CHECK (feedback IN ('helpful','not_helpful')), feedback_at timestamptz
);
CREATE UNIQUE INDEX uq_reco_open ON recommendations (user_id, rule_id, dedup_key) WHERE status = 'open';
CREATE INDEX ON recommendations (user_id, status, opened_at DESC);

CREATE TABLE notifications (                               -- UI-уведомления
  id bigserial PRIMARY KEY, user_id bigint NOT NULL, kind text NOT NULL,  -- goal_achieved|recommendation
  payload jsonb NOT NULL, dedup_key text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
  read_at timestamptz, UNIQUE (user_id, dedup_key)
);
CREATE TABLE digest_pending (                              -- рекомендации, ожидающие письма
  user_id bigint NOT NULL, recommendation_id bigint NOT NULL REFERENCES recommendations ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (user_id, recommendation_id)
);
CREATE TABLE outbox (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL, to_addr text NOT NULL,
  subject text NOT NULL, body_text text NOT NULL, body_html text,
  recommendation_ids bigint[] NOT NULL,
  status text NOT NULL DEFAULT 'pending',   -- pending|sending|sent|failed|dead|cancelled
  send_after timestamptz NOT NULL, defer_reason text,       -- 'quiet_hours'|'hold'|NULL
  attempts int NOT NULL DEFAULT 0, last_error text, next_attempt_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(), sent_at timestamptz
);
CREATE INDEX ON outbox (status, send_after);
CREATE UNIQUE INDEX uq_outbox_open ON outbox (user_id) WHERE status IN ('pending','failed');  -- не более одного неотправленного письма на пользователя
CREATE TABLE mail_inbox (                                  -- smtp-sink
  id bigserial PRIMARY KEY, message_id text UNIQUE NOT NULL, rcpt text NOT NULL,
  raw text NOT NULL, subject text, received_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
  id bigserial PRIMARY KEY, kind text NOT NULL,   -- process_upload|recompute_days|evaluate_rules|flush_digest|send_outbox|tick
  user_id bigint, payload jsonb NOT NULL DEFAULT '{}',
  status text NOT NULL DEFAULT 'queued',          -- queued|running|done|failed
  run_at timestamptz NOT NULL DEFAULT now(), attempts int NOT NULL DEFAULT 0, max_attempts int NOT NULL DEFAULT 5,
  locked_by text, locked_at timestamptz, heartbeat_at timestamptz, last_error text,
  dedupe_key text, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_jobs_dedupe ON jobs (dedupe_key) WHERE status = 'queued' AND dedupe_key IS NOT NULL; -- схлопывание
CREATE INDEX ON jobs (status, run_at);
CREATE TABLE events (
  id bigserial PRIMARY KEY, user_id bigint NOT NULL, type text NOT NULL, payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON events (user_id, id);
```

Идемпотентность загрузки держится на первичных ключах сырья: `(device_id, minute_ts)`, `(device_id, ts)`, `(device_id, start_ts, end_ts)`; запись — `INSERT ... ON CONFLICT DO UPDATE ... WHERE значение отличается RETURNING (xmax = 0) AS inserted`, что даёт счётчики `inserted/updated/duplicate` без повторного чтения. Выборка job: `SELECT ... FROM jobs WHERE status='queued' AND run_at<=now() ORDER BY run_at FOR UPDATE SKIP LOCKED LIMIT 1`; зависшие (`running`, `heartbeat_at` старше 120 с) возвращаются в `queued`.

---

## 4. Правило объединения устройств и дедупликация

Принципы: (1) всё сводится к сетке UTC-минут; (2) на каждую минуту и метрику результат берётся **из одного источника**, поэтому сумма и «двойной счёт» невозможны; (3) приоритет устройства задаёт пользователь (по умолчанию watch 20 > phone 10 > other 5).

### 4.1. Шаги: «приоритет устройства на каждую минуту»
1. Интервал `[start,end)` с `count=c` из `n` минут раскладывается в минуты детерминированно: минута `i` (0..n−1) получает `floor((i+1)·c/n) − floor(i·c/n)`; сумма по интервалу в точности `c`.
2. Каждая минута хранится в `steps_minute` на своём устройстве (PK `(device_id, minute_ts)`).
3. Итоговое значение минуты `M` для пользователя:
   - берутся устройства, имеющие запись для `M` (в т.ч. `steps=0`);
   - выбирается устройство с максимальным `priority` (при равенстве — меньший `device.id`) → `steps(M) = его значение`;
   - **исключение «часы сняты»**: если выбранное устройство дало `0`, а устройство с меньшим приоритетом дало `>0`, берётся наибольшее значение среди остальных. (Часы лежат на столе, телефон в кармане.)
4. Минуты, на которых нет записей ни от одного устройства, — «нет данных» (не ноль).
5. Сумма дня = сумма итоговых минут, чьё локальное время (раздел 5) принадлежит дню.

Почему так: сумма по устройствам удваивает прогулку; `max` по дню/интервалу теряет шаги, когда часы сняты на полдня; приоритет на уровне **минуты** корректно работает при интервалах разной длины (часы 30 мин, телефон 20 мин), а сумма «одной прогулки» всегда принадлежит одному источнику. Пример: часы 06:00–06:30 = 2100, телефон 06:05–06:25 = 1900 → итог 2100 (часы побеждают на всех 30 минутах), а не 4000. Если часов нет с 06:15, на 06:15–06:25 побеждает телефон.

### 4.2. Сон: объединение интервалов
1. Берутся интервалы всех устройств со `stage ≠ awake` (поэтапные light/deep/rem — как «спал»).
2. Объединение (union) по времени: пересекающиеся и соприкасающиеся интервалы склеиваются. Минуты сна = длина объединения. Если часы говорят «спал», а телефон нет — это union, т.е. сон засчитан (телефон лежит и ничего не знает о сне).
3. Интервалы `awake` на объединение не влияют (допущение: перерывы внутри ночи не вычитаются — они уже отсутствуют как дырки между интервалами, если устройство их не покрывает).
4. Сессия сна = цепочка объединённых интервалов с разрывами ≤ 30 мин. Длительность сессии = минуты сна внутри (дырки не считаются). День сессии — по пробуждению (раздел 5.2).

### 4.3. Пульс: объединение точек
1. Точка округляется до минуты; для минуты берутся точки **только устройства с наивысшим приоритетом, у которого в этой минуте есть точки**; значение минуты = среднее bpm его точек (округление half-up). Остальные устройства в эту минуту игнорируются (нет двойного веса).
2. Минуты без точек — «нет данных».
Результат — ряд `hr_minute(M)`, из него считаются resting/peak (раздел 7).

### 4.4. Дедупликация при повторной/частичной загрузке
- Точное повторение (то же устройство, то же время) → `ON CONFLICT` без изменений → `rows_duplicate++`, итоги не меняются.
- Частичное пересечение того же устройства: шаги — **побеждает последняя загруженная запись на каждую минуту** (upsert по `(device_id, minute_ts)`; `rows_updated++` если значение изменилось); пульс — upsert по `(device_id, ts)`; сон — upsert по `(device_id, start_ts, end_ts)`, а разные границы схлопываются через union (п. 4.2), удвоения нет.
- Разные устройства — по правилам 4.1–4.3, суммы не накладываются.
- Результат не зависит от порядка и числа повторов при одинаковых значениях; при разных значениях для одной минуты порядок загрузки определяет победителя (допущение, раздел «Спорные места»).

---

## 5. Дни и часовые пояса

### 5.1. Таймлайн поясов
Локальный день определяется **не** по `offset_min` записи, а по таймлайну пользователя `tz_periods`: упорядоченный список `(valid_from, зона)`. Смещение в момент `t` = смещение зоны периода с наибольшим `valid_from ≤ t` (IANA-зона через `zoneinfo` учитывает DST; `fixed_offset_min` — фиксированное). Локальная дата момента `t`: `local_date(t) = date(t + offset(t))`. Поскольку каждая UTC-минута получает ровно одну локальную дату, **данные не теряются и не удваиваются** при любых сменах.

### 5.2. Сон вечер→утро
Сессия сна относится к локальной дате **момента пробуждения** (`local_date(session.end − 1 мин)`). Ночь 22:40→06:25 попадает в день пробуждения. Дневной сон — по тому же правилу, суммируется с ночным в тот же день. Если сессия закончилась до полуночи (дневной сон 14:00–15:30), день — её собственный. «Ночь на дату D» = сон дня D.

### 5.3. Длина дня и день переезда
`day_start(D)` — первый момент с `local_date = D`, `day_end(D)` — первый момент с `local_date > D`. `day_length_min = day_end − day_start`. Если смена пояса приходится на день D (запись таймлайна с `valid_from` внутри D), длина дня отличается от 1440: переезд на +1 ч на восток → 1380 (23 ч), на −1 ч на запад → 1500 (25 ч); при DST то же самое. API отдаёт `day_length_min`; цели и проценты не нормируются по длине (шаги/сон — абсолютные величины). Проверка: сумма шагов по всем дням диапазона == сумме итоговых минут за тот же UTC-диапазон (инвариант «ничего не потеряно/не удвоено»).

### 5.4. Откуда берутся смены пояса
1. **Настройка**: `PUT /api/settings/timezone {timezone, effective_from?, scope}`. `scope="from_now"` (по умолчанию): добавляется период с `valid_from = effective_from` (по умолчанию «сейчас», пользователь может указать момент прилёта); `scope="all_history"`: таймлайн заменяется одним периодом `-infinity` (исправление ошибочной настройки).
2. **По устройству** (`follow_device_tz = true`, по умолчанию): при обработке чанка, если у устройства с наивысшим приоритетом смещение `offset_min` меняется относительно предыдущей записи (по времени) на ≥ 30 мин и **новое смещение держится ≥ 3 подряд записей** — в таймлайн добавляется период `source='device'`, `fixed_offset_min` = новое смещение, `valid_from` = время первой записи с новым смещением. Если таймлайн уже даёт то же смещение в этот момент — ничего не добавляется (идемпотентно при повторной загрузке). Показывать пользователю баннер «Обнаружена смена пояса, UTC+X» в UI (событие `settings.tz_changed`).
3. Любая правка таймлайна: `tz_version++`, job `recompute_days` на затронутый диапазон `[min(valid_from)−1 день, +∞)` (для `all_history` — весь период данных), затем `evaluate_rules`. Дневные сводки с `tz_version` меньше текущего помечаются устаревшими и пересчитываются; чтение не отдаёт устаревшую строку без метки `stale: true`.

Почему таймлайн, а не смещение записи: в одном дне разные устройства могут иметь разные смещения (телефон переключился, часы нет); таймлайн даёт единое правило и позволяет «пересчёт при смене пояса в настройках».

### 5.5. Прочее время
«Сегодня» и `as_of` (раздел 9) — по текущему поясу таймлайна. Недели — ISO, Пн–Вс по локальным датам.

---

## 6. Пропуски: «нет данных»

Расчёт по локальному дню.

- `wear_minutes` = число различных UTC-минут дня, в которых есть любая запись любого устройства (шаги, пульс; минуты внутри сна тоже считаются).
- **Покрытие дня** `coverage`: `none` — `wear_minutes < 120`; `partial` — 120..719; `complete` — ≥ 720 (12 ч).
- **steps_status**: `no_data` — нет ни одной минуты шагов или `coverage = none` → `steps = null`; `partial` — есть данные, но покрытие `partial`; `complete` — покрытие `complete`. Для `partial` значение отдаётся (сумма имеющегося) вместе с признаком, что это нижняя оценка. `steps = 0` при `complete` — настоящий ноль.
- **sleep_status**: `no_data` — нет сессий сна дня или суммарно < 60 мин (значение `sleep_minutes = null`); иначе `ok`.
- **hr_status**: `no_data` — `hr_minutes < 30`; иначе `ok`; `resting_hr = null`, если кандидатов покоя < 30 (раздел 7).
- Текущий день (`local_date == today`, день не закончился): `in_progress` вместо `partial/complete` в `steps_status`; значение — «на сейчас».

Передача: API возвращает `null` в числовом поле плюс `status` у каждой метрики и `coverage`; UI показывает серое «нет данных» (не «0»), у `partial` — значок «неполные данные». **Правила**: читают только дни со статусом `complete`/`ok`; день `no_data`/`partial`/`in_progress` не срабатывает и не прерывает автозакрытие (результат `UNKNOWN`, раздел 9). Нули на пропусках невозможны: нет данных → нет строки/`null`, а не 0.

---

## 7. Сводки

### 7.1. Дневная (`daily_summary`)
- `steps` — сумма итоговых минут дня (4.1).
- `active_minutes` — число итоговых минут дня с `steps ≥ 100` (темп быстрой ходьбы).
- `sleep_minutes` — сумма минут сна сессий, чей день — этот (5.2); `sleep_start/sleep_end` — начало первой и конец последней ночной сессии (сессии с длиной ≥ 120 мин; иначе — самой длинной).
- `avg_hr` — среднее по минутам ряда `hr_minute`.
- **Пиковый пульс** `peak_hr` — максимум ряда `hr_minute` (поминутные средние гасят одиночные выбросы; отдельные значения вне 25..250 отсекаются валидацией).
- **Пульс покоя** `resting_hr`: кандидаты — минуты `hr_minute`, которые (а) внутри сессии сна этого дня **или** (б) вне сна, но с покрытием шагов (есть запись шагов) и суммой шагов за эту и 9 предыдущих минут = 0 (10 минут неподвижности). Если кандидатов < 30 → `null`. Иначе: взять значения кандидатов, отсортировать, взять нижние 10 % (`ceil(0.1·N)` минимальных) и вернуть **медиану** (округление half-up). Почему: min слишком чувствителен к артефактам, а средний пульс за день включает активность.

### 7.2. Недельная (ISO-неделя, локальные даты Пн–Вс; считается из `daily_summary` на чтении)
- `valid_days_steps` — дней с `steps_status = complete`; `steps_avg` = среднее `steps` по ним; `steps_total` — сумма по дням `complete` и `partial` (инфо).
- `sleep_avg` — среднее `sleep_minutes` по ночам со `sleep_status = ok`; `sleep_nights` — их число.
- `resting_hr_avg` — среднее по дням с непустым `resting_hr`; `peak_hr_max` — максимум `peak_hr`.
- `goal_days` — число дней, где цель шагов достигнута (по `complete`), для сна аналогично.
- Статус недели: `complete` — `valid_days_steps ≥ 5`; `partial` — 1..4; `no_data` — 0 (и все метрики `null`). Текущая неделя считается по прошедшим дням, `status = in_progress`.

### 7.3. Сравнение с предыдущим периодом
Для дня — с предыдущим локальным днём; для недели — с предыдущей ISO-неделей. Сравниваются **средние на валидный день**, а не суммы (иначе неполная неделя «проигрывает»).
Ответ: `{current, previous, delta_abs, delta_pct, comparable, reason}`. `comparable = true`, только если обе стороны имеют значение и (для недели) `valid_days ≥ 3` с каждой стороны; иначе `delta_* = null`, `comparable = false`, `reason ∈ previous_no_data | current_no_data | insufficient_coverage`. `delta_pct` = `delta_abs / previous · 100`, при `previous = 0` — `null`.

Обновление «по мере обработки»: после каждого чанка файла пересчитываются затронутые локальные даты (раздел 11) и отправляется SSE `summary.updated`.

---

## 8. Цели и достижение

Цели: `steps_per_day` (шаги) и `sleep_minutes_per_day` (минуты сна; UI вводит часы, храним минуты). Одна активная цель каждого вида на пользователя; изменение цели действует с момента изменения (прошлые достижения хранят `target_at_time`).

Прогресс дня: `value / target` (процент, может быть > 100), `status` берётся из статуса метрики дня; при `no_data` — `value = null`, прогресс «нет данных».

Достижение:
- Проверяется после каждого пересчёта дневной сводки (каждый чанк, раздел 11) для всех пересчитанных дат.
- Условие: шаги — `steps ≥ target` (достаточно `in_progress/partial`: шаги монотонно растут, нижняя оценка уже выполнена); сон — `sleep_minutes ≥ target` и `sleep_status = ok`.
- Создание: `INSERT INTO goal_achievements ... ON CONFLICT (goal_id, local_date) DO NOTHING RETURNING id`. Только если строка реально вставлена — создаются `notifications` (`dedup_key = 'goal:'||goal_id||':'||date`) и событие SSE `goal.achieved`. Повторная загрузка тех же данных ничего не создаёт. Это работает для двух клиентов: событие приходит обоим, уведомление одно.
- Пересчёт опустил значение ниже цели (исправление данных): `revoked_at = now()`, уведомление не удаляется; при повторном достижении `revoked_at = NULL` без нового уведомления.
- Уведомление в UI создаётся для дат `≥ as_of − 1` (раздел 9); более старые достижения записываются без уведомления (истории достижений полезно, но спам про давнее не нужен). Достижение приходит «сразу, как пришли данные»: на ближайшем чанке, а не по завершении файла.

---

## 9. Правила рекомендаций

Каталог правил — в коде (`backend/app/rules/`), у пользователя хранится только `enabled` (по умолчанию `true`). Пороги фиксированы (параметры показываются в UI, не редактируются — допущение).

**`as_of`** — последняя локальная дата, по которой у пользователя есть любые данные (не часы «сейчас»): так работают исторические выгрузки генератора. Окно оценки — `[as_of−29, as_of]`.

**Результат оценки правила по каждой ситуации** (ситуация = ключ дедупликации):
- `ACTIVE` — условие выполняется по полным данным;
- `CLEARED` — данные однозначно показывают, что ситуация устранена или условие больше не выполняется;
- `UNKNOWN` — данных для суждения нет (пропуски/частичные дни). Открытая рекомендация остаётся открытой, новая не создаётся.

Правила (дни `no_data/partial/in_progress` как пропуски — всегда `UNKNOWN` и разрывают серию «подряд»):

| id | Условие | Окно | Поведение на пропусках | Ключ дедупликации | Автозакрытие (`CLEARED`) |
|---|---|---|---|---|---|
| `sleep_short_streak` | сон < 360 мин три ночи подряд (3 последовательные локальные даты, у каждой `sleep_status=ok`) | 30 дней до `as_of`; серия считается активной, если её последний день ≥ `as_of−1` | пропуск ночи разрывает серию; на дне без данных не срабатывает | `S` = дата первой ночи серии, т. е. `streak:<S>` | любая из ночей `S..S+2` ≥ 360 мин (после исправления данных) или после `S+2` есть ночь с данными ≥ 360 мин; ночи без данных не закрывают |
| `steps_low_streak` | шаги < 50 % цели `steps_per_day` три дня подряд, каждый `complete` (нужна активная цель) | то же | `partial/no_data/in_progress` разрывают серию | `streak:<S>` | любой из дней серии ≥ 50 % цели либо следующий `complete` день ≥ 50 % |
| `resting_hr_elevated` | `resting_hr ≥ baseline + 7` два дня подряд; `baseline` = медиана `resting_hr` за 14 дней до начала серии (нужно ≥ 7 значений, иначе правило неактивно) | то же | дни без `resting_hr` разрывают серию | `streak:<S>` | следующий день с `resting_hr` < `baseline + 7` (baseline хранится в `evidence`) |
| `steps_week_drop` | закрытая ISO-неделя `W` (последний день ≤ `as_of`, `as_of` ≥ ближайший Пн после `W`) и `W−1`, обе с `valid_days_steps ≥ 4`; `steps_avg(W) ≤ 0.7 · steps_avg(W−1)` | 5 последних недель | недели с < 4 валидных дней → `UNKNOWN` | `week:<W>` (напр. `week:2026-W10`) | скользящие последние 7 дней (≥ 4 `complete`) ≥ 0.8 · `steps_avg(W−1)`, либо пересчёт сводит падение < 30 % |
| `sleep_debt_week` | закрытая ISO-неделя `W`, ≥ 5 ночей с данными, `sleep_avg ≤ цель_сна − 60 мин` (нужна активная цель сна) | 5 недель | < 5 ночей → `UNKNOWN` | `week:<W>` | скользящие 7 дней (≥ 5 ночей) со `sleep_avg ≥ цель − 30 мин`, либо пересчёт недели не выполняет условие |
| `peak_hr_high` | в дне ≥ 10 минут ряда `hr_minute` с bpm ≥ 170 | дни `as_of−2..as_of` | без пульса дня → `UNKNOWN` | `day:<дата>` | пересчёт (исправление данных) даёт < 10 таких минут; иначе через 3 дня после даты — `resolved` с `expired` |

Жизненный цикл рекомендации:
1. Нет открытой рекомендации с ключом и оценка `ACTIVE` → **создаётся** `open` (`INSERT`, уникальность частичного индекса `uq_reco_open`), создаётся `notifications` (kind `recommendation`), событие SSE `recommendation.changed`, строка в `digest_pending`.
2. Есть открытая, оценка `ACTIVE` → обновляются `evidence/body/updated_at` (напр. серия удлинилась до 4 ночей), SSE-событие, **новая рекомендация и письмо не создаются** (правило «не повторять, пока ситуация не изменилась»).
3. Есть открытая, оценка `CLEARED` → `status=resolved`, `resolved_at=now()`, `resolution_reason='auto_cleared'`; из `digest_pending` удаляется (если письмо ещё не ушло) и отмена вписывается в неотправленное письмо (раздел 10); SSE-событие.
4. `UNKNOWN` → без изменений.
5. **Повторное открытие**: если после `resolved` та же ситуация возникает снова, это новая ситуация → новая строка (`open`), новое письмо. Ключ завязан на дату начала серии/неделю/день, поэтому та же серия, закрытая и вновь открытая исправлением данных, получает ту же `dedup_key`, но это допустимо: уникальность только среди `open`. История (`resolved`) не удаляется и не перезаписывается.
6. **Отключение правила** (`PATCH /api/rules/{id} {enabled:false}`): правило не оценивается; открытые рекомендации этого правила закрываются (`resolution_reason='rule_disabled'`), остаются в истории; ожидающие письма по ним отзываются. Включение: немедленный `evaluate_rules` по пользователю.
7. Обратная связь: `feedback ∈ helpful|not_helpful|null` на любой строке (открытой или закрытой), `feedback_at`. Не влияет на правила.

Запуск оценки: `evaluate_rules` (job, `dedupe_key='eval:<user>'`) после завершения файла, после смены пояса/цели/правил и тиком раз в 15 с для пользователей с изменившимся `as_of`/суточным переходом. Всегда под `pg_advisory_xact_lock(user_id)`. Оценка идемпотентна: повтор на тех же данных не меняет состояние.

---

## 10. Почта

**Очередь**: при открытии рекомендации — строка в `digest_pending`. Job `flush_digest` (`dedupe_key='digest:<user>'`, `run_at = now + DIGEST_HOLD_SECONDS`, по умолчанию 60 с; несколько вставок схлопываются) выполняется один раз и собирает **все** ещё `open` рекомендации из `digest_pending` в **одно письмо на пользователя за прогон**: тема «N новых рекомендаций», в теле список (заголовок, пояснение, ссылка в приложение). Если у пользователя уже есть неотправленное письмо (`pending/failed`, частичный уникальный индекс), новые рекомендации дописываются в него (тело перегенерируется), а не создаётся второе письмо: так в тихие часы накопленное уходит одним письмом. Письма не создаются, если `email` пуст или `email_enabled=false` (`digest_pending` очищается, `cancelled`, причина `no_email`).

**Тихие часы**: `quiet_start`, `quiet_end` — локальное время в текущем поясе таймлайна. При `start < end` — тихо, если `start ≤ t < end`; при `start > end` (через полночь, напр. 22:00–08:00) — тихо, если `t ≥ start` **или** `t < end`; `start == end` или `quiet_enabled=false` — тихих часов нет. `send_after` = ближайший момент `end` в локальной зоне ≥ `now` (вычисляется через `zoneinfo`: для 22:30 с 22:00–08:00 это 08:00 следующих суток; для 03:00 — 08:00 того же дня; несуществующее локальное время при DST сдвигается вперёд). Проверка делается дважды: при создании письма (`send_after`, `defer_reason='quiet_hours'`) и в момент отправки (если настройки/пояс изменились — переносится заново). Исключений нет: ни одно письмо не отправляется в тихие часы.

**Статусы outbox**: `pending` (в т.ч. отложенное: `send_after > now`) → `sending` (воркер взял, `FOR UPDATE SKIP LOCKED`) → `sent` | `failed` (ошибка SMTP, `attempts++`, `next_attempt_at = now + [1м,5м,30м,2ч][attempts−1]`) → после 5 неудач `dead` (видно в UI, `last_error`). `cancelled` — все рекомендации письма закрылись до отправки (отозванное письмо не уходит) или нет адреса. Перед отправкой воркер проверяет: в письме остаются только `open` рекомендации; если их нет → `cancelled`; если часть закрылась — тело перегенерируется без них. `sending` с протухшим `locked_at` (> 120 с) возвращается в `pending`. Отправка идемпотентна за счёт `Message-ID` (раздел 1). События SSE `mail.status` на смену статуса.

Одна и та же рекомендация приходит в письме максимум один раз (после включения в письмо запись `digest_pending` удаляется, повторных писем на «ACTIVE» нет: п. 9.2).

---

## 11. Обработка файла

1. `POST /api/uploads`: api потоково пишет тело на диск (не буферизует в памяти), создаёт `uploads(status=queued)` и job `process_upload`, отвечает `202 {upload_id}` сразу. Большой файл не блокирует UI и не блокирует event loop api (запись chunked).
2. Воркер берёт job, `status=processing`. Один проход по размеру файла считает `lines_total` (для прогресса; дёшево, читается потоком).
3. Цикл по чанкам по `CHUNK_LINES=2000` строк: чтение с `byte_offset`; одна транзакция на чанк:
   - lock `pg_advisory_xact_lock(user_id)`;
   - парсинг/валидация, разрешение устройств, upsert сырья (раздел 3/4), обнаружение смены пояса (5.4);
   - определение затронутых локальных дат (по `min..max` ts чанка, +1 день по краям для сна), пересчёт `daily_summary` на эти даты (минутный ряд читается из БД, объединение — раздел 4, на Python);
   - проверка достижения целей (раздел 8);
   - обновление `uploads.byte_offset/lines_done/rows_*/min_ts/max_ts`, `events`-строки и `NOTIFY`;
   - commit. Между чанками воркер отпускает lock, чтобы читающие запросы и второй клиент не ждали.
4. Возобновление: при падении воркера job возвращается в `queued` по `heartbeat_at`, обработка продолжается с `byte_offset` (чанк атомарен; повтор чанка идемпотентен).
5. Завершение: `status=done|done_with_errors`, job `evaluate_rules` для пользователя (правила работают на полной картине, а не на промежуточной), событие `upload.done`.
6. Отмена: `POST /api/uploads/{id}/cancel` → флаг проверяется между чанками → `cancelled`; уже записанные чанки остаются (идемпотентны).
7. Конкурентность: файлы разных пользователей — параллельно (`WORKER_CONCURRENCY`); файлы одного пользователя — последовательно (job берётся, только если у пользователя нет `running` process_upload).

**SSE** `GET /api/events` (заголовок `Last-Event-ID` для догонки; keep-alive комментарий каждые 15 с; формат `id: <events.id>\nevent: <type>\ndata: <json>`). События:
| type | data |
|---|---|
| `upload.progress` | `{upload_id, status, lines_done, lines_total, percent, rows_inserted, rows_updated, rows_duplicate, rows_rejected}` (не чаще 1 в 500 мс на файл) |
| `upload.done` / `upload.failed` | `{upload_id, status, counters, error?}` |
| `summary.updated` | `{dates:[YYYY-MM-DD,...], upload_id?}` — клиент перезапрашивает `/api/summary/...` для видимых дат |
| `goal.achieved` | `{notification_id, goal_kind, date, value, target}` |
| `recommendation.changed` | `{id, rule_id, status, change: opened|updated|resolved}` |
| `notification.created` | `{id, kind}` |
| `mail.status` | `{outbox_id, status, send_after?}` |
| `settings.tz_changed` | `{timezone_periods, reason}` |

Сводка обновляется по мере обработки: каждый коммит чанка порождает `summary.updated` с датами этого чанка; на дашборде открытые клиенты (оба) перечитывают сводку, цифры растут до завершения файла; рядом виден прогресс-бар из `upload.progress`.

---

## 12. REST API

Префикс `/api`, JSON, `Content-Type: application/json`, даты `YYYY-MM-DD`, времена RFC 3339 UTC. Аутентификация — cookie `ht_session` (HttpOnly, SameSite=Lax, Path=/); мутирующие запросы требуют заголовок `X-Requested-With: ht` (простая защита от CSRF). Ошибки: `{"error":{"code":"...","message":"..."}}`; 401 без сессии, 404 для чужих объектов, 422 валидация.

**Аутентификация**
| Метод и путь | Тело → ответ |
|---|---|
| `POST /auth/register` | `{login,password}` → `201 {id,login}` + cookie; логин ≥ 3, пароль ≥ 8 |
| `POST /auth/login` | `{login,password}` → `200 {id,login}` + cookie; иначе 401 |
| `POST /auth/logout` | → `204`, сессия удаляется |
| `GET /auth/me` | → `{id,login,timezone,as_of}` |

**Устройства**
| `GET /devices` | → `[{id,external_id,kind,model,name,priority,last_data_at}]` |
| `PATCH /devices/{id}` | `{name?,kind?,priority?}` → устройство; смена приоритета ставит `recompute_days` на всё время |

**Загрузки**
| `POST /uploads` | тело — сырой NDJSON (`Content-Type: application/x-ndjson`, `X-Filename`) или `multipart/form-data` поле `file` → `202 {upload_id,status:"queued"}` |
| `GET /uploads` | → список `[{id,filename,size_bytes,status,lines_done,lines_total,percent,counters,created_at,finished_at}]`, новые сверху |
| `GET /uploads/{id}` | → то же + `min_ts,max_ts,error` |
| `GET /uploads/{id}/errors?limit=100` | → `[{line_no,code,message}]` |
| `POST /uploads/{id}/cancel` | → `202` |

**Сводки**
| `GET /summary/daily?date=` | → `{date, day_start, day_end, day_length_min, coverage, wear_minutes, stale, steps:{value,status}, active_minutes, sleep:{minutes,status,start,end}, hr:{resting,peak,avg,status}, comparison:{steps:Cmp,sleep:Cmp,resting_hr:Cmp,peak_hr:Cmp}, goals:[Progress], processing:bool}` |
| `GET /summary/daily/range?from=&to=` | → `{days:[…без comparison…]}` (≤ 100 дней; для графиков) |
| `GET /summary/weekly?week=2026-W10` (или `date=`) | → `{week,from,to,status,valid_days_steps,steps_avg,steps_total,sleep_avg,sleep_nights,resting_hr_avg,peak_hr_max,goal_days,days:[…],comparison:{…Cmp}}` |
| тип `Cmp` | `{current,previous,delta_abs,delta_pct,comparable,reason}` |

**Цели**
| `GET /goals` | → `[{kind,target,active}]` |
| `PUT /goals/{kind}` | `{target}` (`steps_per_day` — шаги; `sleep_minutes_per_day` — минуты) → цель; запускает пересчёт достижений и правил |
| `GET /goals/progress?date=` | → `[{kind,target,value,percent,status,achieved}]` |
| `GET /goals/achievements?limit=` | → `[{goal_kind,local_date,value,target_at_time,achieved_at,revoked}]` |

**Уведомления UI**
| `GET /notifications?unread=1&limit=50` | → `[{id,kind,payload,created_at,read_at}]` |
| `POST /notifications/{id}/read` | → `204` |
| `POST /notifications/read-all` | → `204` |

**Правила и рекомендации**
| `GET /rules` | → `[{id,title,description,params,enabled,open_count}]` |
| `PATCH /rules/{id}` | `{enabled}` → правило (семантика отключения — раздел 9) |
| `GET /recommendations?status=open\|resolved\|all&rule_id=&limit=&cursor=` | → `{items:[{id,rule_id,title,body,evidence,status,resolution_reason,opened_at,resolved_at,feedback}],next_cursor}` |
| `POST /recommendations/{id}/feedback` | `{value:"helpful"\|"not_helpful"\|null}` → рекомендация |

**Настройки**
| `GET /settings` | → `{email,email_enabled,timezone,timezone_periods:[{valid_from,tz,source}],follow_device_tz,quiet_hours:{enabled,start:"22:00",end:"08:00"}}` |
| `PATCH /settings` | `{email?,email_enabled?,follow_device_tz?,quiet_hours?}` → настройки; невалидный email → 422 |
| `PUT /settings/timezone` | `{timezone:"Europe/Moscow",effective_from?,scope?:"from_now"\|"all_history"}` → настройки; `202`-пересчёт в фоне, событие `summary.updated` |

**Почта и служебное**
| `GET /outbox?limit=` | → `[{id,subject,status,send_after,defer_reason,attempts,sent_at,recommendation_ids}]` |
| `GET /dev/mailbox?limit=` | → письма из `mail_inbox`, адресованные email пользователя `[{id,subject,rcpt,received_at,body_text}]` (только `HT_ENV=dev/test`) |
| `GET /events` | SSE, раздел 11 |
| `GET /health` | → `{db,worker_heartbeat_age_s}` без авторизации |

---

## 13. Структура репозитория и запуск

```
health-tracker-v2/
├─ backend/
│  ├─ pyproject.toml            # зависимости: fastapi, uvicorn, sqlalchemy>=2, alembic, psycopg[binary,pool], aiosmtpd, argon2-cffi, pytest, httpx
│  ├─ alembic.ini, migrations/
│  └─ app/
│     ├─ main.py (api), worker.py, smtp_sink.py, config.py, clock.py, db.py
│     ├─ api/ (auth, devices, uploads, summary, goals, rules, recs, settings, events, dev)
│     ├─ ingest/ (parser.py, validate.py, writer.py)
│     ├─ domain/ (timeline.py, merge.py, summary.py, goals.py, rules/*.py, digest.py, quiet_hours.py)
│     ├─ jobs/ (queue.py, handlers.py, scheduler.py), events.py, mail.py
│     └─ tests/ (unit/, integration/)
├─ frontend/                    # React + Vite + TS; pages: Dashboard, Day/Week, Goals, Recommendations, Uploads, Settings; src/sse.ts
│  └─ e2e/                      # Playwright
├─ scripts/
│  ├─ gen_data.py               # генератор тестовых данных (период, пропуски, два устройства) — пишет владелец
│  ├─ create_db.sql             # роль ht2 и обе БД (если ещё нет)
│  ├─ seed_user.py              # пользователь demo/demo_password (только dev)
│  └─ dev.ps1                   # запускает все процессы
├─ docs/DESIGN.md
├─ data/uploads/                # в .gitignore
└─ .env.example
```

Команды (PowerShell, из корня):
```
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
pip install -e backend[dev]
psql -U postgres -f scripts/create_db.sql          # один раз
cd backend ; alembic upgrade head                  # и с DATABASE_URL тестовой БД для тестов
uvicorn app.main:app --port 8000                   # api
python -m app.worker                               # worker
python -m app.smtp_sink                            # smtp-sink
cd ..\frontend ; npm install ; npm run dev         # :5173, proxy /api -> :8000
python scripts/gen_data.py --from 2026-03-01 --to 2026-03-31 --gaps 0.1 > data/sample.ndjson
pytest backend/app/tests -q                        # unit + integration (БД healthtracker_v2_test)
npx playwright test                                # e2e (поднимает api+worker+sink+vite с тестовой БД)
```

---

## 14. Стратегия доказуемости

Тесты: `pytest` (unit — чистые функции; integration — api+worker в процессе против `healthtracker_v2_test`, каждый тест в чистой схеме; время через `HT_FAKE_NOW`), e2e — Playwright. Каждый тест имеет имя-ссылку на пункт поведения.

| Пункт поведения | Тесты |
|---|---|
| Прогресс обработки виден | `test_upload_progress_events_monotonic` (SSE `upload.progress`: `lines_done` не убывает, финальное `percent=100`); `GET /uploads/{id}` отдаёт прогресс в середине |
| Большой файл не блокирует UI | `test_big_file_api_responsive` (файл 200k строк; во время обработки `GET /summary/daily` и `GET /health` отвечают < 300 мс; `POST /uploads` вернул 202 < 1 с) |
| Сводка обновляется по мере обработки | `test_summary_updates_during_processing` (`summary.updated` приходит до `upload.done`, значение шагов растёт между чанками) |
| Повторная загрузка не дублирует | `test_reupload_idempotent` (дважды один файл → таблицы и сводки идентичны, `rows_duplicate` = всё) |
| Частичное пересечение не дублирует | `test_partial_overlap_no_double_count` (файл A и файл B, пересекающиеся на половине → итог = уникальное объединение) |
| Шаги часов и телефона не суммируются | `test_steps_watch_phone_no_sum` (4.1: часы 2100 / телефон 1900 → 2100); `test_steps_watch_off_phone_used` (часы сняты → берётся телефон); `test_steps_zero_watch_phone_positive`; `test_steps_split_exact_sum` (раскладка по минутам сохраняет сумму) |
| Сон объединяется | `test_sleep_union_overlap`, `test_sleep_sessions_gap_30min` |
| Пульс: нет двойного веса | `test_hr_minute_priority_device` |
| Пропуск = «нет данных» | `test_gap_is_null_not_zero` (день без данных → `steps=null,status=no_data`); `test_partial_coverage_flag`; `test_zero_steps_complete_is_zero` |
| Правила не срабатывают на пропусках | `test_rule_sleep_streak_gap_breaks`, `test_rule_not_fired_on_no_data_days` (по всем 6 правилам) |
| Рекомендация → UI и почта | `test_recommendation_in_ui_and_mail` (после файла `GET /recommendations` содержит open, в `mail_inbox` ровно одно письмо) |
| Нет повторов, пока ситуация не изменилась | `test_no_repeat_while_active` (повторная загрузка и 4-я ночь: одна рекомендация, одно письмо, `evidence` обновлён) |
| Автозакрытие при исправлении | `test_auto_resolve_on_fix` (загрузка ночи ≥ 6 ч → `resolved`, `auto_cleared`); `test_reopen_creates_new_row_and_mail` |
| Несколько рекомендаций → одно письмо | `test_digest_single_mail_multi_recs` (файл запускает ≥ 3 правила → 1 письмо с 3 пунктами); `test_digest_appends_to_pending` |
| Тихие часы | `test_quiet_hours_defer_until_end` (22:30, окно 22:00–08:00 → `send_after` 08:00 локального); `test_quiet_hours_midnight_wrap_boundaries` (21:59/22:00/07:59/08:00/00:00); `test_quiet_hours_changed_tz`; `test_quiet_hours_send_after_end` (письмо уходит после конца) |
| Повторные попытки почты | `test_outbox_retry_backoff` (sink недоступен → `failed`, затем `sent`; `dead` после 5); `test_outbox_idempotent_message_id` |
| Отключённое правило | `test_disabled_rule_not_fired`, `test_disable_keeps_history_and_resolves_open`, `test_disable_cancels_pending_mail` |
| Цель достигнута — уведомление сразу | `test_goal_notification_on_chunk` (уведомление до `upload.done`); `test_goal_notification_not_duplicated` (повторная загрузка); `test_goal_revoked_then_reached` |
| Сон вечер→утро | `test_sleep_assigned_to_wake_day` (22:40→06:25 → день пробуждения); `test_nap_same_day`; `test_sleep_across_midnight_utc_not_local` |
| Смена пояса в поездке | `test_tz_travel_day_23h`, `test_tz_travel_day_25h` (`day_length_min`); `test_tz_travel_no_loss_no_dup` (сумма по дням == сумме минут за диапазон); `test_tz_device_offset_detection` (авто-период, идемпотентность); `test_tz_settings_recompute` (смена пояса в настройках пересчитывает сводки, `tz_version`) |
| Формат/валидация | `test_parse_ok_all_types`, `test_parse_bad_lines_counted`, `test_unknown_version_rejected` |
| Сводки и сравнения | `test_resting_hr_definition`, `test_peak_hr_minute_mean`, `test_weekly_valid_days`, `test_compare_incomplete_week_uses_avg`, `test_compare_not_comparable_reason` |
| Два клиента | `test_sse_two_subscribers_same_user_both_receive`; `test_concurrent_uploads_same_user_serialized` (нет дублей и deadlock); `test_concurrent_feedback_and_rule_toggle`; изоляция пользователей `test_user_isolation` |
| Очередь | `test_jobs_skip_locked_two_workers_no_double_run`, `test_job_resume_from_byte_offset` |

**E2E-сценарий с двумя клиентами** (Playwright, два независимых browser context = две сессии одного пользователя, A и B; сервисы запущены, тестовая БД чистая, цель шагов 8000, сна 7 ч, email задан, тихие часы выключены):
1. A и B входят под одним логином, открывают «Дашборд» и «Рекомендации».
2. A загружает сгенерированный файл (30 дней, пропуски, часы+телефон, три ночи подряд < 6 ч в конце). Ожидание: у **обоих** клиентов виден прогресс-бар, процент растёт; цифры сводки на дашборде растут до завершения; B при этом может переключать вкладки (UI не завис).
3. В середине обработки у обоих появляется toast «Цель достигнута: шаги» (ровно один на клиента; в списке уведомлений одна запись); после — не появляется повторно.
4. После `upload.done` у обоих открыта рекомендация `sleep_short_streak`; в `GET /dev/mailbox` ровно одно письмо, в нём все сработавшие рекомендации.
5. B ставит «полезно» — у A через SSE отметка появляется без перезагрузки; A отключает правило `sleep_short_streak` — у B рекомендация перемещается в «закрытые» с причиной «правило отключено», в истории остаётся.
6. A загружает тот же файл повторно — счётчики сводки не меняются, нового письма и новых рекомендаций нет; «нет данных» дни отображаются серым «нет данных», не нулями.
7. A меняет пояс (`Europe/Moscow` → `Asia/Yekaterinburg`, `scope=all_history`) — у обоих сводка пересчитывается, день без потерь; затем загружает файл с поездкой (смена смещения +3→+5 внутри дня) — день переезда 22 ч (или соответствующая длина), суммы сохранены.
8. Тихие часы: B включает 22:00–08:00, A (с `HT_FAKE_NOW` 22:30) загружает файл, провоцирующий рекомендацию — в `GET /outbox` статус `pending`, `defer_reason=quiet_hours`; после перевода часов на 08:01 письмо уходит единожды.

---

## Спорные места и допущения

1. **Приоритет устройства на минуту** (4.1) — собственное правило. Альтернативы (max по интервалу, сумма) хуже; исключение «часы дали 0, телефон >0» — эвристика, не ТЗ-гарантия. Приоритет по умолчанию watch > phone; пользователь меняет.
2. **Порядок загрузки определяет победителя** при разных значениях одной минуты одного устройства (последняя загрузка побеждает). Без версий источника иное недоказуемо.
3. **Старые интервалы сна не удаляются** при загрузке исправленных границ того же устройства (union); уменьшить сон исправлением нельзя, только добавить. Цена за простоту и отсутствие дублей.
4. **День по таймлайну, а не по `offset_min`**: смещение в записях используется лишь для автообнаружения смены пояса (порог 30 мин, ≥ 3 записи подряд). Короткий перелёт с одной записью с новым смещением не распознаётся — пользователь правит пояс вручную.
5. **`as_of` вместо «сегодня»** для правил и «свежести» уведомлений: иначе исторические выгрузки генератора не давали бы рекомендаций. Следствие: загрузка старого периода создаёт «открытые» рекомендации по его концу и письма о них.
6. **Бэкфилл**: достижения целей при последовательной загрузке многих дней создают уведомление по каждому дню, прошедшему как «самый новый»; UI группирует. Письма — только по рекомендациям.
7. **Дайджест** — «одно письмо за прогон» с окном склейки 60 с и дописыванием в неотправленное письмо, а не «ровно одно письмо в календарный день». Два файла с интервалом в часы дадут два письма; строгое «одно в день» отвергнуто, чтобы не откладывать новые рекомендации на сутки.
8. **Пороги правил фиксированы** (в UI видны, не редактируются). Правила `steps_low_streak` и `sleep_debt_week` требуют активной цели; без цели они неактивны.
9. **Минимальное покрытие** (120/720 минут, сон ≥ 60 мин, пульс ≥ 30 минут) — произвольные, но документированные пороги; вынесены в `config.py`.
10. **Недели ISO (Пн–Вс)**; недельные значения не материализуются, считаются из дневных на чтении.
11. **Регистрация доступна из UI** (в ТЗ только «вход»): нужна для демо и тестов; пароли — argon2id.
12. **SMTP-sink хранит письма в БД** и отдаёт их через `/dev/mailbox` только в `HT_ENV=dev|test`; реальных получателей нет.
13. **Только PostgreSQL-инфраструктура**: очередь, события, почта на таблицах — один сервис вместо брокера; платой становится опрос раз в 2 с как страховка NOTIFY.
14. **Один uvicorn-процесс** api; при нескольких процессах каждый держит свой LISTEN (схема выдержит), но SSE-подписчики привязаны к своему процессу.
