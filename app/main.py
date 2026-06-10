"""FastAPI-приложение прокладки: /init-payment, /webhook/tbank, /health.

Сборка через create_app(...) — компоненты (config, db, клиенты) можно
инъектировать (тесты), иначе берутся из config.yaml.
"""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from .config import AppConfig, get_config
from .database import Database
from .dolyame import (
    STATUS_COMMITTED,
    STATUS_TERMINAL_NEGATIVE,
    STATUS_WAIT_FOR_COMMIT,
    DolyameClient,
    build_item,
    kopecks_to_rubles,
)
from .tbank_credit import (
    STATUS_SIGNED as CREDIT_STATUS_SIGNED,
    STATUS_TERMINAL_NEGATIVE as CREDIT_STATUS_NEGATIVE,
    TBankCreditClient,
    build_credit_item,
)
from .logging_setup import get_logger, setup_logging
from .schemas import InitPaymentRequest, InitPaymentResponse, InitStatus
from .shalamo import ShalamoClient
from .tbank import TBankClient, verify_webhook_token

log = get_logger()

# Назначение тега в webhook: 2 быстрые попытки (PRD §7.8). Суммарное время с
# учётом таймаута shalamo должно укладываться в таймаут webhook Т-Банка (~10с).
WEBHOOK_TAG_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 0.3

# Терминальные негативные статусы Т-Банка — переводим платёж в failed.
NEGATIVE_TBANK_STATUSES = {"REJECTED", "DEADLINE_EXPIRED", "CANCELED", "AUTH_FAIL"}


def _payment_variables(config: AppConfig, order: dict[str, Any]) -> dict[str, Any]:
    """Платёжные переменные контакта (PRD §7.7) + переменные товара."""
    rub = order["amount"] / 100
    variables: dict[str, Any] = {
        "payment_status": "paid",
        "payment_method": order["payment_method"],
        "payment_amount": int(rub) if float(rub).is_integer() else rub,
        "payment_order_id": order["order_id"],
        "payment_id": order.get("tbank_payment_id"),
    }
    product = config.get_product(order["product_id"])
    if product:
        variables.update(product.variables)
    return variables


