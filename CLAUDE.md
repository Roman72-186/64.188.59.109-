# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Правила работы

- **Хэндофф в новый чат → файл [NEXT-CHAT.md](NEXT-CHAT.md).** Любой промт для нового
  чата и последующие уточнения к нему пишутся/обновляются в `NEXT-CHAT.md` (не только
  выводятся в ответ). Файл — самодостаточный контекст для продолжения работы с нуля.

## Что это

Прокладка (Python 3.10+ · FastAPI · SQLite) между платёжными провайдерами и shalamov.io:
принимает оплату и после подтверждённого платежа назначает контакту тег в shalamov.io
(тег запускает авторассылку). Касса/чеки/возвраты — вне scope.
**[PRD.md](PRD.md)** — источник истины по требованиям; **[DOCS.md](DOCS.md)** —
установка/настройка/эксплуатация (синхронизирован с кодом).

**Мультипровайдер (`payment_methods[*].provider` в config.yaml):**
- `tbank` (по умолчанию) — карта/СБП/рассрочка через эквайринг Т-Банка;
- `dolyame` — Долями через прямой Partner API Долями (mTLS+Basic, своя форма);
- `tbank_credit` — кредит/рассрочка через T-Bank Credit Broker (forma.tbank.ru, Basic).

Поток (Т-Банк): бот → `POST /init-payment` → Т-Банк выдаёт `pay_url` → пользователь платит →
`POST /webhook/tbank` → синхронно назначается тег в shalamov.io → авторассылка по тегу.
Поток (Долями): `/init-payment` → `create` → `pay_url` → оплата → `POST /webhook/dolyame`
→ `commit` → тег.
Поток (Credit Broker): `/init-payment` → `create` (без `webhookURL`) → `pay_url` (форма
forma.tbank.ru) → клиент подписывает документы (`signed`) → статус узнаём фоновым опросом
`GET /info` (`tbank_credit.poll_interval_seconds`) и/или `POST /webhook/tbank_credit`
(если домен совпадёт) → (опц. `commit`) → тег.
Авто-апгрейд: если `amount >= credit_threshold_kopecks` и у товара есть `tbank_credit`-метод,
прокладка сама переключает способ оплаты на кредитный (см. `AppConfig.credit_method_for`).
Тег **только назначается** прокладкой; удаляется первым шагом авторассылки внутри shalamov.io.

## Команды

```bash
# окружение: venv на Python 3.12 (см. ниже про версии)
venv\Scripts\pip install -r requirements.txt        # Windows
cp config.example.yaml config.yaml                  # заполнить ключи + secret_token

venv\Scripts\python -m pytest -q                    # 96 тестов: unit + интеграционные
venv\Scripts\python test_flow.py                    # автономный прогон потока на моках
venv\Scripts\uvicorn app.main:create_app --factory --port 8000   # запуск; GET /health -> {"status":"ok"}
```

Один тест: `python -m pytest tests/test_webhook.py::test_503_when_shalamo_fails_then_reprocess -q`.

**Версии Python:** venv собирается на **CPython 3.12** (`...\uv\python\cpython-3.12.13...`).
Системный Python здесь — 3.14, под него нет wheel'ов `pydantic-core` и сборка из Rust
падает (нет MSVC-линкера). Сервер (VPS) — Ubuntu 22.04, Python 3.10.12.

## Архитектура (ключевое, что не видно из одного файла)

- **Всё поведение — через `config.yaml`** (товары, способы оплаты, теги, endpoint'ы
  shalamo). Код под проект не правится. `amount` товара — **в копейках**.
- **`app/main.py::create_app(config, db, tbank, shalamo)`** — фабрика приложения;
  компоненты инъектируются (тесты передают моки + temp SQLite). uvicorn запускается
  через `--factory app.main:create_app` — без config.yaml падает с понятной ошибкой,
  импорт модуля для тестов при этом ничего не создаёт.
- **shalamo — конфиг-адаптер** ([app/shalamo.py](app/shalamo.py)): путь/метод/тело/авторизация
  читаются из конфига. Реальный контракт API неизвестен → правится только `config.yaml`.
- **Мульти-терминал (конфиг-driven):** способ оплаты можно вести через отдельный
  магазин Т-Банка (`tbank.extra_terminals` + `payment_methods[*].terminal`) — так форма
  показывает только этот способ (в API Init фильтра способов нет; это уровень терминала).
  Init/GetState уходят на терминал способа; **подпись webhook проверяется по `TerminalKey`
  из тела** (`config.password_for_terminal_key`), т.к. магазины подписаны разными паролями.
  Без `extra_terminals` поведение прежнее.
