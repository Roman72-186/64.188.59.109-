# NEXT-CHAT.md — хэндофф для нового чата

> Правило (см. CLAUDE.md): промт для нового чата и уточнения к нему живут здесь.
> Это самодостаточный контекст для продолжения работы с нуля. Обновлять при изменениях.
> Скопируй блок «Промт» целиком в новый чат Claude Code (рабочая папка — этот проект).

---

## Промт

```
Контекст: T-Bank Credit Broker (рассрочка installment_3/6/10) интегрирован и был
задеплоен (commits ec2ea92, f5e7bf6, 85/85 тестов), но реальный /init-payment падал:

  "validations":{"webhookURL":"...домен... должен совпадать с доменом вашего сайта"}

Расследование (см. «Уточнения» 10-11.06.2026 ниже) установило: точка Credit Broker
зарегистрирована на домен клиента novoseltsevyayest.ru (НЕ домен прокладки
pay.sushi-house-39.ru); вкладка «Интеграция» в ЛК НЕ имеет полей под webhook вообще;
поле «HTTP-уведомления о статусе заявки» принимает только apex-домен сайта,
поддомены (pay.novoseltsevyayest.ru) ОТКЛОНЯЕТ. DNS/поддомен — тупиковый путь.

РЕШЕНИЕ (реализовано локально 11.06.2026, 90/90 тестов зелёные, НЕ ЗАДЕПЛОЕНО):
webhookURL вообще убран из Create. Подтверждено реальным curl на forma.tbank.ru
(пользователь прогнал сам) — Create БЕЗ webhookURL прошёл, вернул заявку
{"id":"e18e65aa-647a-4c7c-9c0c-04b6c351c008","link":"https://forma.tbank.ru/online/sso/..."}
(тестовая заявка, не подписана клиентом, безобидна, истечёт сама — не трогать).

Источник истины как и раньше — GET /info (Basic auth, без ограничений по домену).
Вместо webhook — общая функция process_credit_status() в app/main.py, вызывается:
  1) из /webhook/tbank_credit (если у БУДУЩЕГО клиента домен совпадёт — сработает);
  2) из НОВОГО фонового поллера _poll_credit_orders() — основной канал теперь.

Новое в конфиге: tbank_credit.poll_interval_seconds (число секунд, 0 = выключено,
дефолт). database.get_pending_credit_orders() выбирает заявки без tag_assigned_at/
fail_tag_assigned_at, отсечка CREDIT_POLL_MAX_AGE_SECONDS = 30 дней (app/main.py).

ЗАДАЧА НОВОГО ЧАТА: задеплоить и проверить реальный installment_3 e2e.

ПРОЕКТ
Рабочая папка открыта (git, ветка main). Прокладка Python 3.10+ / FastAPI / SQLite.
Сначала прочитай: CLAUDE.md (раздел Credit Broker уже обновлён под новую схему),
app/tbank_credit.py (docstring), app/main.py (process_credit_status,
_poll_credit_orders, lifespan, init_credit_payment), app/config.py
(TBankCreditConfig.poll_interval_seconds, credit_broker_methods),
app/database.py (get_pending_credit_orders), deploy/ssh-access.md.
venv на CPython 3.12. Тесты: venv\Scripts\python -m pytest -q (90 passed).

ШАГИ ДЕПЛОЯ
1. Запушить коммит с этими изменениями (если ещё не запушен), задеплоить на
   64.188.59.109 (plink/pscp — см. deploy/ssh-access.md и память про деплой:
   маскированные diff'ы config.yaml, бэкап перед заменой, git reset --hard
   только с подтверждением владельца).
2. В СЕРВЕРНЫЙ config.yaml добавить tbank_credit.poll_interval_seconds (рекомендация
   60-120 сек) — в локальном репо его нет (0/не задан), это ОЖИДАЕМОЕ расхождение
   при структурном diff'е, добавить вручную.
3. Прогнать pytest на сервере (90 passed), перезапустить tbank-proxy.service,
   проверить /health.
4. Тестовый /init-payment с payment_method=installment_3, amount >= 310800 копеек
   (минимум 3108₽ для installment_3 — см. находку 11.06.2026), force: true.
   Ожидаем pay_url БЕЗ ошибки webhookURL (это и есть критерий успеха).
5. Подождать ~poll_interval после изменения статуса заявки (signed/rejected),
   проверить логи (`poll Credit Broker: ...`) и таблицу payments — tag_assigned_at
   / fail_tag_assigned_at должны проставиться так же, как раньше через webhook.

ОСТАВШИЕСЯ TODO (из предыдущих хэндоффов, ниже не нумерую заново):
- Создать в shalamov.io теги paid_installment_basic / fail_installment_basic
  + автоворонки.
- webhook_allowed_subnet для tbank_credit (сейчас пусто) — менее критично теперь,
  основной канал статусов — поллинг, не входящий webhook.
```

