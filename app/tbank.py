"""Клиент Т-Банка (Tinkoff Acquiring API v2): Init, GetState, подпись Token.

Алгоритм Token (Т-Банк):
  1. взять КОРНЕВЫЕ скалярные параметры запроса (без вложенных объектов/массивов и без Token);
  2. добавить пару Password = пароль терминала;
  3. отсортировать по ключу;
  4. сконкатенировать значения в одну строку;
  5. SHA-256 -> hex (нижний регистр).
Тот же алгоритм используется для проверки подписи webhook (verify_webhook_token).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from .logging_setup import get_logger

log = get_logger()


def _token_value(value: Any) -> str:
    """Привести значение к строке так, как этого ждёт Т-Банк при подписи."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build_token(params: dict[str, Any], password: str) -> str:
    """Посчитать Token по корневым скалярным параметрам + Password."""
    payload = {
        k: v
        for k, v in params.items()
        if k != "Token" and not isinstance(v, (dict, list))
    }
    payload["Password"] = password
    concatenated = "".join(_token_value(payload[k]) for k in sorted(payload))
    return hashlib.sha256(concatenated.encode("utf-8")).hexdigest()


def verify_webhook_token(payload: dict[str, Any], password: str) -> bool:
    """Проверить подпись входящего webhook Т-Банка."""
    received = payload.get("Token")
    if not received:
        return False
    expected = build_token(payload, password)
    # сравнение в постоянное время
    return hmac.compare_digest(str(received), expected)


@dataclass
class InitResult:
    success: bool
    payment_id: Optional[str] = None
    pay_url: Optional[str] = None
    error_code: Optional[str] = None
    message: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class StateResult:
    success: bool
    status: Optional[str] = None
    amount: Optional[int] = None
    error_code: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


class TBankClient:
    def __init__(
        self,
        terminal_key: str,
        terminal_password: str,
        api_url: str,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.terminal_key = terminal_key
        self._password = terminal_password
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout_seconds

    def _signed(self, params: dict[str, Any]) -> dict[str, Any]:
        body = dict(params)
        body["Token"] = build_token(body, self._password)
        return body

    async def init_payment(
        self,
        order_id: str,
        amount: int,
        description: str,
        notification_url: Optional[str] = None,
        extra_params: Optional[dict[str, Any]] = None,
        receipt: Optional[dict[str, Any]] = None,
    ) -> InitResult:
        """Создать платёж (POST /Init). amount — в копейках."""
        params: dict[str, Any] = {
            "TerminalKey": self.terminal_key,
            "Amount": int(amount),
            "OrderId": order_id,
            "Description": description or "",
        }
        if notification_url:
            params["NotificationURL"] = notification_url
        if extra_params:
            # PayType и т.п. — корневые скалярные параметры, участвуют в подписи
            params.update(extra_params)
        if receipt:
            # Receipt — вложенный объект (54-ФЗ); в подпись Token НЕ входит
            # (build_token отбрасывает dict/list). Т-Банк так и ожидает.
            params["Receipt"] = receipt

        body = self._signed(params)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(f"{self.api_url}/Init", json=body)
            data = resp.json()
        except Exception as e:  # сеть/таймаут/невалидный JSON
            log.error("Т-Банк Init: ошибка запроса order=%s: %s", order_id, e)
            return InitResult(success=False, message=str(e))

        if data.get("Success"):
            log.info(
                "Т-Банк Init OK order=%s payment_id=%s",
                order_id,
                data.get("PaymentId"),
            )
            return InitResult(
                success=True,
                payment_id=str(data.get("PaymentId")),
                pay_url=data.get("PaymentURL"),
                raw=data,
            )
        log.error(
            "Т-Банк Init отказ order=%s code=%s msg=%s",
            order_id,
            data.get("ErrorCode"),
            data.get("Message"),
        )
        return InitResult(
            success=False,
            error_code=data.get("ErrorCode"),
            message=data.get("Message") or data.get("Details"),
            raw=data,
        )

    async def get_state(self, payment_id: str) -> StateResult:
        """Запросить состояние платежа (POST /GetState) — PRD §7.6."""
        params = {"TerminalKey": self.terminal_key, "PaymentId": str(payment_id)}
        body = self._signed(params)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(f"{self.api_url}/GetState", json=body)
            data = resp.json()
        except Exception as e:
            log.error("Т-Банк GetState: ошибка запроса payment_id=%s: %s", payment_id, e)
            return StateResult(success=False, error_code=str(e))

        if data.get("Success"):
            return StateResult(
                success=True,
                status=data.get("Status"),
                amount=data.get("Amount"),
                raw=data,
            )
        return StateResult(
            success=False, error_code=data.get("ErrorCode"), raw=data
        )
