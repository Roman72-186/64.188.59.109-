"""Unit-тесты: подпись Token и слой БД (идемпотентность, finders)."""

from __future__ import annotations

import tempfile

from app.database import Database
from app.tbank import build_token, verify_webhook_token


def test_token_reference_vector():
    # Эталон из документации Т-Банка.
    params = {
        "TerminalKey": "MerchantTerminalKey",
        "Amount": "19200",
        "OrderId": "21090",
        "Description": "Подарочная карта на 1000 рублей",
    }
    assert (
        build_token(params, "usaf8fw8fsw21g")
        == "0024a00af7c350a3a67ca168ce06502aa72772456662e38696d48b56ee9c97d9"
    )


def test_token_int_and_bool_normalization():
    p = {"Amount": 19200, "Success": True, "Status": "CONFIRMED"}
    # int и bool приводятся к строкам стабильно
    assert build_token(p, "pw") == build_token(
        {"Amount": "19200", "Success": True, "Status": "CONFIRMED"}, "pw"
    )


def test_webhook_verify():
    wh = {"TerminalKey": "T", "OrderId": "o", "Success": True, "Amount": 9900}
    wh["Token"] = build_token(wh, "pw")
    assert verify_webhook_token(wh, "pw") is True
    assert verify_webhook_token(wh, "other") is False
    tampered = dict(wh, Amount=1)
    assert verify_webhook_token(tampered, "pw") is False
    assert verify_webhook_token({"OrderId": "o"}, "pw") is False  # нет Token


def _db() -> Database:
    db = Database(tempfile.mktemp(suffix=".db"))
    db.init_db()
    return db


def test_atomic_capture_idempotent():
    db = _db()
    db.create_payment("o1", "c1", "course_basic", "card", 9900, "paid_card_basic")
    db.mark_paid("o1")
    assert db.atomic_capture("o1") is True
    db.mark_tag_assigned("o1")
    # после назначения тега повторный захват невозможен
    assert db.atomic_capture("o1") is False
    assert db.get_by_order_id("o1")["status"] == "confirmed"


def test_find_active_link_age_and_method():
    db = _db()
    db.create_payment("o1", "c1", "course_basic", "card", 9900, "t")
    db.update_init_result("o1", "p1", "https://pay/o1")
    assert db.find_active_link("c1", "course_basic", "card") is not None
    assert db.find_active_link("c1", "course_basic", "sbp") is None
    # истёкшая ссылка (max_age=0) не считается активной
    assert db.find_active_link("c1", "course_basic", "card", max_age_seconds=0) is None


def test_find_paid_order_requires_paid_at():
    db = _db()
    db.create_payment("o1", "c1", "course_basic", "card", 9900, "t")
    assert db.find_paid_order("c1", "course_basic") is None
    db.mark_paid("o1")
    assert db.find_paid_order("c1", "course_basic") is not None


def test_get_pending_credit_orders_filters():
    db = _db()
    db.create_payment("o1", "c1", "course_basic", "credit", 5000000, "paid_credit_basic")
    db.create_payment("o2", "c2", "course_basic", "credit", 5000000, "paid_credit_basic")
    db.create_payment("o3", "c3", "course_basic", "credit", 5000000, "paid_credit_basic")
    db.create_payment("o4", "c4", "course_basic", "card", 9900, "paid_card_basic")

    # o2 — тег уже назначен, o3 — назначен тег отказа: оба не должны опрашиваться
    db.mark_paid("o2")
    db.atomic_capture("o2")
    db.mark_tag_assigned("o2")
    db.capture_fail_tag("o3")
    db.mark_fail_tag_assigned("o3")

    pending = db.get_pending_credit_orders(["credit"], max_age_seconds=3600)
    ids = {o["order_id"] for o in pending}
    assert ids == {"o1"}  # o2/o3 завершены, o4 — другой способ оплаты

    # max_age=0 -> только что созданные заявки уже "устарели"
    assert db.get_pending_credit_orders(["credit"], max_age_seconds=0) == []

    # неизвестный способ оплаты -> ничего
    assert db.get_pending_credit_orders(["installment_3"], max_age_seconds=3600) == []