---

## Уточнения (дописывать сюда по мере появления)

- **08.06.2026 — ДЕПЛОЙ ВЫПОЛНЕН.** Код на сервере (58 тестов), сертификат mTLS в
  `/opt/tbank_proxy/certs/` (www-data, key 600), серверный `config.yaml` — абсолютные
  cert paths + `provider: dolyame` на `dolyami`. Бэкап: `config.yaml.bak-20260608-predolyame`.
  Сервис перезапущен, `/health` ok локально и по публичному URL.
- **Боевым трафиком подтверждено:** `create→link`; доставка webhook с реального IP Долями
  `91.194.226.250` (внутри allowlist `91.194.226.0/23` — править НЕ нужно); `/info` как
  источник истины; nginx X-Real-IP; негативные статусы `canceled`/`rejected` → отказ.
- **Закрытые неизвестные:** фискализация НЕ требуется (checkout прошёл без чека); 99 ₽
  выше минимума (график 24+25+25+25); боевой checkout активирован. Generic-ошибка формы
  «сервис временно недоступен» была из-за НЕВЕРНОГО телефона (формат `+79991234567`).
- **НЕ проверен только позитив `wait_for_commit → commit → тег`:** на тесте 08.06 банк
  отказал клиенту по скорингу BNPL (`rejected`) — это внешнее, не код. Закрыть: любой
  проходящий скоринг плательщик оплачивает 99 ₽ → в логах ждём `commit OK` + `✅ тег`.
- **TODO:** ротировать боевые креды Долями у менеджера (фигурировали в переписке).
- **09.06.2026 — `force` + `amount` обязателен** (commits `61e52d6`, `8af9577`, 67 тестов).
  `force: true` — пропускает проверку активной ссылки (PRD §7.2), всегда создаёт новый
  платёж. `amount` стал **обязательным** полем: платформа всегда передаёт сумму в копейках,
  `ProductConfig.amount` в конфиге больше не используется (необязателен, можно убрать).
  Задеплоено, сервис здоров.
  **Важно:** пока не прошёл ни один боевой платёж, в БД висит тестовый заказ на 50 ₽
  (contact `3010325`, dolyami) — бот получал его как `existing_active`. Лечится:
  `force: true` в запросе (или подождать час — TTL активной ссылки 1 ч). После первого
  реального `committed` ситуация нормализуется и `force` не нужен.
- **Лимит Долями:** максимум **30 000 ₽** на заказ (ограничение Partner API). Продукты
  дороже 30 000 ₽ через Долями не пройдут (`create` вернёт ошибку) — для них только
  `card` / `sbp` / `installment` (рассрочка Т-Банка, лимиты выше).
- DOCS.md обновлён под прямой Долями (§5 блок dolyame, `/webhook/dolyame`, nginx X-Real-IP).
- **08.06.2026 — ТЕГ ОТКАЗА реализован и протестирован** (commit `ee3f60b`, 62 теста).
  При терминальном негативе Долями (`rejected`/`canceled`) прокладка назначает отдельный
  «тег отказа» — триггер авторассылки «оплата не прошла». Успех (`paid_dolyami_*`) не
  затронут. Best-effort: сбой shalamo логируется, webhook отвечает `OK` (нет `paid_at` →
  нет страховки через `/init-payment`, поэтому НЕ 503). Факт хранится отдельным столбцом
  `fail_tag_assigned_at`; повтор webhook не дублирует. Только Долями (ветка Т-Банка НЕ
  тронута осознанно — иначе деклайны карт вызывали бы retry-storm при сбое shalamo).
