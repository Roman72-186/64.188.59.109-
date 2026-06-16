# NEXT-CHAT.md — хэндофф для нового чата

> Правило (см. CLAUDE.md): промт для нового чата и уточнения к нему живут здесь.
> Это самодостаточный контекст для продолжения работы с нуля. Обновлять при изменениях.
> Скопируй блок «Промт» целиком в новый чат Claude Code (рабочая папка — этот проект).

---

## Промт

```
Контекст: прокладка-платёжка (Python 3.10+ / FastAPI / SQLite) между провайдерами
(Т-Банк эквайринг card/sbp, Долями, T-Bank Credit Broker рассрочка) и shalamov.io.
Рабочая папка открыта (git, ветка main). Сервер 64.188.59.109, systemd-сервис
tbank-proxy.service, код в /opt/tbank_proxy, доступ — plink/pscp (см.
deploy/ssh-access.md, пароль в .env → SSH_PASSWORD; Windows-OpenSSH к серверу НЕ
цепляется, только PuTTY plink). venv на CPython 3.12.
Тесты: venv\Scripts\python -m pytest tests -q -p no:cacheprovider --import-mode=importlib
(103 passed; без --import-mode сборщик падает на кириллическом пути — это не баг кода).

ЧТО СДЕЛАНО В ПРОШЛОМ ЧАТЕ (15-16.06.2026), КОММИТ 9d78f07 — НЕ ЗАПУШЕН (только local):
1. ФИКС ФИСКАЛИЗАЦИИ ДОЛЯМИ. Симптом: оплата проходит, тег ставится, но commit падал
   с HTTP 400 BAD_REQUEST «Неверный формат запроса» (в логах ~10 повторов на заказ),
   заказы коммитились вручную в ЛК Долями. Причина (сверка со swagger.json): на commit
   схема CommitFiscalizationParams требует ВСЕ ТРИ флага (create_receipt_for_committed_items
   / _added_items / _returned_items), а CreateFiscalizationParams — пустой объект,
   поэтому create те же неполные params принимал (200), а commit отбивал (400).
   Прокладка слала только один флаг. Фикс: AppConfig.fiscalization_settings(operation)
   стал operation-aware (commit → 3 флага; create/refund → только type; disabled → None),
   3 вызова в app/dolyame.py передают свою операцию. Исправлен ошибочный комментарий в
   config.py («нет кассы» НЕ причина этого 400). ЗАДЕПЛОЕНО на сервер (pscp, бэкап в
   /opt/tbank_proxy/_backup/20260616-001928/), config.yaml → fiscalization: enabled,
   сервис перезапущен, /health ok.
2. CART-РЕЖИМ (отдельная предыдущая незакоммиченная фича, попала в тот же коммит по
   решению владельца): глобальные tags_by_method/fail_tags_by_method в AppConfig для
   заказов без серверного товара (платформа шлёт состав в cart + сумму в amount);
   build_receipt теперь принимает name/amount/tax вместо product_id. Затронуты
   config.py, main.py, schemas.py, database.py, tests + новый tests/test_cart_mode.py.

ГЛАВНАЯ ЗАДАЧА НОВОГО ЧАТА — закрыть верификацию фикса фискализации:
Фикс доказан по swagger, но swagger.required ≠ гарантия runtime-валидации, а это живой
платёжный поток. Нужен БОЕВОЙ тест: следующий реальный заказ по Долями должен дать в
логах /opt/tbank_proxy/logs/app.log строку «Долями POST /v1/orders/.../commit OK» +
статус committed ИМЕННО ОТ ПРОКЛАДКИ (НЕ ручной commit в ЛК, как было у order=888_... 
15.06). До этого момента фикс — «очень вероятно верный», но не подтверждён. Следить за
логами по grep 'commit|фискал|Долями'. Если commit снова 400 — это уже реальный вопрос
кассы/ОФД у мерчанта Долями (но по CLAUDE.md это должно проявляться как «нет письма»,
а не 400 — чистый commit 200 = ожидаемый исход).

ДОПОЛНИТЕЛЬНО (всплыло при разборе отправки чеков по всем провайдерам, см. «Уточнения»
16.06.2026):
- РАССРОЧКА/КРЕДИТ (Credit Broker) НЕ ШЛЁТ НИКАКИХ ФИСКАЛЬНЫХ ДАННЫХ. build_credit_item
  = только {name, price, quantity}, ни Receipt, ни fiscalization_settings, ни налогов.
  Если по рассрочке нужен 54-ФЗ чек — он сейчас не формируется. Решить с владельцем:
  касса мерчанта пробивает сама ИЛИ дорабатывать (проверить, поддерживает ли API
  forma.tbank.ru передачу данных чека — через Context7/доку, в build_credit_item их нет).
- FALLBACK-EMAIL чека карта/СБП: если бот в /init-payment не передаёт email/phone
  клиента, ВСЕ чеки уходят на fallback receipt.email из config.yaml
  (сейчас jwluwelirka@gmail.com), а не покупателю. Стоит проверить, что бот реально
  шлёт email клиента (по логам email не виден — не логируется).

ПЕРВЫЙ ШАГ: спросить владельца, нужно ли (а) запушить коммит 9d78f07 в origin/main,
(б) заняться верификацией фискализации по логам, (в) фискализацией рассрочки или
(г) проверкой fallback-email. Прочитать CLAUDE.md (разделы Долями / Credit Broker /
чек 54-ФЗ), app/config.py (fiscalization_settings, build_receipt, dolyame_item_receipt),
app/dolyame.py, app/main.py (init-payment ~428, init_credit_payment ~259, webhook
Долями ~617), app/tbank_credit.py (build_credit_item ~76), swagger.json.

ОСТАВШИЕСЯ TODO ИЗ ПРОШЛЫХ ХЭНДОФФОВ (не закрыты):
- Касса/ОФД на стороне Долями у мерчанта (вне кода) — нужна для реальной доставки письма.
- shalamov.io теги для shop2 (paid_card_shop_basic/fail_card_shop_basic) и
  installment (paid/fail_installment_*_basic) + автоворонки.
- Позитив Долями wait_for_commit → commit → тег боевым плательщиком с пройденным скорингом.
- webhook_allowed_subnet для tbank_credit (пусто; основной канал — поллинг).
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

**11.06.2026 — лимиты сумм для всех трёх сроков рассрочки (из реальных ошибок
forma.tbank.ru при тестовых `/init-payment` с маленькими суммами):**
- `installment_3` (3 мес.): **3108–517598 руб.**
- `installment_6` (6 мес.): **3187–530785 руб.**
- `installment_10` (10 мес.): **3294–548847 руб.**

Это лимиты продуктов в ЛК (POS-кредитование), не баг прокладки. Товар
`course_basic` (99 ₽) физически не проходит ни в один из них — рассрочка
актуальна для будущих дорогих товаров.

**11.06.2026 — E2E ПОДТВЕРЖДЁН для негативного статуса (`rejected`):**
Реальный `/init-payment` (`installment_10`, amount=350000 = 3500₽, contact
`3010325`, заказ `course_basic_3010325_9415aca8`) → Create OK → через ~1 мин
поллер получил `GET /info` → `статус rejected` → прокладка сразу назначила
**тег отказа `fail_installment_10_basic`** контакту `3010325`. Лог:
```
поллер: статус rejected order=...9415aca8 — отказ
⛔ Отказ оплаты — тег отказа назначен order=...9415aca8 тег=fail_installment_10_basic contact=3010325
```
Подтверждает, что новая схема per-term тегов (`fail_installment_3/6/10_basic`,
`paid_installment_3/6/10_basic`) корректно работает через поллинг end-to-end.
**Осталось проверить только позитив** (`signed` → `paid_installment_*_basic`) —
для этого нужна заявка, реально подписанная клиентом (банк одобрил скоринг).

**Осиротевшие 404-заявки в поллере:** список вырос (теперь это заказы, у
которых `Create` упал с ошибкой "Сумма заказа должна быть не менее..." — строка
в `payments` создаётся до вызова Create). Безвредно, самоустранится через
`CREDIT_POLL_MAX_AGE_SECONDS` (30 дней). Можно почистить вручную при желании.

**12.06.2026 — ФИСКАЛИЗАЦИЯ ДОЛЯМИ ЗАДЕПЛОЕНА (чек 54-ФЗ на почту клиента).**
Коммит `416c1a8` запушен и развёрнут (git pull + `systemctl restart tbank-proxy`,
`/health` ok, в логах чисто). На сервере `config.yaml` → `fiscalization: enabled`
(бэкап `config.yaml.bak-20260612-prefiscal`).
- Что делает код (`app/dolyame.py`, `app/config.py::dolyame_item_receipt` +
  `fiscalization_settings`, `app/main.py` create+commit): при `enabled` шлёт
  `fiscalization_settings: {type: enabled, params:{create_receipt_for_committed_items:true}}`
  + в каждую позицию `receipt` (tax/payment_method/payment_object/measurement_unit),
  поля из общего блока `receipt` (54-ФЗ), `measurement_unit="шт"`. Чек формируется
  на **commit** (двухфазность). Контакт (email/phone) уже шёл в `client_info`.
- **Осталось (НЕ сторона прокладки):** онлайн-касса/ОФД должна быть подключена
  у мерчанта **на стороне Долями** — код только передаёт данные чека. Финальная
  верификация = **боевой тестовый заказ** по рассрочке → проверить, пришёл ли чек
  на почту (95 тестов на моках доказывают только корректный JSON, не доставку письма).

**15.06.2026 — каждый способ оплаты = своя отдельная форма/ссылка (архитектура,
для справки).** Долями (`provider: dolyame`) и рассрочка Credit Broker
(`provider: tbank_credit`, `installment_3/6/10`) уже работают так: `/init-payment`
для каждого способа отдаёт **свой `pay_url`** на свою форму — Долями-only форма
(`partner.dolyame.ru`) и форма `forma.tbank.ru` соответственно (см. CLAUDE.md
разделы про Долями/Credit Broker, PRD.md:144 и §7.4). Card/sbp/installment через
обычный эквайринг используют общую форму Т-Банка на основном терминале.

**15.06.2026 — добавлен ТРЕТИЙ доп. терминал (своя форма card+СБП), config.yaml
ИЗМЕНЁН ЛОКАЛЬНО, НЕ ЗАДЕПЛОЕНО.** Реквизиты от Регины Габриелян (TerminalKey
`1780560752978` / пароль `ohyf!2KPwHc08&*X`) — это **отдельный магазин/терминал**
Т-Банка (механизм `tbank.extra_terminals`, уже реализован и описан в CLAUDE.md
"Мульти-терминал"), даёт card/sbp **свою форму**, отдельную от основного терминала
`1779970153075`. Изменения в `config.yaml`:
- `tbank.extra_terminals.shop2` — новые реквизиты терминала (`api_url`/`timeout`
  наследуются от основного).
- Новые `payment_methods`: `card_shop2` (label "Банковская карта", `terminal: shop2`),
  `sbp_shop2` (label "СБП", `terminal: shop2`, `extra_params.PayType: O`).
- `products.course_basic.payment_methods` + `tags_by_method` — добавлены
  `card_shop2 -> paid_card_shop2_basic`, `sbp_shop2 -> paid_sbp_shop2_basic`
  (имена тегов придуманы, можно переименовать до создания в shalamov.io).
- 95/95 тестов зелёные (`pytest -q --ignore=bnpl-proxy-kit`; директория
  `bnpl-proxy-kit/` — отдельный packaged-кит, даёт конфликт имён тестовых модулей
  при сборе без `--ignore`, не баг основного проекта).

**15.06.2026 — ДЕПЛОЙ ВЫПОЛНЕН И ПРОВЕРЕН E2E.** Серверный `config.yaml` обновлён
точечной правкой (бэкап `config.yaml.bak-20260615-shop2terminal`), 95/95 тестов
зелёные на сервере, `tbank-proxy.service` перезапущен, `/health` ok. Реальный
`/init-payment`:
- `card_shop2` → `{"status":"created","order_id":"course_basic_shop2_test_001_5263a399","pay_url":"https://pay.tbank.ru/tyBeIRRt"}`
- `sbp_shop2` → `{"status":"created","order_id":"course_basic_shop2_test_002_c190750c","pay_url":"https://pay.tbank.ru/dOVRUOLh"}`

Оба запроса прошли `Т-Банк Init OK` (payment_id `8680281462`/`8680281504`) —
Init реально ушёл через новый терминал `1780560752978`. Ссылки тестовые,
не открывались/не оплачивались.

ОСТАВШИЕСЯ ШАГИ (новый терминал shop2):
1. Создать в shalamov.io теги `paid_card_shop_basic` / `fail_card_shop_basic`
   (+ автоворонки), как делалось для installment/dolyami.
2. Бот должен научиться вызывать `/init-payment` с `payment_method: card_shop2`
   / `sbp_shop2` для этой формы — конфиг сам по себе трафик не направляет.
3. Открыть один из тестовых `pay_url` выше (или новый) и пройти оплату до конца,
   проверить, что `/webhook/tbank` придёт с `TerminalKey=1780560752978`, подпись
   пройдёт (`password_for_terminal_key` уже учитывает `extra_terminals` — код не
   менялся) и тег `paid_card_shop_basic` назначится (или `fail_card_shop_basic`
   при отказе — см. ниже).

**15.06.2026 — ТЕГИ ПЕРЕИМЕНОВАНЫ + ТЕГ ОТКАЗА ДЛЯ shop2 (код+конфиг готовы,
НЕ ЗАДЕПЛОЕНО).** Владелец задал имена: успех = `paid_card_shop_basic`, отказ =
`fail_card_shop_basic`, общие для `card_shop2` И `sbp_shop2` (одна форма shop2 —
один набор тегов; «больше никаких тегов не нужно по обычной оплате»).
- `config.yaml` `products.course_basic`:
  - `tags_by_method.card_shop2` и `.sbp_shop2` переименованы
    `paid_card_shop2_basic`/`paid_sbp_shop2_basic` → оба в `paid_card_shop_basic`.
  - `fail_tags_by_method.card_shop2` и `.sbp_shop2` = `fail_card_shop_basic` (новое).
- **Код-фикс `/webhook/tbank`** (`app/main.py`, ветка не-CONFIRMED): ранее при
  `NEGATIVE_TBANK_STATUSES` (`REJECTED`/`DEADLINE_EXPIRED`/`CANCELED`/`AUTH_FAIL`)
  вызывался только `database.mark_failed()`, тег отказа НЕ назначался (этот путь
  использует обычный эквайринг — `card`/`sbp`/`card_shop2`/`sbp_shop2`). Теперь
  добавлено (по аналогии с `/webhook/dolyame`): `cfg.fail_tag_for()` +
  `database.capture_fail_tag()` + `assign_failure_tag()`. Для `card`/`sbp` (без
  `fail_tags_by_method`) поведение НЕ меняется — `fail_tag_for` вернёт `None`.
- Тесты: +1 (96 всего) — `test_non_confirmed_status_assigns_fail_tag_when_configured`
  (новый, через `env_factory`+конфиг с `fail_tags_by_method.card`) и явная проверка
  в `test_non_confirmed_status_no_access`, что без `fail_tags_by_method` тег НЕ шлётся.
- **Деплой:** требуется git push кода (`app/main.py` + `tests/test_webhook.py` +
  CLAUDE.md) + точечная правка серверного `config.yaml` (переименование тегов +
  fail_tags_by_method, бэкап перед заменой) + restart + `/health`.

**16.06.2026 — ФИКС 400 НА COMMIT ФИСКАЛИЗАЦИИ ДОЛЯМИ + CART-РЕЖИМ (коммит `9d78f07`,
ЗАДЕПЛОЕНО, НЕ ЗАПУШЕНО).** По жалобе «оплата Долями проходит, тег ставится, но
отправка на фискальник падает 400 Неверный формат запроса» вытащены логи сервера
(plink, полный read-доступ дал владелец). Картина по `order=888_2853073_420e14a8`:
`create` с фискальным блоком → HTTP 200, `commit` с тем же блоком → HTTP 400
`BAD_REQUEST` (повтор ~10 раз 18:09–18:20), в 18:24 заказ стал committed РУЧНЫМ
commit в ЛК Долями (не прокладкой), в 18:28 владелец выставил `fiscalization: disabled`
+ рестарт 18:29 (обход). Сверка со `swagger.json`: `CommitFiscalizationParams` требует
ВСЕ 3 флага, `CreateFiscalizationParams` — пустой объект → асимметрия 200/400. Старый
комментарий в config.py («нет кассы») был ошибочным диагнозом.
- Фикс: `AppConfig.fiscalization_settings(operation)` operation-aware (commit → 3 флага
  True/False/False; create/refund → только `type`; disabled → None). `app/dolyame.py` —
  create/commit/refund передают операцию, блок не кладётся при None. Комментарий исправлен.
- Деплой: pscp `config.py`+`dolyame.py`, бэкап `/opt/tbank_proxy/_backup/20260616-001928/`,
  серверный `config.yaml` снова `fiscalization: enabled` (sed), import-check ok, рестарт,
  `/health` ok, на старте `Прокладка запущена. Товаров: 2`.
- Cart-режим (предыдущая незакоммиченная работа) включён в тот же коммит по решению
  владельца («всё одним коммитом»). 103/103 теста зелёные.
- НЕ вошли в коммит (осознанно, untracked): отдельный продаваемый `bnpl-proxy-kit/` +
  его гайды (`BNPL-GUIDE.md`, `DOLYAMI-INSTALLMENTS-INTEGRATION-GUIDE.{md,docx}`,
  `.codex_build_dolyami_docx.py`) — это самостоятельный проект, нельзя смешивать с
  реальным конфигом репо; и скриншоты/фото (`image.png`, `photo_*.jpg`, `Скриншот-*.png`).
- **ОСТАЛОСЬ:** боевой тест — `commit OK`+`committed` ОТ ПРОКЛАДКИ на реальном заказе
  (см. «Промт»). Коммит `9d78f07` не запушен в origin.

**16.06.2026 — РАЗБОР ОТПРАВКИ ЧЕКА ПО ВСЕМ ПРОВАЙДЕРАМ (справка, кода не трогали).**
- Карта/СБП (Т-Банк эквайринг): объект `Receipt` (54-ФЗ) в `Init`, однофазно;
  `build_receipt` → Taxation+Items[{Name,Price,Quantity,Amount,Tax,PaymentMethod,
  PaymentObject}]+Email/Phone; `Receipt` НЕ входит в Token; печатает касса/ОФД Т-Банка
  после CONFIRMED. В логах нет 309 → терминал принимает чек, касса подключена. Боевые
  CONFIRMED-платежи 15.06 (payment_id 8680769176 и др.) подтверждают работу.
- Долями: `fiscalization_settings` (верх) + позиционный `receipt` (tax/payment_method/
  payment_object/measurement_unit), двухфазно (receipt в create И commit, чек на commit),
  контакт в `client_info`, суммы в рублях.
- Рассрочка/кредит (Credit Broker): фискальных данных НЕ передаётся вообще
  (`build_credit_item` = name/price/quantity). → потенциальный пробел, см. «Промт».

**16.06.2026 (сессия 2) — ЗАКРЫТЫ 4 ТРЕКА ИЗ «ПРОМТА» (кода НЕ трогали, кроме push).**
Владелец выбрал все 4 задачи; результат по каждой:
1. **PUSH ВЫПОЛНЕН.** Коммит `9d78f07` (фикс фискализации + cart-режим) запушен в
   `origin/main` (`416c1a8..9d78f07`). Локаль и origin в синхроне.
   **ДЕПЛОЙ git-регуляризован:** на сервере git был на `416c1a8` с pscp-правками
   поверх (фикс залит файлами, не git). Сверено `git diff origin/main`: рабочее дерево
   отличалось ТОЛЬКО доками/тестами/`config.example.yaml` (app/*.py байт-в-байт = origin,
   т.е. рантайм уже верный), `config.yaml` в `.gitignore`, бэкапы untracked → потерь нет.
   `git reset --hard origin/main` → HEAD `9d78f07`, дерево чистое, `config.yaml` сохранён
   (`fiscalization: enabled`), 103/103 теста на сервере зелёные, `/health` ok. Рестарт
   НЕ потребовался (PID 206827 уже крутит идентичный код).
2. **ВЕРИФИКАЦИЯ ФИКСА ФИСКАЛИЗАЦИИ — фикс ПОДТВЕРЖДЁН ЖИВЫМ, runtime-доказательство
   ждёт реального заказа.** По логам сервера (plink): процесс uvicorn (PID 206827)
   стартовал **19:20:52 UTC 15.06** — ПОСЛЕ записи пофикшенных файлов (`config.py`
   mtime 19:19:52, `dolyame.py` 19:19:56, `config.yaml` 19:20:12 UTC) → загружен
   operation-aware код + `fiscalization: enabled`. Сервер в **UTC**; «00:19 16.06»
   из хэндоффа = локальное время прошлой машины (UTC+5), бэкап-каталог
   `_backup/20260616-001928` подтверждает offset. Баговый order=888_2853073_420e14a8
   (400×10, ручной commit в ЛК 18:24) был ДО рестарта. **С момента рестарта ни одного
   заказа по Долями не было** (в логах только поллинг Credit Broker) → эмпирическое
   `commit OK ОТ ПРОКЛАДКИ` пока не наблюдалось. Форсировать нельзя (нужен реальный
   плательщик с пройденным скорингом). Состояние: «живой и корректный, ждём первого
   реального заказа Долями».
3. **ФИСКАЛИЗАЦИЯ РАССРОЧКИ — НЕ ПРОБЕЛ.** Офиц. дока Т-Банка
   (tbank.ru/business/help/sales/loans/how-to-integrate/API/): по POS-рассрочке/кредиту
   чек 54-ФЗ выбивается **автоматически на стороне Т-Банка** («Выбивать чек через
   облачную кассу или сервис фискализации не нужно: система сделает это автоматически»).
   Значит `build_credit_item` без фискальных полей — корректно по дизайну, а не пробел.
   Детальная схема `forma.tbank.ru` за auth (403). **Решение владельца:** оставить как
   есть, перед любыми правками УТОЧНИТЬ У ПОДДЕРЖКИ Т-БАНКА, нужен ли чек от прокладки.
   (TODO — на стороне владельца, не код.)
4. **FALLBACK-EMAIL ЧЕКА (карта/СБП) — НЕ ПРОВЕРЯЕТСЯ ИЗ ЛОГОВ.** `email`/`phone` из
   `/init-payment` НЕ логируются; fallback `receipt.email`/`receipt.phone` в конфиге
   задан → warning «нет Email/Phone» физически не сработает (его условие — пустые И
   запрос, И fallback), 0 совпадений в логах ничего не доказывает. **Решение владельца:**
   САМ проверит почтовый ящик fallback (`receipt.email` в config.yaml): если там чеки
   многих РАЗНЫХ покупателей — бот НЕ шлёт их email и всё уходит на fallback. (TODO —
   на стороне владельца. Запасной план, если ящик неинформативен: добавить masked-лог
   НАЛИЧИЯ email/phone в `/init-payment` — но это код+деплой, не делалось.)

**16.06.2026 (сессия 3) — CloudKassir: касса для Долями (КОД ГОТОВ, НЕ ЗАДЕПЛОЕН/НЕ
ЗАКОММИЧЕН, ЖДЁТ БОЕВОЙ ВАЛИДАЦИИ).** Владелец: по карте/СБП чеки идут (касса на стороне
эквайринга Т-Банка), по Долями — нет. Подключаем CloudKassir (CloudPayments KKT,
`POST api.cloudpayments.ru/kkt/receipt`, Basic Public ID:API Secret) кассой для Долями.
Реквизиты в `config.yaml` (секреты): `public_id=pk_45fdd72cc3ddb6082e4f23eebeb3b`,
`api_secret` (дан), `inn=236000893906` (ИП Новосельцов К.Д.), СНО патент, ФФД 1.2,
fallback-email `yayest.community@yandex.ru`.
- **Область = ТОЛЬКО Долями.** Карта/СБП фискализируются эквайрингом, рассрочка/кредит —
  автоматически на стороне Т-Банка (сессия 2 п.3: «выбивать чек через облачную кассу не
  нужно, система сделает сама»). Поэтому `fiscalize_providers: [dolyame]` — это ФИНАЛЬНАЯ
  область, НЕ временная: добавлять `tbank_credit`/`tbank` НЕЛЬЗЯ (двойной чек).
- **Сделано (116 тестов, +13):** `app/cloudkassir.py` (клиент send_receipt+ping, маппинг
  строк 54-ФЗ→числовые коды KKT, сборка тела); `CloudKassirConfig`+`cloudkassir_methods()`
  в `config.py`; миграция БД `email`/`phone`/`receipt_sent_at` + **бэкфилл** существующих
  как «обработано» (НЕ фискализируем задним числом заказы до подключения кассы); в `main.py`
  `fiscalize_order` + **фоновая реконсиляция** `_fiscalize_pending` (развязана с webhook/тегом:
  пробивает чек по `paid_at IS NOT NULL AND receipt_sent_at IS NULL` каждые
  `cloudkassir.poll_interval_seconds`, идемпотентно — InvoiceId=order_id + X-Request-ID,
  `mark_receipt_sent` только при `Queued`); блоки `cloudkassir` в `config.yaml`/example.
  У Долями выставлено `fiscalization: disabled` (CloudKassir заменяет → прошлая задача
  «верификация commit-фискализации Долями» СНЯТА, чек теперь не от Долями).
- **СТАТУС = «собрано, ждёт боевой валидации», НЕ done.** Тесты проверяют форму JSON,
  которую мы САМИ задали (мок отдаёт `Success:true`), а НЕ которую примет касса — тот же
  класс риска, что 400 на commit Долями (required≠runtime). Нерешённые схемные развилки
  (если касса отбивает 4xx — туда; см. «PENDING LIVE VALIDATION» в `app/cloudkassir.py`):
  (1) патент без НДС — шлём `Vat: null`, касса может хотеть отсутствие ключа/код «без НДС»
  (самый вероятный отказ); (2) `Vat` vs `VatRate`; (3) `Quantity` числом vs строкой.
- **ШАГИ ВАЛИДАЦИИ (по порядку):** (а) задеплоить (git push+pull+restart, миграция на
  старте; серверный `config.yaml`: блок `cloudkassir` + `dolyame.fiscalization: disabled`);
  (б) `POST /test` (`CloudKassirClient.ping()`) боевыми кредами — проверяет ТОЛЬКО
  авторизацию/связь, без чека; (в) один боевой заказ Долями → в логах `🧾 Чек CloudKassir
  принят`, проверить письмо клиенту, прочитать РЕАЛЬНОЕ тело ответа `/kkt/receipt` (оно
  разрешит развилки Vat/Quantity). Только (в) переводит статус в «verified».
- Коммит ещё НЕ создан. Файлы изменены: `app/cloudkassir.py` (новый),
  `app/{config,database,main}.py`, `config.yaml`, `config.example.yaml`, `CLAUDE.md`,
  `tests/test_cloudkassir.py` (новый).
