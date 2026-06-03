"""Pydantic-схемы запроса/ответа /init-payment и набор статусов (PRD §7.4)."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class InitStatus(str, Enum):
    CREATED = "created"
    EXISTING_ACTIVE = "existing_active"
    ALREADY_PAID_ACCESS_GRANTED = "already_paid_access_granted"
    ALREADY_PAID_PENDING_ACCESS = "already_paid_pending_access"
    INVALID_PRODUCT = "invalid_product"
    INVALID_PAYMENT_METHOD = "invalid_payment_method"
    PAYMENT_CREATION_FAILED = "payment_creation_failed"
    FORBIDDEN = "forbidden"


class InitPaymentRequest(BaseModel):
    contact_id: str = Field(..., min_length=1, description="внутренний ID контакта shalamov.io")
    product_id: str = Field(..., min_length=1)
    payment_method: str = Field(..., min_length=1)
    # Контакт для чека 54-ФЗ (опционально; иначе берётся fallback из config.receipt)
    email: Optional[str] = Field(default=None, description="email покупателя для чека")
    phone: Optional[str] = Field(default=None, description="телефон покупателя для чека")


class InitPaymentResponse(BaseModel):
    status: InitStatus
    order_id: Optional[str] = None
    pay_url: Optional[str] = None
    message: Optional[str] = None