- **09.06.2026 — ТЕГ ОТКАЗА АКТИВИРОВАН В ПРОДЕ.** Тег `fail_dolyami_basic` создан в
  shalamov.io с авторассылкой «оплата не прошла» (подтверждено владельцем). В `config.yaml`
  (локально и на сервере) под `products.course_basic` добавлено:
  ```yaml
  fail_tags_by_method:
    dolyami: fail_dolyami_basic
  ```
  Код задеплоен (`git checkout origin/main -- ...` поверх «грязного» серверного чек-аута —
  предварительно построчно сверено, что расхождение это ровно фича тега отказа и ничего
  больше), 62/62 теста на сервере зелёные, миграция `fail_tag_assigned_at` прошла на старте
  автоматически, сервис перезапущен (`tbank-proxy.service` active), `/health` ok локально и
  по `https://pay.sushi-house-39.ru/health`. Резервная копия серверного конфига —
  `config.yaml.bak-20260608-predolyame` (старая, ещё с шага активации Долями).
  **Проверка в проде:** при следующем негативном скоринге Долями (`rejected`/`canceled`)
  ждать в логах `⛔ Отказ оплаты — тег отказа назначен` + появление тега в shalamov.io.
- **09.06.2026 — ПЕРЕОПРЕДЕЛЕНИЕ СУММЫ в `/init-payment`.** (commit `7fc92e4`, 65 тестов.)
  В тело `/init-payment` добавлено опциональное поле `amount` (целое, в копейках):
  платформа (бот/shalamov.io) формирует сумму сама и передаёт её в запросе. Защита: запрос
  идёт по `X-Secret-Token`, конечный клиент его не видит и подменить сумму не может. Если
  `amount` не передан — берётся `product.amount` из `config.yaml` (поведение прежнее).
  Сумма пробрасывается во все места: БД, лог, Init Т-Банка, чек 54-ФЗ (`build_receipt`),
  Долями `create`. `config.yaml` не менялся. Задеплоено: 65/65 тестов на сервере, сервис
  перезапущен, `/health` ok.

---

## Краткий статус (для человека)

**ДЕПЛОЙ ВЫПОЛНЕН (08.06.2026).** Код на сервере 64.188.59.109, сертификат mTLS
в `/opt/tbank_proxy/certs/`, `provider: dolyame` включён, сервис работает. Боевым трафиком
подтверждено: `create→link`, webhook с реального IP Долями (в allowlist), `/info`, негативные
статусы `rejected`/`canceled`. Закрыто: чек НЕ нужен, 99 ₽ выше минимума, checkout активен,
причина generic-ошибки формы — неверный телефон. **Не закрыт только позитив `commit → тег`**:
на тесте банк отказал клиенту по скорингу BNPL (внешнее, не код) — нужен проходящий скоринг
плательщик. TODO: ротировать боевые креды Долями у менеджера.

**09.06.2026 — ТЕГ ОТКАЗА ЗАДЕПЛОЕН И АКТИВЕН.** При `rejected`/`canceled` контакту
назначается `fail_dolyami_basic` (тег + авторассылка «оплата не прошла» уже созданы в
shalamov.io). Commit `ee3f60b`, `config.yaml` правлен (`fail_tags_by_method`), сервис здоров.
Ожидаем реального негативного статуса в логах.

**09.06.2026 — СУММА В ЗАПРОСЕ.** Поле `amount` добавлено в `/init-payment` (commit `7fc92e4`,
65 тестов). Платформа передаёт сумму в копейках — прокладка использует её вместо `config.yaml`.
Если не передано — поведение прежнее. `config.yaml` не менялся. Сервис перезапущен, здоров.

**09.06.2026 — AMOUNT ОБЯЗАТЕЛЕН, КОНФИГ НЕ НУЖЕН.** `amount` стал обязательным полем
`/init-payment` — платформа всегда передаёт сумму сама, из `config.yaml` она больше не
берётся (commit `8af9577`, 67 тестов, задеплоено). `ProductConfig.amount` в конфиге
теперь необязателен и игнорируется. Поле `force: true` пропускает проверку активной
ссылки — удобно для тестирования с разными суммами (commit `61e52d6`).

**Открытые вопросы:** позитив `commit → тег` (нужен платёж с прошедшим скорингом BNPL);
тег отказа — ждём первого реального `rejected`/`canceled` в логах. Детали — в «Уточнениях».

