"""Интеграционные тесты /init-payment — все статусы ответа (PRD §7.4)."""

from __future__ import annotations


def _post(env, body, token=None):
    headers = {}
    if token is not None:
        headers["X-Secret-Token"] = token
    return env.client.post("/init-payment", json=body, headers=headers)


BASIC = {"contact_id": "c1", "product_id": "course_basic", "payment_method": "card"}


def test_forbidden_without_token(env):
    r = _post(env, BASIC)
    assert r.status_code == 403
    assert r.json()["status"] == "forbidden"


def test_forbidden_wrong_token(env):
    r = _post(env, BASIC, token="wrong")
    assert r.status_code == 403
    assert r.json()["status"] == "forbidden"


def test_invalid_product(env):
    r = _post(env, dict(BASIC, product_id="nope"), token=env.secret)
    assert r.status_code == 400
    assert r.json()["status"] == "invalid_product"


def test_invalid_payment_method(env):
    # installment не входит в payment_methods товара course_basic
    r = _post(env, dict(BASIC, payment_method="installment"), token=env.secret)
    assert r.status_code == 400
    assert r.json()["status"] == "invalid_payment_method"


def test_created(env):
    r = _post(env, BASIC, token=env.secret)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "created"
    assert data["order_id"] and data["pay_url"]


def test_payment_creation_failed(env):
    env.tbank.init_succeeds = False
    r = _post(env, BASIC, token=env.secret)
    assert r.status_code == 502
    assert r.json()["status"] == "payment_creation_failed"


def test_existing_active_link_reused(env):
    first = _post(env, BASIC, token=env.secret).json()
    second = _post(env, BASIC, token=env.secret).json()
    assert second["status"] == "existing_active"
    assert second["order_id"] == first["order_id"]
    # второй раз новый платёж в Т-Банке не создавался
    assert len(env.tbank.init_calls) == 1


def test_already_paid_access_granted(env):
    # создаём и «оплачиваем» с назначенным тегом
    order_id = _post(env, BASIC, token=env.secret).json()["order_id"]
    env.db.mark_paid(order_id)
    env.db.mark_tag_assigned(order_id)
    r = _post(env, BASIC, token=env.secret)
    assert r.status_code == 200
    assert r.json()["status"] == "already_paid_access_granted"
    # новый платёж не создавался (только первый Init)
    assert len(env.tbank.init_calls) == 1


def test_already_paid_reassigns_tag_success(env):
    order_id = _post(env, BASIC, token=env.secret).json()["order_id"]
    env.db.mark_paid(order_id)  # оплачено, но тег НЕ назначен
    r = _post(env, BASIC, token=env.secret)
    assert r.json()["status"] == "already_paid_access_granted"
    assert env.db.get_by_order_id(order_id)["tag_assigned_at"] is not None


def test_already_paid_pending_when_shalamo_down(env):
    order_id = _post(env, BASIC, token=env.secret).json()["order_id"]
    env.db.mark_paid(order_id)
    env.shalamo.assign_ok = False
    r = _post(env, BASIC, token=env.secret)
    assert r.json()["status"] == "already_paid_pending_access"
    assert env.db.get_by_order_id(order_id)["tag_assigned_at"] is None
