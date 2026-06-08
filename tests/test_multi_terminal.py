"""Маршрутизация по терминалам: отдельный магазин под способ оплаты (напр. Долями).

Проверяет: разрешение реквизитов терминала (наследование от основного), выбор
терминала по способу оплаты, валидацию ссылки на терминал, и — главное — проверку
подписи webhook по TerminalKey (webhook доп. магазина подписан другим паролем)."""

from __future__ import annotations

import os

import pytest
import yaml
from pydantic import ValidationError

from app.config import AppConfig
from app.tbank import build_token

SECRET_TOKEN = "A" * 64
EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.yaml"
)


def _base_raw() -> dict:
    raw = yaml.safe_load(open(EXAMPLE, encoding="utf-8"))
    raw["server"]["secret_token"] = SECRET_TOKEN
    raw["server"]["public_url"] = "https://test.local"
    raw["tbank"]["terminal_key"] = "MainKey"
    raw["tbank"]["terminal_password"] = "mainpw"
    raw["shalamo"]["api_key"] = "shkey"
    return raw


def _multiterminal_raw() -> dict:
    raw = _base_raw()
    raw["tbank"]["extra_terminals"] = {
        "dolyami_shop": {"terminal_key": "DolyamiKey", "terminal_password": "dolyamipw"}
    }
    raw["payment_methods"]["dolyami"]["terminal"] = "dolyami_shop"
    return raw


# ── unit: разрешение терминалов в конфиге ───────────────────────────────────


def test_resolved_terminals_inherit_main_api_url_and_timeout():
    cfg = AppConfig.model_validate(_multiterminal_raw())
    terminals = cfg.resolved_terminals()
    assert set(terminals) == {"MainKey", "DolyamiKey"}
    dolyami = terminals["DolyamiKey"]
    # api_url/timeout не заданы у доп. терминала -> наследуются от основного
    assert dolyami.api_url == cfg.tbank.api_url
    assert dolyami.timeout_seconds == cfg.tbank.timeout_seconds
    assert dolyami.terminal_password == "dolyamipw"


def test_terminal_key_for_method_routes_dolyami_to_extra():
    cfg = AppConfig.model_validate(_multiterminal_raw())
    assert cfg.terminal_key_for_method("dolyami") == "DolyamiKey"
    assert cfg.terminal_key_for_method("card") == "MainKey"
    assert cfg.terminal_key_for_method("sbp") == "MainKey"


def test_password_lookup_by_terminal_key():
    cfg = AppConfig.model_validate(_multiterminal_raw())
    assert cfg.password_for_terminal_key("MainKey") == "mainpw"
    assert cfg.password_for_terminal_key("DolyamiKey") == "dolyamipw"
    assert cfg.password_for_terminal_key("UnknownKey") is None


def test_unknown_terminal_reference_rejected():
    raw = _base_raw()
    raw["payment_methods"]["dolyami"]["terminal"] = "does_not_exist"
    with pytest.raises(ValidationError):
        AppConfig.model_validate(raw)


def test_no_extra_terminals_backward_compatible():
    cfg = AppConfig.model_validate(_base_raw())
    assert cfg.terminal_key_for_method("dolyami") == "MainKey"
    assert list(cfg.resolved_terminals()) == ["MainKey"]


# ── интеграция: webhook доп. магазина подписан своим паролем ─────────────────


def _make_env_mt(env_factory):
    return env_factory(AppConfig.model_validate(_multiterminal_raw()))


def _create_dolyami_order(env) -> dict:
    body = {"contact_id": "c1", "product_id": "course_basic", "payment_method": "dolyami"}
    order_id = env.client.post(
        "/init-payment", json=body, headers={"X-Secret-Token": env.secret}
    ).json()["order_id"]
    return env.db.get_by_order_id(order_id)


def _confirmed(order: dict, terminal_key: str) -> dict:
    return {
        "TerminalKey": terminal_key,
        "OrderId": order["order_id"],
        "PaymentId": order["tbank_payment_id"],
        "Status": "CONFIRMED",
        "Success": True,
        "Amount": order["amount"],
    }


def test_dolyami_webhook_verified_with_extra_terminal_password(env_factory):
    env = _make_env_mt(env_factory)
    order = _create_dolyami_order(env)
    payload = _confirmed(order, "DolyamiKey")
    payload["Token"] = build_token(payload, "dolyamipw")  # подпись доп. магазина
    r = env.client.post("/webhook/tbank", json=payload)
    assert r.status_code == 200 and r.text == "OK"
    row = env.db.get_by_order_id(order["order_id"])
    assert row["tag_assigned_at"] is not None
    assert env.shalamo.tag_calls == [("tag", "c1", "paid_dolyami_basic")]


def test_dolyami_webhook_rejects_main_terminal_password(env_factory):
    env = _make_env_mt(env_factory)
    order = _create_dolyami_order(env)
    payload = _confirmed(order, "DolyamiKey")
    payload["Token"] = build_token(payload, "mainpw")  # НЕ тот пароль -> подпись неверна
    r = env.client.post("/webhook/tbank", json=payload)
    assert r.status_code == 200 and r.text == "OK"
    assert env.db.get_by_order_id(order["order_id"])["tag_assigned_at"] is None
    assert env.shalamo.tag_calls == []


def test_webhook_unknown_terminal_key_no_access(env_factory):
    env = _make_env_mt(env_factory)
    order = _create_dolyami_order(env)
    payload = _confirmed(order, "GhostKey")
    payload["Token"] = build_token(payload, "whatever")
    r = env.client.post("/webhook/tbank", json=payload)
    assert r.status_code == 200 and r.text == "OK"
    assert env.db.get_by_order_id(order["order_id"])["tag_assigned_at"] is None
