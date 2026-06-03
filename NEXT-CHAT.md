# Промт для нового чата

> Скопируй блок ниже целиком в новый чат Claude Code (рабочая папка — этот проект).

---

```
Проект: прокладка оплат Т-Банк ↔ shalamov.io (Python 3.10+ · FastAPI · SQLite).
Рабочая папка уже открыта. Сначала прочитай CLAUDE.md, потом PRD.md (источник истины)
и DOCS.md (установка/эксплуатация). Код уже написан и протестирован — НЕ переписывай
с нуля, продолжай с текущего состояния.

ЧТО УЖЕ СДЕЛАНО (готово и проверено):
- Вся логика: app/{config,database,tbank,shalamo,schemas,main,logging_setup}.py
- Тесты: pytest 23/23 и python test_flow.py 12/12 — зелёные локально (venv на Python 3.12)
  И на сервере (Ubuntu 22.04, Python 3.10.12).
- Деплой-артефакты: deploy/ (systemd tbank-proxy.service с --factory, nginx.conf.example,
  ssh-access.md), README.md.
- SSH к серверу 64.188.59.109 настроен, доступы в .env (см. deploy/ssh-access.md).
- Реквизиты Т-Банка в config.yaml: активен ТЕСТОВЫЙ терминал на rest-api-test.tinkoff.ru,
  боевой лежит закомментированным рядом.
- config.yaml и .env — в .gitignore (секреты в репозиторий не коммитятся).

ВАЖНЫЕ НЮАНСЫ ОКРУЖЕНИЯ:
- Запуск: venv\Scripts\uvicorn app.main:create_app --factory --port 8000 (factory-паттерн!).
- Локальный venv — на Python 3.12 (системный 3.14 ломает сборку pydantic-core).
- SSH к серверу: Windows-OpenSSH НЕ цепляется (ошибка KEX) — использовать plink (PuTTY)
  с -batch -hostkey, см. deploy/ssh-access.md. Команды/копирование — через plink/pscp.
- «Зелёные тесты» = логика на МОКАХ. Реальные API Т-Банка и shalamo ещё не вызывались.

ЧТО НУЖНО СДЕЛАТЬ ДАЛЬШЕ (в порядке приоритета):
1. shalamo в config.yaml: сейчас заглушки (api_key=DEMO_SHALAMO_KEY, пути /contacts/tag и
   /contacts/variables — предполагаемые). Узнать у shalamov.io реальные: api_key, точные
   пути endpoint'ов, формат тела, схему авторизации — и вписать в блок shalamo
   (это конфиг-адаптер, код менять НЕ нужно). Детали — DOCS.md §9.1.
2. server.public_url в config.yaml: сейчас example.invalid → поставить реальный домен.
   Этот же URL (+ /webhook/tbank) прописать как webhook в ЛК Т-Банка.
3. Деплой на VPS: скопировать проект в /opt/tbank_proxy, venv, pip install, systemd-юнит
   из deploy/, nginx + HTTPS (certbot). Т-Банк шлёт webhook только по HTTPS. См. DOCS.md §4–§7.
4. Тестовый платёж на тестовом терминале: пройти оплату, проверить в logs/app.log строку
   «✅ Платёж подтверждён», статус confirmed в payments.db, и что в shalamo назначился тег
   и стартовала авторассылка. Затем повторить для СБП/Долями. Чеклист — DOCS.md §14 / PRD §13.
5. (опц.) Первый git commit — секреты уже в .gitignore. Спросить пользователя.

С ЧЕГО НАЧАТЬ: прочитать CLAUDE.md → решить, что из п.1–4 делаем первым. Скорее всего
п.1 (реальные endpoint'ы shalamo) блокирует реальный тест — начни с уточнения этого
контракта у пользователя.
```

---

## Краткий статус (для человека)

Прокладка **собрана и протестирована** (pytest 23 + test_flow 12, локально и на Py3.10
сервера). Осталось: реальные доступы shalamov.io, домен, деплой на VPS, тестовый платёж.
Реквизиты Т-Банка уже внесены (активен тестовый терминал).
