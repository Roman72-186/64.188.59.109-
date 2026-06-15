"""Тесты чека 54-ФЗ (Receipt): сборка объекта, подпись Token, проброс в Init."""

from __future__ import annotations

import os

import yaml

from app.config import AppConfig
from app.tbank import build_token

TEST_PASSWORD = "testpw"
SECRET_TOKEN = "A" * 64
EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.yaml"
)


def _cfg_with_receipt(**receipt_overrides) -> AppConfig:
    raw = yaml.safe_load(open(EXAMPLE, encoding="utf-8"))
    raw["server"]["secret_token"] = SECRET_TOKEN
    raw["server"]["public_url"] = "https://test.local"
    raw["tbank"]["terminal_key"] = "TestKey"
    raw["tbank"]["terminal_password"] = TEST_PASSWORD
    raw["shalamo"]["api_key"] = "shkey"
    receipt = {
        "enabled": True,
        "taxation": "usn_income",
        "tax": "none",
        "payment_object": "service",
        "payment_method": "full_prepayment",
        "email": "fallback@example.com",
        "phone": "",
    }
    receipt.update(receipt_overrides)
    raw["receipt"] = receipt
    return AppConfig.model_validate(raw)


def test_build_receipt_structure():
    cfg = _cfg_with_receipt()
    r = cfg.build_receipt("Курс: Базовый тариф", 9900, email="buyer@example.com")
    assert r["Taxation"] == "usn_income"
    assert r["Email"] == "buyer@example.com"  # из запроса, не fallback
    assert "Phone" not in r
    assert len(r["Items"]) == 1
    item = r["Items"][0]
    assert item["Name"] == "Курс: Базовый тариф"
    assert item["Price"] == 9900
    assert item["Quantity"] == 1
    assert item["Amount"] == 9900  # Price * Quantity, в копейках
    assert item["Tax"] == "none"
    assert item["PaymentObject"] == "service"
    assert item["PaymentMethod"] == "full_prepayment"


def test_build_receipt_fallback_contact():
    cfg = _cfg_with_receipt()
    r = cfg.build_receipt("Курс: Базовый тариф", 9900)  # бот не передал email/phone
    assert r["Email"] == "fallback@example.com"


def test_build_receipt_disabled_returns_none():
    cfg = _cfg_with_receipt(enabled=False)
    assert cfg.build_receipt("Курс", 9900, email="b@e.com") is None


def test_no_receipt_block_returns_none():
    raw = yaml.safe_load(open(EXAMPLE, encoding="utf-8"))
    raw["server"]["secret_token"] = SECRET_TOKEN
    raw["server"]["public_url"] = "https://test.local"
    raw["tbank"]["terminal_password"] = TEST_PASSWORD
    raw["shalamo"]["api_key"] = "shkey"
    raw.pop("receipt", None)
    cfg = AppConfig.model_validate(raw)
    assert cfg.receipt is None
    assert cfg.build_receipt("Курс", 9900, email="b@e.com") is None


def test_tax_override():
    # Явный tax (вызывающий передаёт product.tax или из cart) важнее receipt.tax.
    cfg = _cfg_with_receipt(tax="vat20")
    r = cfg.build_receipt("Курс", 9900, tax="vat10", email="b@e.com")
    assert r["Items"][0]["Tax"] == "vat10"
    # Без явного tax — берётся receipt.tax.
    r2 = cfg.build_receipt("Курс", 9900, email="b@e.com")
    assert r2["Items"][0]["Tax"] == "vat20"


def test_receipt_excluded_from_token():
    """Receipt — вложенный объект и НЕ должен влиять на подпись Token."""
    base = {"TerminalKey": "T", "Amount": 9900, "OrderId": "o1"}
    with_receipt = dict(base, Receipt={"Taxation": "usn_income", "Items": [{"x": 1}]})
    assert build_token(base, "pw") == build_token(with_receipt, "pw")


def test_init_payment_passes_receipt(env_factory):
    """С включённым чеком /init-payment пробрасывает Receipt в Т-Банк."""
    env = env_factory(_cfg_with_receipt())
    resp = env.client.post(
        "/init-payment",
        headers={"X-Secret-Token": SECRET_TOKEN},
        json={
            "contact_id": "c1", "product_id": "course_basic",
            "payment_method": "card", "email": "buyer@example.com", "amount": 9900,
        },
    )
    assert resp.status_code == 200
    receipt = env.tbank.init_calls[-1]["receipt"]
    assert receipt is not None
    assert receipt["Email"] == "buyer@example.com"
    assert receipt["Items"][0]["Amount"] == 9900


def test_amount_override_reflected_in_receipt(env_factory):
    """Сумма, переданная платформой в /init-payment, должна попасть в Receipt —
    иначе Init и Receipt разойдутся и Т-Банк отклонит платёж."""
    env = env_factory(_cfg_with_receipt())
    resp = env.client.post(
        "/init-payment",
        headers={"X-Secret-Token": SECRET_TOKEN},
        json={
            "contact_id": "c1", "product_id": "course_basic",
            "payment_method": "card", "email": "buyer@example.com", "amount": 12345,
        },
    )
    assert resp.status_code == 200
    receipt = env.tbank.init_calls[-1]["receipt"]
    assert receipt["Items"][0]["Price"] == 12345
    assert receipt["Items"][0]["Amount"] == 12345
    assert env.tbank.init_calls[-1]["amount"] == 12345


def test_init_payment_no_receipt_when_disabled(env):
    """Без блока receipt в Init уходит receipt=None (обратная совместимость)."""
    resp = env.client.post(
        "/init-payment",
        headers={"X-Secret-Token": SECRET_TOKEN},
        json={"contact_id": "c1", "product_id": "course_basic", "payment_method": "card", "amount": 9900},
    )
    assert resp.status_code == 200
    assert env.tbank.init_calls[-1]["receipt"] is None
