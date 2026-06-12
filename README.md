# Прокладка оплат Т-Банк ↔ shalamov.io

Принимает оплату и после подтверждённого платежа назначает контакту тег в
shalamov.io (тег запускает авторассылку). Касса/чеки/возвраты — вне scope.

**Мультипровайдер:** карта/СБП/рассрочка идут через эквайринг Т-Банка, **Долями —
через прямой Partner API Долями** (`provider: dolyame`, mTLS+Basic, своя Долями-only
форма и отдельный webhook), **кредит/рассрочка на крупные суммы — через T-Bank Credit
Broker** (`provider: tbank_credit`, forma.tbank.ru, статус — фоновым опросом `GET /info`
и/или отдельным webhook). Провайдер выбирается на способе оплаты в `config.yaml`.

Стек: Python 3.10+ · FastAPI · SQLite. Поведение задаётся **только** через
`config.yaml` — код под конкретный проект не правится.

## Документация
- **[PRD.md](PRD.md)** — требования и критерии приёмки (источник истины).
- **[DOCS.md](DOCS.md)** — установка, настройка, эксплуатация, траблшутинг.
- **[deploy/ssh-access.md](deploy/ssh-access.md)** — доступ к VPS.

## Быстрый старт (локально)

```bash
python -m venv venv
venv/bin/pip install -r requirements.txt        # Windows: venv\Scripts\pip
cp config.example.yaml config.yaml              # заполнить ключи и secret_token
venv/bin/python test_flow.py                    # прогон потока на моках, все PASS
venv/bin/uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
curl http://localhost:8000/health               # {"status":"ok"}
```

> `secret_token`: `python -c "import secrets; print(secrets.token_hex(32))"`

## Тесты

```bash
venv/bin/pytest -q        # unit + интеграционные (подпись, идемпотентность, 8 статусов, 503)
venv/bin/python test_flow.py   # автономный happy-path/edge на моках
```

## API
- `POST /init-payment` — создать платёж (заголовок `X-Secret-Token`), вернуть `pay_url`. Статусы — PRD §7.4.
- `POST /webhook/tbank` — уведомление Т-Банка (эквайринг); назначает тег синхронно, при сбое shalamo отдаёт `503`.
- `POST /webhook/dolyame` — уведомление прямого Долями; защита по IP-allowlist, источник истины `GET /info`, на `wait_for_commit` → `commit` → тег.
- `POST /webhook/tbank_credit` — доп. канал для T-Bank Credit Broker; защита по IP-allowlist, источник истины `GET /info`. Основной канал — фоновый поллер (`tbank_credit.poll_interval_seconds`).
- `GET /health` — `{"status":"ok"}`.

Подробности эндпоинтов и переменных контакта — [DOCS.md §10](DOCS.md).

## Деплой
systemd-юнит и nginx — в [deploy/](deploy/). Кратко: см. [DOCS.md §4–§7](DOCS.md).
