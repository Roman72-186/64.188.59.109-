# Прокладка оплат Т-Банк ↔ shalamov.io

Принимает оплату через Т-Банк и после подтверждённого платежа назначает контакту
тег в shalamov.io (тег запускает авторассылку). Касса/чеки/возвраты — вне scope.

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
- `POST /webhook/tbank` — уведомление Т-Банка; назначает тег синхронно, при сбое shalamo отдаёт `503`.
- `GET /health` — `{"status":"ok"}`.

Подробности эндпоинтов и переменных контакта — [DOCS.md §10](DOCS.md).

## Деплой
systemd-юнит и nginx — в [deploy/](deploy/). Кратко: см. [DOCS.md §4–§7](DOCS.md).
