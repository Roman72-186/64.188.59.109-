"""Интеграционные тесты /init-payment — все статусы ответа (PRD §7.4)."""

from __future__ import annotations


def _post(env, body, token=None):
    headers = {}
    if token is not None:
        headers["X-Secret-Token"] = token
    return env.client.post("/init-payment", json=body, headers=headers)


BASIC = {"contact_id": "c1", "product_id": "course_basic", "payment_method": "card", "amount": 9900}


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


def test_amount_override_used_instead_of_config(env):
    # платформа передаёт свою сумму — она должна уйти в Init и сохраниться в БД
    r = _post(env, dict(BASIC, amount=12345), token=env.secret)
    assert r.status_code == 200
    order_id = r.json()["order_id"]
    assert env.tbank.init_calls[-1]["amount"] == 12345
    assert env.db.get_by_order_id(order_id)["amount"] == 12345


def test_amount_string_and_numeric_contact_are_accepted(env):
    r = _post(
        env,
        dict(BASIC, contact_id=12345, amount="9 900", payment_method=" card "),
        token=env.secret,
    )
    assert r.status_code == 200
    order_id = r.json()["order_id"]
    row = env.db.get_by_order_id(order_id)
    assert row["contact_id"] == "12345"
    assert row["amount"] == 9900
    assert env.tbank.init_calls[-1]["amount"] == 9900


def test_amount_omitted_falls_back_to_config(env):
    env.cfg.products["course_basic"].amount = 9900
    body = dict(BASIC)
    body.pop("amount")
    r = _post(env, body, token=env.secret)
    assert r.status_code == 200
    order_id = r.json()["order_id"]
    assert env.tbank.init_calls[-1]["amount"] == 9900
    assert env.db.get_by_order_id(order_id)["amount"] == 9900


def test_amount_omitted_without_config_amount_rejected(env):
    body = dict(BASIC)
    body.pop("amount")
    r = _post(env, body, token=env.secret)
    assert r.status_code == 400
    assert r.json()["status"] == "invalid_product"


def _age_paid(env, order_id, minutes):
    """Сдвинуть время оплаты заказа в прошлое."""
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    with env.db._connect() as conn:
        conn.execute("UPDATE payments SET paid_at = ? WHERE order_id = ?", (ts, order_id))


def test_force_double_click_returns_same_link(env):
    """Бот шлёт force=True на каждое нажатие: второе нажатие на ту же сумму
    получает прежнюю ссылку, второго живого платежа в банке нет."""
    first = _post(env, dict(BASIC, force=True), token=env.secret).json()
    second = _post(env, dict(BASIC, force=True), token=env.secret).json()
    assert second["status"] == "existing_active"
    assert second["order_id"] == first["order_id"]
    assert second["pay_url"] == first["pay_url"]
    assert len(env.tbank.init_calls) == 1


def test_force_other_amount_creates_new_link(env):
    """Другая сумма (сменили тариф) — новая ссылка, даже если прежняя жива."""
    first = _post(env, BASIC, token=env.secret).json()
    second = _post(env, dict(BASIC, force=True, amount=19900), token=env.secret).json()
    assert second["status"] == "created"
    assert second["order_id"] != first["order_id"]
    assert len(env.tbank.init_calls) == 2


def test_force_failed_link_not_reused(env):
    """Ссылку, по которой банк отказал, повторно не выдаём — её уже не оплатить."""
    first = _post(env, dict(BASIC, force=True), token=env.secret).json()
    env.db.mark_failed(first["order_id"], "Т-Банк статус REJECTED")
    second = _post(env, dict(BASIC, force=True), token=env.secret).json()
    assert second["status"] == "created"
    assert second["order_id"] != first["order_id"]


def test_force_right_after_payment_is_not_paid_twice(env):
    """Оплатил и сразу нажал «Оплатить» ещё раз: новый платёж не создаётся
    (инцидент 2922635: 2×5000 ₽ за 5 минут, 26.08.2026)."""
    first = _post(env, dict(BASIC, force=True), token=env.secret).json()
    env.db.mark_paid(first["order_id"])
    env.db.mark_tag_assigned(first["order_id"])
    _age_paid(env, first["order_id"], minutes=5)

    second = _post(env, dict(BASIC, force=True), token=env.secret).json()
    assert second["status"] == "already_paid_access_granted"
    assert second["order_id"] == first["order_id"]
    assert len(env.tbank.init_calls) == 1


def test_force_creates_new_payment_after_previous_payment(env):
    """Продление: прежняя оплата старше 30 минут не мешает новой покупке."""
    first = _post(env, BASIC, token=env.secret).json()
    env.db.mark_paid(first["order_id"])
    env.db.mark_tag_assigned(first["order_id"])
    _age_paid(env, first["order_id"], minutes=31)

    second = _post(env, dict(BASIC, force=True), token=env.secret).json()

    assert second["status"] == "created"
    assert second["order_id"] != first["order_id"]
    assert len(env.tbank.init_calls) == 2


def test_force_recent_payment_other_amount_creates_new(env):
    """Свежая оплата на другую сумму — не дубль (докупка другого тарифа)."""
    first = _post(env, BASIC, token=env.secret).json()
    env.db.mark_paid(first["order_id"])
    env.db.mark_tag_assigned(first["order_id"])

    second = _post(env, dict(BASIC, force=True, amount=19900), token=env.secret).json()
    assert second["status"] == "created"


def test_force_with_amount_override(env):
    _post(env, BASIC, token=env.secret)
    r = _post(env, dict(BASIC, force=True, amount=12345), token=env.secret)
    assert r.status_code == 200
    assert env.tbank.init_calls[-1]["amount"] == 12345


def test_already_paid_pending_when_shalamo_down(env):
    order_id = _post(env, BASIC, token=env.secret).json()["order_id"]
    env.db.mark_paid(order_id)
    env.shalamo.assign_ok = False
    r = _post(env, BASIC, token=env.secret)
    assert r.json()["status"] == "already_paid_pending_access"
    assert env.db.get_by_order_id(order_id)["tag_assigned_at"] is None
