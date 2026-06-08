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

**Мультипровайдер:** карта/СБП/рассрочка — через эквайринг Т-Банка; **Долями — через
прямой Partner API Долями** (`provider: dolyame`). Провайдер выбирается на способе оплаты
в `config.yaml`.

Поток (Т-Банк): бот → `POST /init-payment` → Т-Банк выдаёт `pay_url` → пользователь платит →
`POST /webhook/tbank` → синхронно назначается тег в shalamov.io → авторассылка по тегу.
Поток (Долями): `/init-payment` → `create` → `pay_url` → оплата → `POST /webhook/dolyame`
→ `commit` → тег. Тег **только назначается** прокладкой; удаляется первым шагом
авторассылки внутри shalamov.io.

## Команды

```bash
# окружение: venv на Python 3.12 (см. ниже про версии)
venv\Scripts\pip install -r requirements.txt        # Windows
cp config.example.yaml config.yaml                  # заполнить ключи + secret_token

venv\Scripts\python -m pytest -q                    # 62 теста: unit + интеграционные
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
- **Логи** — `logs/app.log` + stdout; секреты маскируются (`logging_setup.mask_secrets`).

## Деплой / доступ

- VPS, systemd, nginx+HTTPS — [deploy/](deploy/) и [DOCS.md §4–§7](DOCS.md).
- SSH к серверу `64.188.59.109` — [deploy/ssh-access.md](deploy/ssh-access.md).
  Важно: Windows-OpenSSH к серверу не цепляется (KEX) — рабочий клиент `plink` (PuTTY).
- Секреты — в `config.yaml` и `.env` (оба в `.gitignore`, в репозиторий не коммитятся).
