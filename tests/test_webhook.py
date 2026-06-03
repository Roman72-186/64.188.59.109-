"""Интеграционные тесты /webhook/tbank: подпись, сумма, идемпотентность, 503."""

from __future__ import annotations

from app.tbank import build_token

BASIC = {"contact_id": "c1", "product_id": "course_basic", "payment_method": "card"}


def signed_webhook(payload: dict, password: str = "testpw") -> dict:
    body = dict(payload)
    body["Token"] = build_token(body, password)
    return body


def _create_order(env) -> dict:
    """Создать платёж через /init-payment и вернуть его строку из БД."""
    order_id = env.client.post(
        "/init-payment", json=BASIC, headers={"X-Secret-Token": env.secret}
    ).json()["order_id"]
    return env.db.get_by_order_id(order_id)


def _confirmed_payload(order: dict, amount: int | None = None) -> dict:
    return {
        "TerminalKey": "TestKey",
        "OrderId": order["order_id"],
        "PaymentId": order["tbank_payment_id"],
        "Status": "CONFIRMED",
        "Success": True,
        "Amount": amount if amount is not None else order["amount"],
    }


def test_confirmed_assigns_tag(env):
    order = _create_order(env)
    r = env.client.post("/webhook/tbank", json=signed_webhook(_confirmed_payload(order)))
    assert r.status_code == 200 and r.text == "OK"
    row = env.db.get_by_order_id(order["order_id"])
    assert row["status"] == "confirmed"
    assert row["tag_assigned_at"] is not None
    assert env.shalamo.tag_calls == [("tag", "c1", "paid_card_basic")]


def test_bad_signature_no_access(env):
    order = _create_order(env)
    payload = _confirmed_payload(order)
    payload["Token"] = "deadbeef"  # неверная подпись
    r = env.client.post("/webhook/tbank", json=payload)
    assert r.status_code == 200 and r.text == "OK"
    assert env.db.get_by_order_id(order["order_id"])["tag_assigned_at"] is None
    assert env.shalamo.tag_calls == []


def test_wrong_amount_rejected_via_getstate(env):
    order = _create_order(env)
    # webhook с неверной суммой; GetState тоже вернёт «не ту» сумму -> отказ
    env.tbank.state_result.amount = 1
    r = env.client.post(
        "/webhook/tbank", json=signed_webhook(_confirmed_payload(order, amount=1))
    )
    assert r.status_code == 200 and r.text == "OK"
    assert env.db.get_by_order_id(order["order_id"])["tag_assigned_at"] is None


def test_non_confirmed_status_no_access(env):
    order = _create_order(env)
    payload = _confirmed_payload(order)
    payload["Status"] = "REJECTED"
    r = env.client.post("/webhook/tbank", json=signed_webhook(payload))
    assert r.status_code == 200 and r.text == "OK"
    row = env.db.get_by_order_id(order["order_id"])
    assert row["tag_assigned_at"] is None
    assert row["status"] == "failed"


def test_idempotent_duplicate_webhook(env):
    order = _create_order(env)
    p = signed_webhook(_confirmed_payload(order))
    env.client.post("/webhook/tbank", json=p)
    env.client.post("/webhook/tbank", json=p)  # дубль
    # тег назначен ровно один раз
    assert len(env.shalamo.tag_calls) == 1


def test_503_when_shalamo_fails_then_reprocess(env):
    order = _create_order(env)
    p = signed_webhook(_confirmed_payload(order))

    env.shalamo.assign_ok = False
    r = env.client.post("/webhook/tbank", json=p)
    assert r.status_code == 503
    row = env.db.get_by_order_id(order["order_id"])
    assert row["tag_assigned_at"] is None
    assert row["paid_at"] is not None  # оплата зафиксирована
    # две быстрые попытки внутри одного webhook
    assert len(env.shalamo.tag_calls) == 2

    # Т-Банк повторяет webhook; shalamo снова доступен -> доступ выдаётся
    env.shalamo.assign_ok = True
    r2 = env.client.post("/webhook/tbank", json=p)
    assert r2.status_code == 200 and r2.text == "OK"
    assert env.db.get_by_order_id(order["order_id"])["tag_assigned_at"] is not None


def test_unknown_order_returns_ok(env):
    payload = {
        "TerminalKey": "TestKey", "OrderId": "ghost", "PaymentId": "x",
        "Status": "CONFIRMED", "Success": True, "Amount": 9900,
    }
    r = env.client.post("/webhook/tbank", json=signed_webhook(payload))
    assert r.status_code == 200 and r.text == "OK"