**09.06.2026 — T-BANK CREDIT BROKER (кредит/рассрочка > 30 000 ₽) — КОД ГОТОВ, НЕ ЗАДЕПЛОЕН.**
Новый провайдер `provider: tbank_credit` + 82 теста (было 67). Авто-апгрейд:
`credit_threshold_kopecks: 3000000` → если `amount >= 30 000 ₽` и у товара есть credit-метод —
прокладка автоматически переключается на Credit Broker. Новые файлы/изменения:
`app/tbank_credit.py`, `app/config.py` (TBankCreditConfig, credit_threshold_kopecks),
`app/main.py` (init_credit_payment + /webhook/tbank_credit), `config.example.yaml`.
**Нужно для деплоя:**
1. В ЛК Т-Бизнеса (бизнес.тбанк.ру → POS-кредитование) получить: ShopId, ShowcaseId, PromoCode,
   API-пароль (Магазины → Настройки API). Тестовый API: `https://forma.tinkoff.ru/api/partners/v2`.
2. Добавить в `config.yaml` блок `tbank_credit` + `credit_threshold_kopecks: 3000000`.
3. Добавить способ `credit` в `payment_methods` (provider: tbank_credit) и в `products.*`
   (payment_methods, tags_by_method, опц. fail_tags_by_method).
4. Развернуть на сервер (`git push` + `git pull` + `pip install` + `systemctl restart`).
5. Сначала протестировать на demo-ключах: `showcaseId = "demo-<твой_showcase>"`, URL = tinkoff.ru.

**10.06.2026 — КОНФИГ ЗАПОЛНЕН (промокод = "default"), ВСЁ ЕЩЁ НЕ ЗАДЕПЛОЕНО.**
`config.yaml` уже содержит блок `tbank_credit` (shop_id/showcase_id/api_password —
боевые значения от пользователя), способ `installment` переведён на
`provider: tbank_credit` (настроен ТАК ЖЕ как Долями — отдельная форма/webhook,
не через эквайринг), добавлен в `products.course_basic` (tags_by_method:
`paid_installment_basic`, fail_tags_by_method: `fail_installment_basic`).
`promo_code` по документации — `string(64), optional`, по умолчанию `"default"`
(используется единственный/основной кредитный продукт магазина) → выставлен
`promo_code: default`, `TBankCreditConfig.promo_code` в `app/config.py` теперь
имеет default `"default"`. `credit_threshold_kopecks` НЕ задан — авто-апгрейда по
сумме нет, `installment` доступен только как явный выбор способа оплаты. 82 теста
зелёные локально.
**10.06.2026 — ЗАДЕПЛОЕНО.** Коммит `ec2ea92` запушен и развёрнут на сервере.
Важный нюанс при деплое: git на сервере был на старом коммите `8407ba3` (12 коммитов
позади) с незакоммиченными staged-изменениями от прошлых pscp-деплоев — сделан
`git reset --hard origin/main` (проверено: рабочая копия была строгим подмножеством
origin/main, потерь нет). Также обнаружено: серверный `config.yaml` имел
`provider: dolyame` у способа `dolyami`, которого не было в локальной копии конфига —
добавлено в локальный конфиг перед заливкой (иначе сломали бы Долями). Старый
`config.yaml` сохранён как `config.yaml.bak-20260610-precredit`. 82 теста на сервере
зелёные, сервис перезапущен (`systemctl restart tbank-proxy`), `/health` → `{"status":"ok"}`,
в логах `Прокладка запущена. Товаров: 1`, ошибок нет.

**Осталось:**
- Создать в shalamov.io теги `paid_installment_basic` / `fail_installment_basic`
  (+ автофоллоу), как для Долями.
- После первого реального заказа уточнить `webhook_allowed_subnet` для
  Credit Broker (сейчас пусто — без IP-фильтра).

**10.06.2026 — ТРИ ПРОМОКОДА РАССРОЧКИ (3/6/10 мес), НЕ ЗАДЕПЛОЕНО.**
Владелец прислал реальные коды продуктов из ЛК (POS-кредитование → промокоды):
`installment_0_0_3_3,4_1,7` (3 мес), `installment_0_0_6_5,8_3` (6 мес),
`installment_0_0_10_8,9_4,6` (10 мес). Реализовано переопределение `promoCode` Credit
Broker per-способ оплаты:
- `app/config.py` — новое поле `PaymentMethodConfig.promo_code: str | None`
  (только для `provider: tbank_credit`, иначе ошибка валидации) + метод
  `AppConfig.promo_code_for_method()` (override способа, иначе `tbank_credit.promo_code`).
