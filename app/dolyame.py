"""Клиент прямого Partner API Долями (partner.dolyame.ru) — отдельно от эквайринга Т-Банка.

Зачем отдельно: нужна форма «только Долями», а в эквайринге Т-Банка обязательные
способы оплаты из формы не убираются. Прямой API даёт нативную Долями-only форму.

Особенности API (см. swagger.json в корне):
  • Авторизация: HTTP Basic (login/password) + ОБЯЗАТЕЛЬНЫЙ клиентский сертификат mTLS
    (cert=(crt, key)) на КАЖДЫЙ запрос. Без сертификата не пройдёт даже TLS-handshake.
  • Заголовок X-Correlation-ID = UUID v4 на каждый вызов (для трассировки/идемпотентности).
  • Суммы в API — В РУБЛЯХ (number, 99.00). В проекте суммы хранятся В КОПЕЙКАХ —
    конвертация здесь, точно, через Decimal (никаких float-делений).
  • Двухфазность: клиент платит первые 25% → статус wait_for_commit (деньги захолдированы)
    → партнёр ОБЯЗАН вызвать commit → committed/completed. Tag-гейт ставится после commit.
  • 429 Too Many Requests → заголовок X-Retry-After (секунды); лимит >10 req/s. Уважаем.
  • Единый формат ошибки: {code, errorDetailCode, message, correlationId, details}.

Эндпоинты ({orderId} = НАШ order.id, переданный в create):
  POST /v1/orders/create            → OrderInfo (поле link = pay_url)
  GET  /v1/orders/{orderId}/info    → OrderInfo
  POST /v1/orders/{orderId}/commit  → захват холда
  POST /v1/orders/{orderId}/cancel  → отмена
  POST /v1/orders/{orderId}/refund  → возврат (вне scope прокладки, метод задел на будущее)
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

import httpx

from .config import DolyameConfig
from .logging_setup import get_logger

log = get_logger()

# Статусы заказа Долями (OrderInfo.status), на которые опирается логика выдачи доступа.
STATUS_WAIT_FOR_COMMIT = {"wait_for_commit", "waiting_for_commit"}
STATUS_COMMITTED = {"committed", "completed"}
STATUS_TERMINAL_NEGATIVE = {"rejected", "canceled"}

# Сколько раз повторить запрос при 429 (с уважением к X-Retry-After).
MAX_RETRY_429 = 2


def kopecks_to_rubles(kopecks: int) -> Decimal:
    """Копейки (int) -> рубли (Decimal, ровно 2 знака). Без float-погрешности."""
    return (Decimal(int(kopecks)) / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def rubles_to_decimal(value: Any) -> Optional[Decimal]:
    """Значение суммы из ответа API (рубли) -> Decimal(2). None, если не число."""
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None


@dataclass
class DolyameResult:
    """Унифицированный результат вызова Долями (для create/info/commit/cancel/refund)."""

    success: bool
    status: Optional[str] = None          # OrderInfo.status
    amount: Optional[Decimal] = None      # рубли
    residual_amount: Optional[Decimal] = None
    link: Optional[str] = None            # pay_url (create/info)
    order_id: Optional[str] = None
    end_cooling_period: Optional[str] = None
    error_code: Optional[str] = None      # HTTP-код или business code (code/errorDetailCode)
    message: Optional[str] = None
    correlation_id: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


def _order_info_result(correlation_id: str, data: dict[str, Any]) -> DolyameResult:
    """Разобрать тело OrderInfo (общее для create/info/commit/cancel) в DolyameResult."""
    return DolyameResult(
        success=True,
        status=data.get("status"),
        amount=rubles_to_decimal(data.get("amount")),
        residual_amount=rubles_to_decimal(data.get("residual_amount")),
        link=data.get("link"),
        end_cooling_period=data.get("end_cooling_period"),
        correlation_id=correlation_id,
        raw=data,
    )


class DolyameClient:
    def __init__(self, config: DolyameConfig) -> None:
        self.config = config
        self.base_url = config.base_url.rstrip("/")

    # ── транспорт ────────────────────────────────────────────────────────────

    @property
    def _cert(self) -> Optional[tuple[str, str]]:
        """Клиентский сертификат mTLS для httpx. None, если пути не заданы
        (каркас работает «вхолостую» на моках; живой вызов без сертификата
        упадёт на TLS-handshake — это ожидаемо до получения сертификата)."""
        if self.config.cert_path and self.config.key_path:
            return (self.config.cert_path, self.config.key_path)
        return None

    async def _request(
        self, method: str, path: str, body: Optional[dict[str, Any]] = None
    ) -> DolyameResult:
        """Один вызов API с Basic + mTLS + X-Correlation-ID и retry на 429.

        Возвращает DolyameResult: success=True при 2xx (тело — OrderInfo),
        иначе success=False с разобранным кодом ошибки."""
        url = f"{self.base_url}{path}"
        auth = httpx.BasicAuth(self.config.login, self.config.password)

        last: DolyameResult = DolyameResult(success=False, message="запрос не выполнен")
        for attempt in range(1, MAX_RETRY_429 + 2):
            correlation_id = str(uuid.uuid4())
            headers = {"X-Correlation-ID": correlation_id}
            try:
                async with httpx.AsyncClient(
                    timeout=self.config.timeout_seconds,
                    cert=self._cert,
                    auth=auth,
                ) as client:
                    resp = await client.request(
                        method, url, headers=headers, json=body
                    )
            except Exception as e:  # сеть/таймаут/TLS
                log.error("Долями %s %s: ошибка запроса: %s", method, path, e)
                return DolyameResult(
                    success=False, correlation_id=correlation_id, message=str(e)
                )

            if 200 <= resp.status_code < 300:
                data = _safe_json(resp)
                log.info(
                    "Долями %s %s OK (HTTP %s) cid=%s",
                    method, path, resp.status_code, correlation_id,
                )
                return _order_info_result(correlation_id, data)

            # 429 — уважаем X-Retry-After и повторяем
            if resp.status_code == 429 and attempt <= MAX_RETRY_429:
                retry_after = _parse_retry_after(resp)
                log.warning(
                    "Долями %s %s: 429, повтор через %.1fs (попытка %d) cid=%s",
                    method, path, retry_after, attempt, correlation_id,
                )
                await asyncio.sleep(retry_after)
                continue

            last = _error_result(correlation_id, resp)
            log.error(
                "Долями %s %s отказ: HTTP %s code=%s msg=%s cid=%s",
                method, path, resp.status_code, last.error_code, last.message,
                correlation_id,
            )
            return last

        return last

    # ── методы API ───────────────────────────────────────────────────────────

    async def create(
        self,
        order_id: str,
        amount_kopecks: int,
        items: list[dict[str, Any]],
        client_info: Optional[dict[str, Any]] = None,
        notification_url: Optional[str] = None,
        success_url: Optional[str] = None,
        fail_url: Optional[str] = None,
    ) -> DolyameResult:
        """POST /v1/orders/create. amount_kopecks — в копейках (конвертируем в рубли).

        Инвариант Долями: order.amount + prepaid_amount = Σ(item.quantity·item.price).
        Здесь prepaid_amount = 0, items строятся из товара (одна позиция)."""
        order: dict[str, Any] = {
            "id": order_id,
            "amount": _num(kopecks_to_rubles(amount_kopecks)),
            "items": items,
        }
        body: dict[str, Any] = {
            "order": order,
            "fiscalization_settings": self.config.fiscalization_settings(),
        }
        if client_info:
            body["client_info"] = client_info
        if notification_url:
            body["notification_url"] = notification_url
        if success_url:
            body["success_url"] = success_url
        if fail_url:
            body["fail_url"] = fail_url

        res = await self._request("POST", "/v1/orders/create", body)
        res.order_id = order_id
        return res

    async def info(self, order_id: str) -> DolyameResult:
        """GET /v1/orders/{orderId}/info — источник истины по статусу/сумме."""
        res = await self._request("GET", f"/v1/orders/{order_id}/info")
        res.order_id = order_id
        return res

    async def commit(
        self, order_id: str, amount_kopecks: int, items: list[dict[str, Any]]
    ) -> DolyameResult:
        """POST /v1/orders/{orderId}/commit — захват холда (двухфазность).

        ВАЖНО про идемпотентность: повторный commit недопустим. Вызывающий код
        (webhook) сперва смотрит info(): commit зовётся ТОЛЬКО если статус
        wait_for_commit. На повторном webhook статус уже committed → commit не зовётся."""
        body = {
            "amount": _num(kopecks_to_rubles(amount_kopecks)),
            "items": items,
            "fiscalization_settings": self.config.fiscalization_settings(),
        }
        res = await self._request("POST", f"/v1/orders/{order_id}/commit", body)
        res.order_id = order_id
        return res

    async def cancel(self, order_id: str) -> DolyameResult:
        """POST /v1/orders/{orderId}/cancel — отмена незакоммиченного заказа."""
        res = await self._request("POST", f"/v1/orders/{order_id}/cancel")
        res.order_id = order_id
        return res

    async def refund(
        self, order_id: str, amount_kopecks: int, returned_items: list[dict[str, Any]]
    ) -> DolyameResult:
        """POST /v1/orders/{orderId}/refund — возврат. Возвраты вне scope прокладки
        (CLAUDE.md), метод оставлен заделом на будущее."""
        body = {
            "amount": _num(kopecks_to_rubles(amount_kopecks)),
            "returned_items": returned_items,
            "fiscalization_settings": self.config.fiscalization_settings(),
        }
        res = await self._request("POST", f"/v1/orders/{order_id}/refund", body)
        res.order_id = order_id
        return res


# ── вспомогательное ──────────────────────────────────────────────────────────


def build_item(
    name: str,
    amount_kopecks: int,
    quantity: int = 1,
    receipt: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Позиция заказа Долями из товара. price — за единицу (рубли); инвариант
    amount = Σ(quantity·price) держится точно (Decimal).

    `receipt` — объект фискализации позиции (tax/payment_method/payment_object/
    measurement_unit); кладётся только при включённой фискализации. None — не
    добавляем (нефискальный заказ). Должен совпадать на create и commit."""
    per_unit = (
        kopecks_to_rubles(amount_kopecks) / Decimal(quantity)
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    item: dict[str, Any] = {
        "name": name[:128],
        "quantity": quantity,
        "price": _num(per_unit),
    }
    if receipt is not None:
        item["receipt"] = receipt
    return item


def _num(value: Decimal) -> float:
    """Decimal(2) -> число для JSON-тела (рубли, напр. 99.0 / 99.99)."""
    return float(value)


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"raw": data}
    except Exception:
        return {}


def _parse_retry_after(resp: httpx.Response) -> float:
    """X-Retry-After (секунды). Дефолт 1с, если заголовка нет/он кривой."""
    raw = resp.headers.get("X-Retry-After") or resp.headers.get("Retry-After")
    try:
        return max(0.0, float(raw)) if raw is not None else 1.0
    except (TypeError, ValueError):
        return 1.0


def _error_result(correlation_id: str, resp: httpx.Response) -> DolyameResult:
    """Разобрать единый формат ошибки Долями {code, errorDetailCode, message, ...}."""
    data = _safe_json(resp)
    code = data.get("errorDetailCode") or data.get("code") or str(resp.status_code)
    return DolyameResult(
        success=False,
        error_code=str(code),
        message=data.get("message") or resp.text[:300],
        correlation_id=data.get("correlationId") or correlation_id,
        raw=data,
    )
