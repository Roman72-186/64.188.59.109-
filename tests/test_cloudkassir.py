"""Тесты онлайн-кассы CloudKassir: маппинг 54-ФЗ -> коды KKT, тело /kkt/receipt,
авторизация/идемпотентность, выборка нефискализированных заказов в БД, конфиг.

Реальные запросы не уходят: httpx.AsyncClient подменяется на MockTransport
(как в test_shalamo). Каждый успешный /kkt/receipt = реальный чек — в тестах мок.
"""

from __future__ import annotations

import asyncio
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.cloudkassir import (  # noqa: E402
    PAYMENT_METHOD,
    PAYMENT_OBJECT,
    TAXATION_SYSTEM,
    VAT,
    CloudKassirClient,
    build_customer_receipt,
)
from app.config import AppConfig, CloudKassirConfig, ReceiptConfig  # noqa: E402
from app.database import Database  # noqa: E402


def _receipt_patent() -> ReceiptConfig:
    """Боевой профиль чека: патент, без НДС, услуга, предоплата 100%, fallback-email."""
    return ReceiptConfig(
        enabled=True,
        taxation="patent",
        tax="none",
        email="fallback@example.com",
        phone="",
        payment_method="full_prepayment",
        payment_object="service",
    )


def _ck_config() -> CloudKassirConfig:
    return CloudKassirConfig(
        enabled=True,
        public_id="pk_test",
        api_secret="secret",
        inn="236000893906",
        api_url="https://kassa.test",
        fiscalize_providers=["dolyame", "tbank_credit"],
    )


# ── маппинг 54-ФЗ -> коды CloudKassir ────────────────────────────────────────


def test_build_receipt_patent_mapping():
    cr = build_customer_receipt(
        name="Курс «Я Есть»",
        amount_kopecks=9900,
        receipt_cfg=_receipt_patent(),
    )
    assert cr["TaxationSystem"] == 5            # патент
    assert cr["Amounts"]["Electronic"] == 99.0  # полная сумма электронно
    assert cr["Email"] == "fallback@example.com"
    item = cr["Items"][0]
    assert item["Label"] == "Курс «Я Есть»"
    assert item["Price"] == 99.0
    assert item["Amount"] == 99.0
    assert item["Quantity"] == 1
    assert item["Vat"] is None                  # без НДС (патент)
    assert item["Method"] == 1                  # full_prepayment
    assert item["Object"] == 4                  # service
    assert item["MeasurementUnit"] == "шт"


def test_mapping_tables_cover_config_values():
    # значения из 54-ФЗ конфига (receipt) должны иметь код в таблицах CloudKassir
    assert TAXATION_SYSTEM["patent"] == 5
    assert TAXATION_SYSTEM["usn_income"] == 1
    assert VAT["none"] is None
    assert VAT["vat20"] == 20
    assert PAYMENT_METHOD["full_prepayment"] == 1
    assert PAYMENT_OBJECT["service"] == 4


def test_build_receipt_contact_and_tax_override():
    cr = build_customer_receipt(
        name="X",
        amount_kopecks=12345,
        receipt_cfg=_receipt_patent(),
        tax_override="vat20",
        email="buyer@example.com",
        phone="+79990000000",
    )
    assert cr["Email"] == "buyer@example.com"   # из заказа, не fallback
    assert cr["Phone"] == "+79990000000"
    assert cr["Items"][0]["Vat"] == 20          # override
    assert cr["Items"][0]["Amount"] == 123.45


# ── send_receipt: тело, авторизация, идемпотентность, парсинг ответа ──────────


def _client_with_capture(monkeypatch, *, status=200, payload=None):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["content"] = request.content
        return httpx.Response(status, json=payload if payload is not None else {})

    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    monkeypatch.setattr("app.cloudkassir.httpx.AsyncClient", patched)
    return CloudKassirClient(_ck_config(), _receipt_patent()), captured


def test_send_receipt_body_auth_idempotency(monkeypatch):
    client, captured = _client_with_capture(
        monkeypatch,
        payload={"Success": True, "Message": "Queued", "Model": {"Id": "r-1", "ReceiptLocalUrl": "https://r/r-1"}},
    )
    res = asyncio.run(
        client.send_receipt(
            order_id="ord-42",
            account_id="contact-7",
            name="Курс",
            amount_kopecks=9900,
            email="buyer@example.com",
        )
    )
    assert res.success and res.queued
    assert res.receipt_id == "r-1"
    assert res.receipt_url == "https://r/r-1"

    # эндпоинт и метод
    assert captured["method"] == "POST"
    assert captured["url"] == "https://kassa.test/kkt/receipt"
    # идемпотентность: InvoiceId в теле + X-Request-ID = order_id
    import json
    body = json.loads(captured["content"])
    assert body["Inn"] == "236000893906"
    assert body["Type"] == "Income"
    assert body["InvoiceId"] == "ord-42"
    assert body["AccountId"] == "contact-7"
    assert body["CustomerReceipt"]["Email"] == "buyer@example.com"
    assert captured["headers"]["x-request-id"] == "ord-42"
    # Basic-авторизация присутствует
    assert captured["headers"]["authorization"].lower().startswith("basic ")