- `app/tbank_credit.py` — `TBankCreditClient.create(..., promo_code=...)` переопределяет
  `config.promo_code`.
- `app/main.py` — `init_credit_payment` передаёт `cfg.promo_code_for_method(method)`.
- `config.yaml` — старый единственный способ `installment` (provider: tbank_credit,
  promo_code: default) заменён на три: `installment_3` / `installment_6` / `installment_10`,
  каждый со своим `promo_code`. Все три ведут на те же теги `paid_installment_basic` /
  `fail_installment_basic` (доступ не зависит от срока рассрочки).
- `config.example.yaml` — задокументирован per-способ `promo_code` + пример
  `installment_3`/`installment_6`.
- Тесты: +3 (85 всего) — дефолтный promo_code, override per-способ, валидация
  (promo_code без provider=tbank_credit → ошибка конфига).
**10.06.2026 — ЗАДЕПЛОЕНО.** Коммит `f5e7bf6` запушен и развёрнут (git pull
fast-forward `ec2ea92..f5e7bf6`, 85/85 тестов на сервере, новый `config.yaml`
залит, бэкап `config.yaml.bak-20260610-installment-promo`, сервис перезапущен,
`/health` → ok). Бот должен слать `payment_method` = `installment_3` |
`installment_6` | `installment_10` (старое имя `installment` больше не
существует в конфиге).

**10.06.2026 — БЛОКЕР: Credit Broker отклоняет webhookURL по домену.**
Тестовый запрос `/init-payment` с `payment_method: installment_3` вернул
`payment_creation_failed`:
```
"validations":{"webhookURL":"Для ссылки возврата некорректно указан домен,
он должен совпадать с доменом вашего сайта"}
```
Прокладка шлёт `webhookURL = https://pay.sushi-house-39.ru/webhook/tbank_credit`
(`cfg.server.public_url + "/webhook/tbank_credit"`, `app/main.py:240`). Это
блокер на стороне ЛК, не код.

**Уточнение от поддержки Т-Банка:** динамический `webhookURL` (домен, отличный
от основного сайта) в теле `Create` — штатный способ (это и делает прокладка).

**11.06.2026 — испробовано и НЕ помогло:** пользователь заполнил в ЛК
(раздел «Т-Рассрочки» → «HTTP-уведомления о статусе заявки» → «Ссылка для
HTTP-нотификации (webhook URL)», ранее было пустое поле, для Долями там стоит
`https://dolyame.ru/white-list/`) значением
`https://pay.sushi-house-39.ru/webhook/tbank_credit`. Повторный тест —
**та же ошибка** `webhookURL: домен должен совпадать с доменом вашего сайта`.
Значит это поле — НЕ то, с которым сверяется домен `webhookURL` из Create.
«Домен сайта» настраивается где-то ещё (вероятно реквизиты магазина/ShopId
`4bd5a1e9-c05d-42bd-9c9e-ebbb266a67b2` — вкладка «Магазины»/«Реквизиты»/
«Основная информация» в ЛК), либо нужно повторно уточнить у поддержки точное
расположение поля для ShowcaseId `1c9fa012-13e4-4e55-b83b-e7668bb9124b`.

**11.06.2026 — доп. находка:** для `installment_3` (3 мес.) сумма заказа должна
быть **от 3108 до 517598 руб.** (ошибка `errors: ["Сумма заказа должна быть не
менее 3108 и не более 517598 руб. включительно"]` при сумме 99 руб). Это лимит
продукта в ЛК, не баг прокладки — но важно для тестирования (использовать
amount >= 310800 копеек) и для будущей логики (товар `course_basic` стоит 99 ₽
— рассрочка для него физически невозможна, актуально для дорогих товаров).

После исправления домена — повторить тестовый `/init-payment` с
`installment_3/6/10`, amount >= 310800 (curl-пример был выслан пользователю
в чате, добавить `"force": true` для повторных тестов с тем же contact_id).
Опционально: `tbank_credit.success_url/fail_url/return_url` в `config.yaml`
(сейчас пустые) — редиректы покупателя после успеха/отказа, уходят как
`successURL`/`failURL`/`returnURL` в тело Create.