- **Прямой Долями — отдельный провайдер** ([app/dolyame.py](app/dolyame.py)): способ с
  `provider: dolyame` идёт НЕ через эквайринг, а через Partner API Долями (`partner.dolyame.ru`)
  — даёт Долями-only форму. **mTLS-сертификат обязателен на КАЖДЫЙ запрос** (cert/key из
  конфига, абсолютные пути) + Basic + `X-Correlation-ID`. Суммы в API — в рублях (конвертация
  из копеек через Decimal). **Двухфазность:** оплата 25% → `wait_for_commit` → прокладка зовёт
  `commit` (захват) → `committed` → тег. Свой webhook **`POST /webhook/dolyame`**: тело НЕ
  подписано → источник истины `GET /info`; доверенность отправителя — по **IP-allowlist**
  (`webhook_allowed_subnet`, реальный IP из `X-Real-IP` от nginx). Та же модель идемпотентности
  и 503-повтора, что у Т-Банка. `provider: dolyame` требует блок `dolyame` в конфиге.
  **Фискализация (чек 54-ФЗ на почту клиента, конфиг-driven):** при `dolyame.fiscalization:
  enabled` в `Create`/`Commit` шлётся `fiscalization_settings: {type: enabled, params:
  {create_receipt_for_committed_items: true}}` + в каждую позицию `receipt`
  (`tax`/`payment_method`/`payment_object`/`measurement_unit`, поля из общего блока `receipt`,
  `measurement_unit` фиксирован `шт` — см. `AppConfig.dolyame_item_receipt`). Чек формируется
  Долями на **commit** (двухфазность), позиции в `Create` и `Commit` ОБЯЗАНЫ совпадать. Прокладка
  лишь передаёт данные — печатает чек касса/ОФД на стороне мерчанта у Долями; без подключённой
  кассы письмо не придёт (не баг прокладки). `disabled` → `fiscalization_settings: {type: disabled}`,
  позиционный `receipt` не шлётся.
- **T-Bank Credit Broker — отдельный провайдер** ([app/tbank_credit.py](app/tbank_credit.py)):
  способ с `provider: tbank_credit` идёт через `forma.tbank.ru/api/partners/v2/orders`
  (НЕ эквайринг). `Create` — без авторизации (shopId+showcaseId+promoCode в теле, из ЛК
  business.tbank.ru/posloans); `Commit`/`Cancel`/`Info` — Basic (`showcase_id:api_password`).
  Суммы — в рублях (Decimal). Статусы заявки: `new → inprogress → approved → signed →
  canceled|rejected`. На `signed`: если `commit_on_webhook: true` — прокладка зовёт `Commit`
  (ручное подтверждение, обязателен в течение 14 дней), иначе тег ставится сразу (авто-
  подтверждение настраивается в ЛК). `provider: tbank_credit` требует блок `tbank_credit`
  в конфиге. **Авто-апгрейд по сумме:** `AppConfig.credit_threshold_kopecks` — если `amount`
  запроса `/init-payment` ≥ порога и у товара есть метод с `provider: tbank_credit`, прокладка
  сама подменяет `payment_method` на кредитный (`credit_method_for`), независимо от того,
  что запросил бот.
  **`webhookURL` в `Create` НЕ передаётся**: Т-Банк отклоняет `Create`, если домен
  webhookURL не совпадает с доменом витрины клиента (а у витрин разных клиентов он свой —
  прокладка на это не влияет). Вместо webhook — **общая функция `process_credit_status`**
  в [app/main.py](app/main.py) (источник истины всегда `GET /info`, тело webhook не
  подписано), вызывается из двух мест:
  - **`POST /webhook/tbank_credit`** — если у клиента домен витрины совпадёт с доменом
    прокладки и webhook дойдёт; IP-allowlist через `webhook_allowed_subnet` (пусто = без
    проверки);
  - **фоновый поллер** (`tbank_credit.poll_interval_seconds`, 0 = выключен) — каждые N
    секунд опрашивает `GET /info` по заявкам без `tag_assigned_at`/`fail_tag_assigned_at`
    (`database.get_pending_credit_orders`, отсечка `CREDIT_POLL_MAX_AGE_SECONDS` = 30 дней) —
    основной канал, не зависит от домена.
