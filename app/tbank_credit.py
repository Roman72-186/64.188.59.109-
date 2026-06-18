"""Клиент T-Bank Credit Broker API (forma.tbank.ru) — кредит/рассрочка.

Документация: https://forma.tbank.ru/docs/online/setup-types/api/description-api

Базовый URL: https://forma.tbank.ru/api/partners/v2/orders
Аутентификация:
  • Create — без Basic Auth; shopId + showcaseId передаются в теле.
  • Commit / Cancel / Info — Basic Auth (showcase_id:api_password, Base64).

Суммы: в РУБЛЯХ (float). Конвертация из копеек — через Decimal (без float-погрешности).

Статусы заявки:
  new → inprogress → approved → signed → canceled | rejected
  • signed = клиент подписал документы, деньги захолдированы (или перечислены при авто-commit).
  • Commit подтверждает актуальность заказа и инициирует перечисление партнёру (14 дней).
  • canceled / rejected = терминальный негатив.

Webhook: webhookURL в Create НЕ передаётся (см. process_credit_status в app/main.py) —
Т-Банк отклоняет Create, если домен webhookURL не совпадает с доменом витрины (а у
витрин разных клиентов он свой, прокладка на это не влияет). /webhook/tbank_credit
остаётся рабочим, если домены совпадут (тело не подписано — источник истины GET /info,
аутентичность отправителя по IP). Основной канал — фоновый опрос GET /info
(tbank_credit.poll_interval_seconds), не зависящий от домена.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

import httpx

from .config import TBankCreditConfig
from .logging_setup import get_logger

log = get_logger()

# Терминальный успех (клиент подписал документы).
STATUS_SIGNED = {"signed"}
# Терминальный негатив: доступ НЕ выдаём ни при одном из этих статусов.
STATUS_TERMINAL_NEGATIVE = {"canceled", "rejected"}
# Из терминального негатива тег отказа («оплата не прошла») ставим ТОЛЬКО при
# реальном отклонении заявки банком. `canceled` = заявка брошена/протухла
# (клиент не довёл оформление) — это НЕ неудачная оплата, рассылку «оплата не
# прошла» не запускаем (иначе спамим тех, кто открыл форму и ушёл). Та же
# логика, что у Долями (см. dolyame.STATUS_FAIL_TAG).
STATUS_FAIL_TAG = {"rejected"}


def kopecks_to_rubles(kopecks: int) -> Decimal:
    """Копейки -> рубли (Decimal, ровно 2 знака)."""
    return (Decimal(int(kopecks)) / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def rubles_to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None


@dataclass
class CreditResult:
    """Унифицированный результат вызова Credit Broker API."""

    success: bool
    status: Optional[str] = None          # статус заявки (signed / canceled / …)
    application_id: Optional[str] = None  # внутренний ID заявки Credit Broker
    order_number: Optional[str] = None    # наш order_id (orderNumber в API)
    link: Optional[str] = None            # ссылка на форму (create)
    amount: Optional[Decimal] = None      # сумма в рублях из ответа
    error_code: Optional[str] = None
    message: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


def build_credit_item(name: str, amount_kopecks: int, quantity: int = 1) -> dict[str, Any]:
    """Позиция заказа Credit Broker (суммы в рублях). Инвариант: sum = Σ(price·quantity)."""
    per_unit = (
        kopecks_to_rubles(amount_kopecks) / Decimal(quantity)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"name": name[:128], "price": float(per_unit), "quantity": quantity}


class TBankCreditClient:
    """Клиент T-Bank Credit Broker (forma.tbank.ru/api/partners/v2)."""

    def __init__(self, config: TBankCreditConfig) -> None:
        self.config = config
        self.base_url = config.api_url.rstrip("/")
        self._auth = httpx.BasicAuth(config.showcase_id, config.api_password)

    # ── транспорт ─────────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]] = None,
        use_auth: bool = True,
    ) -> CreditResult:
        url = f"{self.base_url}{path}"
        auth = self._auth if use_auth else None
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                resp = await client.request(method, url, json=body, auth=auth)
        except Exception as e:
            log.error("Credit Broker %s %s: ошибка сети: %s", method, path, e)
            return CreditResult(success=False, message=str(e))

        if 200 <= resp.status_code < 300:
            data = _safe_json(resp)
            log.info("Credit Broker %s %s OK (HTTP %s)", method, path, resp.status_code)
            return CreditResult(
                success=True,
                status=data.get("status"),
                application_id=str(data["id"]) if data.get("id") else None,
                order_number=data.get("orderNumber"),
                link=data.get("link"),
                amount=rubles_to_decimal(data.get("sum") or data.get("amount")),
                raw=data,
            )

        data = _safe_json(resp)
        log.error(
            "Credit Broker %s %s отказ: HTTP %s %s",
            method, path, resp.status_code, data,
        )
        return CreditResult(
            success=False,
            error_code=str(resp.status_code),
            message=data.get("message") or resp.text[:300],
            raw=data,
        )

    # ── методы API ────────────────────────────────────────────────────────────

    async def create(
        self,
        order_id: str,
        amount_kopecks: int,
        items: list[dict[str, Any]],
        customer_info: Optional[dict[str, Any]] = None,
        webhook_url: Optional[str] = None,
        promo_code: Optional[str] = None,
    ) -> CreditResult:
        """POST /orders/create — создать заявку на кредит/рассрочку.

        Без Basic Auth: shopId + showcaseId идут в теле. orderNumber = наш order_id,
        передаётся в webhook и нужен для Commit/Cancel/Info. promo_code переопределяет
        config.promo_code (напр. разные сроки рассрочки — разные продукты в ЛК)."""
        body: dict[str, Any] = {
            "shopId": self.config.shop_id,
            "showcaseId": self.config.showcase_id,
            "sum": float(kopecks_to_rubles(amount_kopecks)),
            "items": items,
            "orderNumber": order_id,
            "promoCode": promo_code or self.config.promo_code,
        }
        if webhook_url:
            body["webhookURL"] = webhook_url
        if self.config.success_url:
            body["successURL"] = self.config.success_url
        if self.config.fail_url:
            body["failURL"] = self.config.fail_url
        if self.config.return_url:
            body["returnURL"] = self.config.return_url
        if customer_info:
            body["values"] = {"contact": customer_info}

        res = await self._request("POST", "/orders/create", body, use_auth=False)
        res.order_number = order_id
        return res

    async def commit(self, order_number: str) -> CreditResult:
        """POST /orders/{orderNumber}/commit — подтвердить заявку (ручной режим)."""
        res = await self._request("POST", f"/orders/{order_number}/commit")
        res.order_number = order_number
        return res

    async def cancel(self, order_number: str) -> CreditResult:
        """POST /orders/{orderNumber}/cancel — отменить заявку."""
        res = await self._request("POST", f"/orders/{order_number}/cancel")
        res.order_number = order_number
        return res

    async def info(self, order_number: str) -> CreditResult:
        """GET /orders/{orderNumber}/info — актуальный статус заявки."""
        res = await self._request("GET", f"/orders/{order_number}/info")
        res.order_number = order_number
        return res


# ── вспомогательное ───────────────────────────────────────────────────────────

def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"raw": data}
    except Exception:
        return {}