def create_app(
    config: Optional[AppConfig] = None,
    db: Optional[Database] = None,
    tbank: Optional[TBankClient] = None,
    shalamo: Optional[ShalamoClient] = None,
    dolyame: Optional[DolyameClient] = None,
    tbank_credit: Optional[TBankCreditClient] = None,
) -> FastAPI:
    setup_logging()
    cfg = config or get_config()
    database = db or Database()
    shalamo_client = shalamo or ShalamoClient(cfg.shalamo)

    # Клиент прямого Долями: инъектированный (тесты) или из конфига, если блок задан.
    dolyame_client = dolyame
    if dolyame_client is None and cfg.dolyame is not None:
        dolyame_client = DolyameClient(cfg.dolyame)

    # Credit Broker: инъектированный (тесты) или из конфига, если блок задан.
    credit_client = tbank_credit
    if credit_client is None and cfg.tbank_credit is not None:
        credit_client = TBankCreditClient(cfg.tbank_credit)

    # Клиенты Т-Банка по terminal_key: основной + доп. магазины (напр. отдельный
    # магазин под Долями, чтобы форма показывала только его). Способ оплаты ->
    # терминал задаётся в config. Инъектированный клиент (тесты) используется как
    # единственный для всех способов.
    if tbank is not None:
        def client_for_method(method: str) -> Any:
            return tbank
    else:
        clients_by_terminal = {
            t.terminal_key: TBankClient(
                terminal_key=t.terminal_key,
                terminal_password=t.terminal_password,
                api_url=t.api_url,
                timeout_seconds=t.timeout_seconds,
            )
            for t in cfg.resolved_terminals().values()
        }

        def client_for_method(method: str) -> Any:
            return clients_by_terminal[cfg.terminal_key_for_method(method)]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.init_db()
        log.info("Прокладка запущена. Товаров: %d", len(cfg.products))
        yield

    app = FastAPI(title="TBank ↔ shalamov.io proxy", lifespan=lifespan)

    # ── выдача доступа (общий код для webhook и init-payment) ────────────────

    async def grant_access(order: dict[str, Any], attempts: int) -> bool:
        """Назначить контакту тег (+ переменные). Тег — гейт доступа: его успешная
        установка запускает автоворонку. Переменные отправляются перед тегом
        (best-effort), чтобы воронка стартовала уже с ними. Возвращает True при
        успешной установке тега."""
        contact_id = order["contact_id"]
        tag = order["tag_name"]
        variables = _payment_variables(cfg, order)
        last_error: Optional[str] = None

        for attempt in range(1, attempts + 1):
            var_res = await shalamo_client.set_variables(contact_id, variables)
            if not var_res.ok:
                log.warning(
                    "shalamo: переменные не установлены (попытка %d/%d) order=%s: %s",
                    attempt, attempts, order["order_id"], var_res.error,
                )
            tag_res = await shalamo_client.assign_tag(contact_id, tag)
            if tag_res.ok:
                database.mark_tag_assigned(order["order_id"])
                log.info(
                    "✅ Платёж подтверждён order=%s тег=%s contact=%s",
                    order["order_id"], tag, contact_id,
                )
                return True
            last_error = tag_res.error
            log.error(
                "❌ Назначение тега не удалось (попытка %d/%d) order=%s: %s",
                attempt, attempts, order["order_id"], last_error,
            )
            if attempt < attempts:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS)

        database.record_tag_error(order["order_id"], f"assign_tag failed: {last_error}")
        log.error(
            "❌ Доступ НЕ выдан order=%s — ТРЕБУЕТ РУЧНОГО РАЗБОРА", order["order_id"]
        )
        return False

    async def assign_failure_tag(
        order: dict[str, Any], tag: str, attempts: int
    ) -> bool:
        """Назначить контакту тег ОТКАЗА оплаты (триггер авторассылки «оплата не
        прошла»). В отличие от grant_access: переменные не шлём (для отказа не
        нужны), это не гейт доступа. Возвращает True при успешной установке.
        При неудаче shalamo — best-effort: пишем ошибку в лог, НЕ 503 (у отказа
        нет paid_at, значит нет страховки через /init-payment; вызывающий
        возвращает OK в любом случае)."""
        contact_id = order["contact_id"]
        last_error: Optional[str] = None
        for attempt in range(1, attempts + 1):
            tag_res = await shalamo_client.assign_tag(contact_id, tag)
            if tag_res.ok:
                database.mark_fail_tag_assigned(order["order_id"])
                log.info(
                    "⛔ Отказ оплаты — тег отказа назначен order=%s тег=%s contact=%s",
                    order["order_id"], tag, contact_id,
                )
                return True
            last_error = tag_res.error
            log.error(
                "❌ Тег отказа не назначен (попытка %d/%d) order=%s: %s",
                attempt, attempts, order["order_id"], last_error,
            )
            if attempt < attempts:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS)
        database.record_tag_error(
            order["order_id"], f"assign_fail_tag failed: {last_error}"
        )
        return False

    # ── создание платежа через прямой Долями ─────────────────────────────────

    async def init_dolyame_payment(
        order_id: str, product: Any, amount: int, req: InitPaymentRequest
    ) -> JSONResponse:
        """Создать заказ в прямом Partner API Долями и вернуть link как pay_url."""
        items = [build_item(product.name, amount)]
        client_info: dict[str, Any] = {}
        if req.phone:
            client_info["phone"] = req.phone
        if req.email:
            client_info["email"] = req.email
        notification_url = cfg.server.public_url.rstrip("/") + "/webhook/dolyame"
        res = await dolyame_client.create(
            order_id=order_id,
            amount_kopecks=amount,
            items=items,
            client_info=client_info or None,
            notification_url=notification_url,
        )
        if res.success and res.link:
            database.update_init_result(order_id, "", res.link)
            log.info("init-payment: получена ссылка Долями order=%s", order_id)
            return _resp(InitStatus.CREATED, 200, order_id=order_id, pay_url=res.link)

        database.mark_failed(order_id, f"Долями create: {res.error_code} {res.message}")
        log.error("init-payment: Долями не создал заказ order=%s", order_id)
        return _resp(
            InitStatus.PAYMENT_CREATION_FAILED, 502, order_id=order_id,
            message=res.message,
        )

    # ── создание платежа через Credit Broker ─────────────────────────────────

    async def init_credit_payment(
        order_id: str, product: Any, amount: int, req: InitPaymentRequest
    ) -> JSONResponse:
        """Создать заявку на кредит/рассрочку через T-Bank Credit Broker."""
        if credit_client is None:
            log.error("init-payment: Credit Broker не сконфигурирован order=%s", order_id)
            database.mark_failed(order_id, "Credit Broker не сконфигурирован")
            return _resp(InitStatus.PAYMENT_CREATION_FAILED, 502, order_id=order_id)

        items = [build_credit_item(product.name, amount)]
        customer_info: dict[str, Any] = {}
        if req.phone:
            customer_info["phone"] = req.phone
        if req.email:
            customer_info["email"] = req.email
        notification_url = cfg.server.public_url.rstrip("/") + "/webhook/tbank_credit"
        res = await credit_client.create(
            order_id=order_id,
            amount_kopecks=amount,
            items=items,
            customer_info=customer_info or None,
            webhook_url=notification_url,
        )
        if res.success and res.link:
            app_id = res.application_id or ""
            database.update_init_result(order_id, app_id, res.link)
            log.info("init-payment: получена ссылка Credit Broker order=%s", order_id)
            return _resp(InitStatus.CREATED, 200, order_id=order_id, pay_url=res.link)

        database.mark_failed(order_id, f"Credit Broker create: {res.error_code} {res.message}")
        log.error("init-payment: Credit Broker не создал заявку order=%s", order_id)
        return _resp(
            InitStatus.PAYMENT_CREATION_FAILED, 502, order_id=order_id,
            message=res.message,
        )

    # ── /init-payment ────────────────────────────────────────────────────────

    @app.post("/init-payment", response_model=InitPaymentResponse)
    async def init_payment(
        req: InitPaymentRequest,
        x_secret_token: Optional[str] = Header(default=None, alias="X-Secret-Token"),
    ) -> JSONResponse:
        # 1. секретный токен
        if not x_secret_token or not secrets.compare_digest(
            x_secret_token, cfg.server.secret_token
        ):
            log.warning("init-payment: неверный X-Secret-Token")
            return _resp(InitStatus.FORBIDDEN, 403)

        # 2. товар
        product = cfg.get_product(req.product_id)
        if product is None:
            log.warning("init-payment: неизвестный product_id=%s", req.product_id)
            return _resp(InitStatus.INVALID_PRODUCT, 400)

        # 3. способ оплаты
        if not cfg.is_method_allowed(req.product_id, req.payment_method):
            log.warning(
                "init-payment: способ '%s' недоступен товару '%s'",
                req.payment_method, req.product_id,
            )
            return _resp(InitStatus.INVALID_PAYMENT_METHOD, 400)

        # 3б. авто-апгрейд до кредита при превышении порога:
        #   если amount >= credit_threshold_kopecks И у товара есть tbank_credit-метод
        #   И запрошенный метод — НЕ tbank_credit (чтобы не зациклиться),
        #   то переключаем на кредитный способ оплаты.
        amount = req.amount
        effective_method = req.payment_method
        if (
            cfg.credit_threshold_kopecks is not None
            and amount >= cfg.credit_threshold_kopecks
            and cfg.provider_for_method(req.payment_method) != "tbank_credit"
        ):
            credit_method = cfg.credit_method_for(req.product_id)
            if credit_method:
                log.info(
                    "init-payment: сумма %d >= %d — апгрейд до кредита, метод %s->%s",
                    amount, cfg.credit_threshold_kopecks,
                    req.payment_method, credit_method,
                )
                effective_method = credit_method

        # 4. товар уже оплачен (PRD §7.3)
        paid = database.find_paid_order(req.contact_id, req.product_id)
        if paid is not None:
            if paid["tag_assigned_at"]:
                log.info(
                    "init-payment: уже оплачено и доступ выдан order=%s",
                    paid["order_id"],
                )
                return _resp(
                    InitStatus.ALREADY_PAID_ACCESS_GRANTED, 200,
                    order_id=paid["order_id"], pay_url=paid["pay_url"],
                )
            log.info(
                "init-payment: оплачено, но тег не назначен — пробуем назначить order=%s",
                paid["order_id"],
            )
            ok = await grant_access(paid, attempts=1)
            status = (
                InitStatus.ALREADY_PAID_ACCESS_GRANTED if ok
                else InitStatus.ALREADY_PAID_PENDING_ACCESS
            )
            return _resp(status, 200, order_id=paid["order_id"], pay_url=paid["pay_url"])

        # 5. активная неоплаченная ссылка (PRD §7.2) — пропускаем при force=True
        if not req.force:
            active = database.find_active_link(
                req.contact_id, req.product_id, effective_method
            )
            if active is not None:
                log.info("init-payment: возврат активной ссылки order=%s", active["order_id"])
                return _resp(
                    InitStatus.EXISTING_ACTIVE, 200,
                    order_id=active["order_id"], pay_url=active["pay_url"],
                )

        # 6. создание нового платежа
        order_id = f"{req.product_id}_{req.contact_id}_{secrets.token_hex(4)}"
        tag = cfg.tag_for(req.product_id, effective_method)
        database.create_payment(
            order_id, req.contact_id, req.product_id, effective_method,
            amount, tag,
        )
        log.info(
            "init-payment: создан платёж order=%s product=%s method=%s amount=%d",
            order_id, req.product_id, effective_method, amount,
        )

        # 6a. прямой Долями — отдельный провайдер (не эквайринг Т-Банка)
        if cfg.provider_for_method(effective_method) == "dolyame":
            return await init_dolyame_payment(order_id, product, amount, req)

        # 6б. Credit Broker — кредит/рассрочка
        if cfg.provider_for_method(effective_method) == "tbank_credit":
            return await init_credit_payment(order_id, product, amount, req)

        notification_url = cfg.server.public_url.rstrip("/") + "/webhook/tbank"
        receipt = cfg.build_receipt(
            req.product_id, email=req.email, phone=req.phone, amount=amount
        )
        if cfg.receipt and cfg.receipt.enabled and receipt is not None:
            if not receipt.get("Email") and not receipt.get("Phone"):
                log.warning(
                    "init-payment: чек включён, но нет Email/Phone order=%s — "
                    "Т-Банк отклонит Init (передай email/phone или задай fallback)",
                    order_id,
                )
        init = await client_for_method(req.payment_method).init_payment(
            order_id=order_id,
            amount=amount,
            description=product.description,
            notification_url=notification_url,
            extra_params=cfg.merged_extra_params(req.payment_method),
            receipt=receipt,
        )
        if init.success and init.pay_url:
            database.update_init_result(order_id, init.payment_id or "", init.pay_url)
            log.info("init-payment: получена ссылка оплаты order=%s", order_id)
            return _resp(
                InitStatus.CREATED, 200, order_id=order_id, pay_url=init.pay_url
            )

        database.mark_failed(order_id, f"Init: {init.error_code} {init.message}")
        log.error("init-payment: Т-Банк не создал платёж order=%s", order_id)
        return _resp(
            InitStatus.PAYMENT_CREATION_FAILED, 502, order_id=order_id,
            message=init.message,
        )

    # ── /webhook/tbank ─────────────────────────────────────────────────────

    @app.post("/webhook/tbank")
    async def webhook_tbank(request: Request) -> PlainTextResponse:
        try:
            payload = await request.json()
        except Exception:
            log.error("webhook: тело не разобралось как JSON")
            return PlainTextResponse("OK")

        # 1. подпись — пароль выбираем по TerminalKey (может быть доп. магазин)
        terminal_key = str(payload.get("TerminalKey"))
        password = cfg.password_for_terminal_key(terminal_key)
        if password is None:
            log.error("webhook: неизвестный TerminalKey=%s — не обрабатываем", terminal_key)
            return PlainTextResponse("OK")
        if not verify_webhook_token(payload, password):
            log.error("webhook: НЕВЕРНАЯ ПОДПИСЬ — не обрабатываем как оплату")
            return PlainTextResponse("OK")

        order_id = payload.get("OrderId")
        payment_id = payload.get("PaymentId")
        tbank_status = payload.get("Status")
        log.info(
            "webhook Т-Банк: order=%s payment_id=%s status=%s",
            order_id, payment_id, tbank_status,
        )

        # 2. найти заказ
        order = None
        if order_id:
            order = database.get_by_order_id(str(order_id))
        if order is None and payment_id:
            order = database.get_by_tbank_payment_id(str(payment_id))
        if order is None:
            log.error("webhook: заказ не найден order=%s payment_id=%s", order_id, payment_id)
            return PlainTextResponse("OK")

        oid = order["order_id"]
        database.set_tbank_status(oid, str(tbank_status))

        # 3. не CONFIRMED — доступ не выдаём
        if tbank_status != "CONFIRMED":
            if tbank_status in NEGATIVE_TBANK_STATUSES:
                database.mark_failed(oid, f"Т-Банк статус {tbank_status}")
            log.info("webhook: статус %s — доступ не выдаём order=%s", tbank_status, oid)
            return PlainTextResponse("OK")

        # 4. CONFIRMED
        # 4a. сверка суммы; при расхождении — доппроверка GetState (PRD §7.6)
        wh_amount = payload.get("Amount")
        if wh_amount != order["amount"]:
            log.warning(
                "webhook: сумма webhook=%s != заказ=%s order=%s — проверяем GetState",
                wh_amount, order["amount"], oid,
            )
            state = await client_for_method(order["payment_method"]).get_state(
                str(payment_id)
            )
            if not (
                state.success
                and state.status == "CONFIRMED"
                and state.amount == order["amount"]
            ):
                log.error(
                    "webhook: сумма не подтверждена GetState order=%s — доступ не выдаём", oid
                )
                return PlainTextResponse("OK")
            log.info("webhook: сумма подтверждена через GetState order=%s", oid)

        # 4b. идемпотентность: тег уже назначен
        if order["tag_assigned_at"]:
            log.info("webhook: повторный — тег уже назначен order=%s, пропускаем", oid)
            return PlainTextResponse("OK")

        # фиксируем факт оплаты банком
        database.mark_paid(oid, str(tbank_status))

        # 4c. атомарный захват (защита от дублей)
        if not database.atomic_capture(oid):
            fresh = database.get_by_order_id(oid)
            if fresh and fresh["tag_assigned_at"]:
                log.info("webhook: параллельный — тег уже назначен order=%s", oid)
            else:
                log.info("webhook: order=%s обрабатывается параллельно, пропускаем", oid)
            return PlainTextResponse("OK")

        # 4d. назначение тега, 2 попытки
        ok = await grant_access(order, attempts=WEBHOOK_TAG_ATTEMPTS)
        if ok:
            return PlainTextResponse("OK")

        # 4f. обе попытки неуспешны — НЕ возвращаем OK, отдаём 503 (PRD §7.8)
        log.error("webhook: возвращаем 503 order=%s — Т-Банк повторит webhook", oid)
        return PlainTextResponse("Service Unavailable", status_code=503)

    # ── /webhook/dolyame ───────────────────────────────────────────────────

    @app.post("/webhook/dolyame")
    async def webhook_dolyame(request: Request) -> PlainTextResponse:
        if dolyame_client is None or cfg.dolyame is None:
            log.error("webhook Долями: провайдер не сконфигурирован — игнорируем")
            return PlainTextResponse("OK")

        # 1. источник webhook — только подсеть Долями (за nginx: X-Forwarded-For)
        client_ip = _client_ip(request)
        if not _ip_in_subnet(client_ip, cfg.dolyame.webhook_allowed_subnet):
            log.error("webhook Долями: запрещённый IP %s — игнорируем", client_ip)
            return PlainTextResponse("Forbidden", status_code=403)

        # 2. тело: интересует только id заказа — статус/сумму берём из /info
        try:
            payload = await request.json()
        except Exception:
            log.error("webhook Долями: тело не разобралось как JSON")
            return PlainTextResponse("OK")
        order_id = payload.get("id")
        log.info(
            "webhook Долями: order=%s status=%s (тело не доверяем, проверяем /info)",
            order_id, payload.get("status"),
        )

        order = database.get_by_order_id(str(order_id)) if order_id else None
        if order is None:
            log.error("webhook Долями: заказ не найден order=%s", order_id)
            return PlainTextResponse("OK")
        oid = order["order_id"]

        # 3. источник истины — GET /info (webhook Долями не подписан, как у Т-Банка)
        info = await dolyame_client.info(oid)
        if not info.success:
            log.error("webhook Долями: /info недоступен order=%s — 503, повтор", oid)
            return PlainTextResponse("Service Unavailable", status_code=503)
        database.set_tbank_status(oid, str(info.status))

        # 4. терминальный негатив — доступ не выдаём; назначаем тег ОТКАЗА
        # (если задан для способа) как триггер авторассылки «оплата не прошла».
        if info.status in STATUS_TERMINAL_NEGATIVE:
            database.mark_failed(oid, f"Долями статус {info.status}")
            log.info("webhook Долями: статус %s order=%s — отказ", info.status, oid)
            fail_tag = cfg.fail_tag_for(order["product_id"], order["payment_method"])
            if fail_tag and database.capture_fail_tag(oid):
                await assign_failure_tag(order, fail_tag, attempts=WEBHOOK_TAG_ATTEMPTS)
            # best-effort: отказ не блокирует ответ, всегда OK (нет paid_at → нет
            # страховки через /init-payment, Долями может не повторить webhook).
            return PlainTextResponse("OK")

        # 5. оплата ещё не произошла (new/approved) — ждём
        if info.status not in STATUS_WAIT_FOR_COMMIT and info.status not in STATUS_COMMITTED:
            log.info("webhook Долями: статус %s order=%s — доступ не выдаём", info.status, oid)
            return PlainTextResponse("OK")

        # 6. сверка суммы (рубли, точно через Decimal) — ДО commit, чтобы не
        # захватывать холд с неверной суммой.
        expected = kopecks_to_rubles(order["amount"])
        if info.amount is not None and info.amount != expected:
            log.error(
                "webhook Долями: сумма info=%s != заказ=%s order=%s — доступ не выдаём",
                info.amount, expected, oid,
            )
            return PlainTextResponse("OK")

        # 7. двухфазность: на wait_for_commit захватываем холд (commit), затем доступ
        product = cfg.get_product(order["product_id"])
        item_name = product.name if product else order["product_id"]
        if info.status in STATUS_WAIT_FOR_COMMIT and cfg.dolyame.commit_on_webhook:
            items = [build_item(item_name, order["amount"])]
            commit = await dolyame_client.commit(oid, order["amount"], items)
            if not commit.success:
                log.error("webhook Долями: commit не удался order=%s — 503", oid)
                return PlainTextResponse("Service Unavailable", status_code=503)
            log.info("webhook Долями: commit OK order=%s", oid)
        # commit_on_webhook=False — выдаём доступ уже на холде (wait_for_commit)

        # 8. идемпотентность: тег уже назначен
        if order["tag_assigned_at"]:
            log.info("webhook Долями: повторный — тег уже назначен order=%s", oid)
            return PlainTextResponse("OK")

        # фиксируем факт оплаты (после commit) — страхует PRD §7.3, даже если
        # Долями не повторит webhook: следующий /init-payment до-назначит тег.
        database.mark_paid(oid, str(info.status))

        if not database.atomic_capture(oid):
            fresh = database.get_by_order_id(oid)
            if fresh and fresh["tag_assigned_at"]:
                log.info("webhook Долями: параллельный — тег уже назначен order=%s", oid)
            else:
                log.info("webhook Долями: order=%s обрабатывается параллельно", oid)
            return PlainTextResponse("OK")

        # 8. назначение тега. 1 попытка: бюджет webhook уже съеден info+commit (mTLS).
        ok = await grant_access(order, attempts=1)
        if ok:
            return PlainTextResponse("OK")
        log.error("webhook Долями: тег не назначен order=%s — 503, повтор", oid)
        return PlainTextResponse("Service Unavailable", status_code=503)

    # ── /webhook/tbank_credit ─────────────────────────────────────────────────

    @app.post("/webhook/tbank_credit")
    async def webhook_tbank_credit(request: Request) -> PlainTextResponse:
        if credit_client is None or cfg.tbank_credit is None:
            log.error("webhook Credit Broker: провайдер не сконфигурирован — игнорируем")
            return PlainTextResponse("OK")

        # 1. IP-allowlist (опционально; если подсеть не задана — пропускаем проверку)
        if cfg.tbank_credit.webhook_allowed_subnet:
            client_ip = _client_ip(request)
            if not _ip_in_subnet(client_ip, cfg.tbank_credit.webhook_allowed_subnet):
                log.error(
                    "webhook Credit Broker: запрещённый IP %s — игнорируем", client_ip
                )
                return PlainTextResponse("Forbidden", status_code=403)
        else:
            log.debug("webhook Credit Broker: webhook_allowed_subnet не задан, IP не проверяем")

        # 2. тело — берём orderNumber (наш order_id) и application_id
        try:
            payload = await request.json()
        except Exception:
            log.error("webhook Credit Broker: тело не разобралось как JSON")
            return PlainTextResponse("OK")

        order_id = payload.get("orderNumber")
        application_id = payload.get("id")
        log.info(
            "webhook Credit Broker: order=%s app_id=%s status=%s (проверяем /info)",
            order_id, application_id, payload.get("status"),
        )

        # Попытка найти заказ: сначала по orderNumber, затем по application_id (tbank_payment_id)
        order = None
        if order_id:
            order = database.get_by_order_id(str(order_id))
        if order is None and application_id:
            order = database.get_by_tbank_payment_id(str(application_id))
        if order is None:
            log.error(
                "webhook Credit Broker: заказ не найден order=%s app_id=%s",
                order_id, application_id,
            )
            return PlainTextResponse("OK")
        oid = order["order_id"]

        # 3. источник истины — GET /info (webhook не подписан)
        info = await credit_client.info(oid)
        if not info.success:
            log.error("webhook Credit Broker: /info недоступен order=%s — 503, повтор", oid)
            return PlainTextResponse("Service Unavailable", status_code=503)
        database.set_tbank_status(oid, str(info.status))

        # 4. терминальный негатив — доступ не выдаём; назначаем тег отказа (если задан)
        if info.status in CREDIT_STATUS_NEGATIVE:
            database.mark_failed(oid, f"Credit Broker статус {info.status}")
            log.info(
                "webhook Credit Broker: статус %s order=%s — отказ", info.status, oid
            )
            fail_tag = cfg.fail_tag_for(order["product_id"], order["payment_method"])
            if fail_tag and database.capture_fail_tag(oid):
                await assign_failure_tag(order, fail_tag, attempts=WEBHOOK_TAG_ATTEMPTS)
            return PlainTextResponse("OK")

        # 5. заявка не подписана — ждём
        if info.status not in CREDIT_STATUS_SIGNED:
            log.info(
                "webhook Credit Broker: статус %s order=%s — доступ не выдаём",
                info.status, oid,
            )
            return PlainTextResponse("OK")

        # 6. signed — при ручном режиме вызываем Commit, затем выдаём доступ
        if cfg.tbank_credit.commit_on_webhook:
            commit = await credit_client.commit(oid)
            if not commit.success:
                log.error("webhook Credit Broker: commit не удался order=%s — 503", oid)
                return PlainTextResponse("Service Unavailable", status_code=503)
            log.info("webhook Credit Broker: commit OK order=%s", oid)

        # 7. идемпотентность: тег уже назначен
        if order["tag_assigned_at"]:
            log.info(
                "webhook Credit Broker: повторный — тег уже назначен order=%s", oid
            )
            return PlainTextResponse("OK")

        database.mark_paid(oid, str(info.status))

        if not database.atomic_capture(oid):
            fresh = database.get_by_order_id(oid)
            if fresh and fresh["tag_assigned_at"]:
                log.info(
                    "webhook Credit Broker: параллельный — тег уже назначен order=%s", oid
                )
            else:
                log.info(
                    "webhook Credit Broker: order=%s обрабатывается параллельно", oid
                )
            return PlainTextResponse("OK")

        # 8. назначение тега (1 попытка: бюджет webhook съеден info+commit)
        ok = await grant_access(order, attempts=1)
        if ok:
            return PlainTextResponse("OK")
        log.error("webhook Credit Broker: тег не назначен order=%s — 503, повтор", oid)
        return PlainTextResponse("Service Unavailable", status_code=503)

    # ── /health ──────────────────────────────────────────────────────────────

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


# Запуск: uvicorn app.main:create_app --factory
# (фабрика вызывается на старте; неверный config.yaml падает с понятной ошибкой).


def _client_ip(request: Request) -> str:
    """Реальный IP клиента за nginx. Берём X-Real-IP (nginx ставит $remote_addr —
    клиент его подменить не может). Запасной вариант — ПОСЛЕДНИЙ хоп X-Forwarded-For
    (nginx добавляет реальный адрес в конец через $proxy_add_x_forwarded_for; первый
    элемент задаёт клиент и доверять ему нельзя). Иначе — peer соединения."""
    real = request.headers.get("X-Real-IP")
    if real:
        return real.strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else ""


def _ip_in_subnet(ip: str, subnet: str) -> bool:
    """Принадлежит ли IP подсети (CIDR). По множеству, не строковому префиксу."""
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return False


def _resp(
    status: InitStatus,
    http_code: int,
    order_id: Optional[str] = None,
    pay_url: Optional[str] = None,
    message: Optional[str] = None,
) -> JSONResponse:
    payload = InitPaymentResponse(
        status=status, order_id=order_id, pay_url=pay_url, message=message
    )
    return JSONResponse(status_code=http_code, content=payload.model_dump())