- **CloudKassir — единая онлайн-касса (фискализация 54-ФЗ)** ([app/cloudkassir.py](app/cloudkassir.py)):
  карту/СБП через эквайринг Т-Банка фискализирует касса автоматически (чек идёт сам),
  а Долями и рассрочка/кредит своих чеков НЕ дают. CloudKassir (CloudPayments KKT,
  `POST {api_url}/kkt/receipt`, Basic `public_id:api_secret`) подключается как касса
  мерчанта и пробивает чек по этим каналам. **Модель — отдельный факт `receipt_sent_at`
  в БД** (как `paid_at`/`tag_assigned_at`): фискализация РАЗВЯЗАНА с webhook и назначением
  тега — **фоновая реконсиляция** (`cloudkassir.poll_interval_seconds`) пробивает чек по
  оплаченным заказам без `receipt_sent_at` (`database.get_unfiscalized_orders` по каналам
  из `cloudkassir.fiscalize_providers`, отсечка `CLOUDKASSIR_MAX_AGE_SECONDS`=30 дней),
  поэтому транзиентный сбой кассы не теряется и не задерживает выдачу доступа. **Не гейт
  доступа**, best-effort. Идемпотентность: `InvoiceId`=`order_id` дедуплицируется кассой
  (+ `X-Request-ID`), `mark_receipt_sent` — только при `Queued`. Признаки расчёта
  (`taxation`/`tax`/`payment_method`/`payment_object`) берутся из общего блока `receipt`
  (маппинг строк 54-ФЗ → числовые коды KKT в `cloudkassir.py`), чтобы чек совпадал с
  картой/СБП. Email/Phone сохраняются в БД на `/init-payment` (для отложенного чека),
  иначе fallback `receipt.email/phone`. **Миграция `receipt_sent_at` бэкфиллит** существующие
  заказы как «обработанные» → касса не пробивает чеки задним числом по заказам до её
  подключения. При фискализации Долями через CloudKassir у Долями ставим
  `fiscalization: disabled` (иначе двойной чек). **СТАТУС: собрано, ждёт боевой валидации** —
  схема `/kkt/receipt` не подтверждена реальным ответом (см. «PENDING LIVE VALIDATION» в
  [app/cloudkassir.py](app/cloudkassir.py)); боевой `fiscalize_providers: [dolyame]` (рассрочка
  отложена до согласования полей чека).
- **Два независимых факта в БД** ([app/database.py](app/database.py)): `paid_at`
  (банк подтвердил) и `tag_assigned_at` (доступ выдан). На этом стоит вся логика
  «оплачено, но тег не назначен» (PRD §7.3) и идемпотентность.
- **Идемпотентность webhook** — `atomic_capture()`: один `UPDATE ... WHERE
  tag_assigned_at IS NULL`. Повторный webhook после успеха не переназначает тег.
- **Модель webhook (PRD §7.8, НЕ старая фоновая):** тег назначается **синхронно**,
  до 2 быстрых попыток; при неудаче shalamo — **HTTP 503 (не `OK`)**, платёж остаётся
  пере-обрабатываемым, Т-Банк повторит. Поэтому `shalamo.timeout_seconds * 2` должно
  укладываться в таймаут webhook Т-Банка (~10с).
- **Тег — «гейт» доступа:** успех = тег назначен (запускает авторассылку). Переменные
  шлются перед тегом best-effort; их сбой не блокирует доступ (пишется в лог).
- **Чек 54-ФЗ — только передаётся в Init, не реализуется прокладкой**
  (`ReceiptConfig`/`AppConfig.build_receipt` в [app/config.py](app/config.py)): боевой
  терминал с онлайн-кассой требует объект `Receipt` в каждом Init, иначе ошибка
  `309 {request.validate.expected.receipt}`; без блока `receipt`/`enabled: false`
  Receipt не шлётся (тестовый терминал). Чек = одна позиция (товар); сумма берётся
  из `amount` запроса (не `product.amount` — иначе разойдётся с Init). Email/Phone
  получателя обязательны по 54-ФЗ — из `/init-payment` либо fallback `receipt.email/phone`.
- **Тег отказа (`fail_tags_by_method`)** — отдельный опциональный факт в БД
  (`fail_tag_assigned_at`, `capture_fail_tag`/`mark_fail_tag_assigned` в
  [app/database.py](app/database.py)): при терминальном негативном статусе у Долями,
  Credit Broker (rejected/canceled) или обычного эквайринга Т-Банка
  (`NEGATIVE_TBANK_STATUSES` в `/webhook/tbank`: REJECTED/DEADLINE_EXPIRED/CANCELED/
  AUTH_FAIL) прокладка best-effort назначает тег отказа (триггер авторассылки «оплата
  не прошла») — не гейт доступа. Если для способа тег не задан в
  `tags_by_method`/`fail_tags_by_method`, ничего не шлётся (старое поведение).
- **Логи** — `logs/app.log` + stdout; секреты маскируются (`logging_setup.mask_secrets`).

## Деплой / доступ

- VPS, systemd, nginx+HTTPS — [deploy/](deploy/) и [DOCS.md §4–§7](DOCS.md).
- SSH к серверу `64.188.59.109` — [deploy/ssh-access.md](deploy/ssh-access.md).
  Важно: Windows-OpenSSH к серверу не цепляется (KEX) — рабочий клиент `plink` (PuTTY).
- Секреты — в `config.yaml` и `.env` (оба в `.gitignore`, в репозиторий не коммитятся).