def test_send_receipt_failure_does_not_mark(monkeypatch):
    client, _ = _client_with_capture(
        monkeypatch, payload={"Success": False, "Message": "INN invalid"}
    )
    res = asyncio.run(
        client.send_receipt(
            order_id="o", account_id="c", name="n", amount_kopecks=100
        )
    )
    assert res.success is False
    assert res.error == "INN invalid"


def test_ping_hits_test_endpoint(monkeypatch):
    client, captured = _client_with_capture(
        monkeypatch, payload={"Success": True, "Message": "OK"}
    )
    res = asyncio.run(client.ping())
    assert res.success
    assert captured["url"] == "https://kassa.test/test"


# ── БД: сохранение контакта, выборка нефискализированных, пометка ─────────────


def _db(tmp_path) -> Database:
    db = Database(db_path=str(tmp_path / "t.db"))
    db.init_db()
    return db


def test_create_payment_persists_contact(tmp_path):
    db = _db(tmp_path)
    row = db.create_payment(
        "o1", "c1", "p1", "dolyami", 9900, "paid_tag",
        item_name="Курс", email="buyer@example.com", phone="+79990000000",
    )
    assert row["email"] == "buyer@example.com"
    assert row["phone"] == "+79990000000"


def test_unfiscalized_selection_and_mark(tmp_path):
    db = _db(tmp_path)
    # оплаченный заказ Долями — должен попасть в выборку
    db.create_payment("paid", "c", "p", "dolyami", 9900, "t", item_name="Курс")
    db.mark_paid("paid")
    # неоплаченный — не попадает
    db.create_payment("pending", "c", "p", "dolyami", 9900, "t", item_name="Курс")
    # оплаченный, но другой канал (карта) — не попадает (фискализируется эквайрингом)
    db.create_payment("card", "c", "p", "card", 9900, "t", item_name="Курс")
    db.mark_paid("card")

    pending = db.get_unfiscalized_orders(["dolyami", "installment_3"], 30 * 24 * 3600)
    ids = {r["order_id"] for r in pending}
    assert ids == {"paid"}

    # после пометки чек больше не выбирается
    db.mark_receipt_sent("paid")
    pending2 = db.get_unfiscalized_orders(["dolyami"], 30 * 24 * 3600)
    assert pending2 == []


def test_unfiscalized_empty_methods(tmp_path):
    db = _db(tmp_path)
    assert db.get_unfiscalized_orders([], 1000) == []


def test_migration_backfills_existing_rows(tmp_path):
    """Заказы, завершённые ДО подключения кассы, не должны фискализироваться задним
    числом: миграция помечает их receipt_sent_at (бэкфилл)."""
    import sqlite3

    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE payments ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL UNIQUE,"
        " contact_id TEXT NOT NULL, product_id TEXT NOT NULL,"
        " payment_method TEXT NOT NULL, amount INTEGER NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'pending', tbank_payment_id TEXT,"
        " tbank_status TEXT, pay_url TEXT, tag_name TEXT, paid_at TEXT,"
        " tag_assigned_at TEXT, last_error TEXT, created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO payments (order_id, contact_id, product_id, payment_method,"
        " amount, status, paid_at, created_at, updated_at) VALUES"
        " ('old','c','p','dolyami',9900,'confirmed',"
        " '2026-06-01T00:00:00','2026-06-01T00:00:00','2026-06-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    db = Database(db_path=path)
    db.init_db()  # миграция: добавляет receipt_sent_at + бэкфилл существующих строк

    assert db.get_unfiscalized_orders(["dolyami"], 365 * 24 * 3600) == []
    assert db.get_by_order_id("old")["receipt_sent_at"] is not None


# ── конфиг: cloudkassir_methods + валидация ──────────────────────────────────


def _app_config(monkeypatch, *, enabled=True, providers=("dolyame",)):
    import yaml
    example = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "config.example.yaml",
    )
    raw = yaml.safe_load(open(example, encoding="utf-8"))
    raw["server"]["secret_token"] = "A" * 64
    raw["server"]["public_url"] = "https://test.local"
    raw["tbank"]["terminal_key"] = "K"
    raw["tbank"]["terminal_password"] = "pw"
    raw["shalamo"]["api_key"] = "k"
    raw["dolyame"] = {"base_url": "https://d.test", "login": "l", "password": "p"}
    raw["payment_methods"]["dolyami"]["provider"] = "dolyame"
    raw["cloudkassir"] = {
        "enabled": enabled,
        "public_id": "pk",
        "api_secret": "s",
        "inn": "1",
        "fiscalize_providers": list(providers),
    }
    return AppConfig.model_validate(raw)


def test_cloudkassir_methods_selects_dolyame(monkeypatch):
    cfg = _app_config(monkeypatch, providers=("dolyame",))
    assert "dolyami" in cfg.cloudkassir_methods()


def test_cloudkassir_methods_empty_when_disabled(monkeypatch):
    cfg = _app_config(monkeypatch, enabled=False, providers=("dolyame",))
    assert cfg.cloudkassir_methods() == []


def test_cloudkassir_bad_provider_rejected(monkeypatch):
    with pytest.raises(Exception):
        _app_config(monkeypatch, providers=("dolyame", "bogus"))
