"""Клиент онлайн-кассы CloudKassir (CloudPayments KKT) — фискализация чеков 54-ФЗ.

Зачем отдельно: карту/СБП через эквайринг Т-Банка фискализирует касса автоматически
(чек печатается после CONFIRMED), а Долями и рассрочка/кредит (Credit Broker) своих
чеков НЕ дают. CloudKassir подключается как единая касса мерчанта и пробивает чек по
этим каналам напрямую через API — прокладка лишь передаёт состав чека.

Особенности API (см. https://developers.cloudkassir.ru):
  • Эндпоинт:  POST {base_url}/kkt/receipt  (по умолчанию api.cloudpayments.ru).
  • Авторизация: HTTP Basic — Public ID (login) : API Secret (password).
  • Поля тела — PascalCase. Суммы — В РУБЛЯХ (number, 2 знака). В проекте суммы хранятся
    в копейках — конвертация здесь, точно, через Decimal (kopecks_to_rubles из dolyame).
  • Идемпотентность: дедуп по InvoiceId + заголовок X-Request-ID (кэш результата 1ч).
    Поэтому повторная отправка того же заказа не создаёт второй чек — безопасно ретраить.
  • Ответ асинхронный: {"Success": true, "Message": "Queued", "Model": {"Id": ...,
    "ReceiptLocalUrl": ...}} — чек ставится в очередь, печатает касса/ОФД.
  • Тестовый эндпоинт POST /test — проверяет логин/пароль БЕЗ пробития чека (ping()).

ВАЖНО: каждый успешный вызов /kkt/receipt = реальный фискальный документ. В тестах
клиент мокается (как dolyame), живой вызов — только ping() либо боевой заказ.

PENDING LIVE VALIDATION — схема собрана по developers.cloudkassir.ru, но НЕ проверена
боевым ответом /kkt/receipt (как было с 400 на commit Долями: swagger.required ≠
runtime). Тесты проверяют форму, которую мы САМИ задали, а не которую примет касса.
Развилки, которые разрешит только реальный чек (если касса отбивает 4xx — смотреть сюда):
  • Патент без НДС: шлём Vat=null. Возможно, касса хочет отсутствие ключа или код
    «без НДС» — самый вероятный кандидат на отказ.
  • Имя поля ставки: Vat (использовано) vs VatRate (встречалось в доке).
  • Quantity: шлём числом 1; в одном источнике значился строкой.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from .config import CloudKassirConfig, ReceiptConfig
from .dolyame import _num, kopecks_to_rubles
from .logging_setup import get_logger

log = get_logger()


# ── маппинг строковых значений 54-ФЗ (из общего блока receipt) в коды CloudKassir ──
# Источник кодов: developers.cloudkassir.ru. Перечни общие с эквайрингом Т-Банка,
# чтобы чек CloudKassir совпадал с чеком карты/СБП (единая касса).

# Система налогообложения (CustomerReceipt.TaxationSystem).
TAXATION_SYSTEM = {
    "osn": 0,                 # общая
    "usn_income": 1,          # УСН доход
    "usn_income_outcome": 2,  # УСН доход-расход
    "envd": 3,                # ЕНВД
    "esn": 4,                 # ЕСХН
    "patent": 5,              # патент
}

# Ставка НДС (Item.Vat). None → «без НДС» (для патента/УСН без НДС ключ шлём как null).
VAT = {
    "none": None,
    "vat0": 0,
    "vat5": 5,
    "vat10": 10,
    "vat20": 20,
    "vat22": 22,
    "vat105": 105,
    "vat107": 107,
    "vat110": 110,
    "vat120": 120,
    "vat122": 122,
}

# Признак способа расчёта (Item.Method, тег-1214).
PAYMENT_METHOD = {
    "full_prepayment": 1,   # предоплата 100%
    "prepayment": 2,        # предоплата (частичная)
    "advance": 3,           # аванс
    "full_payment": 4,      # полный расчёт (по умолчанию)
    "partial_payment": 5,   # частичный расчёт и кредит
    "credit": 6,            # передача в кредит
    "credit_payment": 7,    # оплата кредита
}

# Признак предмета расчёта (Item.Object, тег-1212).
PAYMENT_OBJECT = {
    "commodity": 1,   # товар
    "excise": 2,      # подакцизный товар
    "job": 3,         # работа
    "service": 4,     # услуга
    "payment": 10,    # платёж
}


@dataclass
class CloudKassirResult:
    """Унифицированный результат вызова CloudKassir."""

    success: bool
    queued: bool = False                  # чек принят в очередь (Message == "Queued")
    receipt_id: Optional[str] = None      # Model.Id
    receipt_url: Optional[str] = None     # Model.ReceiptLocalUrl
    error: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


def build_customer_receipt(
    *,
    name: str,
    amount_kopecks: int,
    receipt_cfg: Optional[ReceiptConfig],
    tax_override: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
) -> dict[str, Any]:
    """Собрать объект CustomerReceipt (одна позиция = оплачиваемый товар).

    Признаки расчёта берутся из общего блока `receipt` (54-ФЗ) — те же, что у чека
    карты/СБП. `tax_override` переопределяет ставку НДС позиции (иначе receipt.tax).
    Email/Phone — контакт получателя чека (из заказа), иначе fallback из receipt.
    Amounts.Electronic = полная сумма (покупатель оплачивает электронно).
    """
    r = receipt_cfg
    rub = _num(kopecks_to_rubles(amount_kopecks))
    tax_key = tax_override or (r.tax if r else "none")
    item: dict[str, Any] = {
        "Label": name[:128],
        "Price": rub,
        "Quantity": 1,
        "Amount": rub,
        "Vat": VAT.get(tax_key, None),
        "Method": PAYMENT_METHOD.get(
            (r.payment_method if r and r.payment_method else "full_payment"), 4
        ),
        "Object": PAYMENT_OBJECT.get(
            (r.payment_object if r and r.payment_object else "commodity"), 1
        ),
        "MeasurementUnit": "шт",
    }
    cr: dict[str, Any] = {
        "Items": [item],
        "TaxationSystem": TAXATION_SYSTEM.get((r.taxation if r else "osn"), 0),
        "Amounts": {"Electronic": rub},
    }
    contact_email = email or (r.email if r else None)
    contact_phone = phone or (r.phone if r else None)
    if contact_email:
        cr["Email"] = contact_email
    if contact_phone:
        cr["Phone"] = contact_phone
    return cr


class CloudKassirClient:
    def __init__(
        self, config: CloudKassirConfig, receipt: Optional[ReceiptConfig]
    ) -> None:
        self.config = config
        self.receipt = receipt
        self.base_url = config.api_url.rstrip("/")

    @property
    def _auth(self) -> httpx.BasicAuth:
        return httpx.BasicAuth(self.config.public_id, self.config.api_secret)

    async def _post(self, path: str, body: dict[str, Any], request_id: str) -> CloudKassirResult:
        url = f"{self.base_url}{path}"
        headers = {"X-Request-ID": request_id}
        try:
            async with httpx.AsyncClient(
                timeout=self.config.timeout_seconds, auth=self._auth
            ) as client:
                resp = await client.post(url, headers=headers, json=body)
        except Exception as e:  # сеть/таймаут
            log.error("CloudKassir POST %s: ошибка запроса: %s", path, e)
            return CloudKassirResult(success=False, error=str(e))

        data: dict[str, Any] = {}
        try:
            data = resp.json()
        except Exception:
            data = {}

        if 200 <= resp.status_code < 300 and data.get("Success"):
            model = data.get("Model") or {}
            message = str(data.get("Message") or "")
            log.info(
                "CloudKassir POST %s OK (HTTP %s) message=%s id=%s",
                path, resp.status_code, message, model.get("Id"),
            )
            return CloudKassirResult(
                success=True,
                queued=message.lower() == "queued",
                receipt_id=model.get("Id"),
                receipt_url=model.get("ReceiptLocalUrl"),
                raw=data,
            )

        err = str(data.get("Message") or f"HTTP {resp.status_code}")
        log.error("CloudKassir POST %s отказ: HTTP %s msg=%s", path, resp.status_code, err)
        return CloudKassirResult(success=False, error=err, raw=data)

    async def send_receipt(
        self,
        *,
        order_id: str,
        account_id: str,
        name: str,
        amount_kopecks: int,
        tax_override: Optional[str] = None,
        email: Optional[str] = None,
        phone: Optional[str] = None,
    ) -> CloudKassirResult:
        """Пробить чек прихода (Type=Income). InvoiceId=order_id (дедуп на стороне
        кассы), X-Request-ID=order_id (кэш повторов 1ч) — повторный вызов не
        создаёт второй чек."""
        customer_receipt = build_customer_receipt(
            name=name,
            amount_kopecks=amount_kopecks,
            receipt_cfg=self.receipt,
            tax_override=tax_override,
            email=email,
            phone=phone,
        )
        body: dict[str, Any] = {
            "Inn": self.config.inn,
            "Type": "Income",
            "InvoiceId": order_id,
            "AccountId": account_id,
            "CustomerReceipt": customer_receipt,
        }
        return await self._post("/kkt/receipt", body, request_id=order_id)

    async def ping(self) -> CloudKassirResult:
        """POST /test — проверка логина/пароля БЕЗ пробития чека."""
        return await self._post("/test", {}, request_id="ping")
