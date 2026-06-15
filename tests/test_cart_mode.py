"""Тесты cart-режима: product_id НЕ из config.products.

Платформа сама передаёт состав заказа в `cart` и сумму в `amount`, не привязываясь
к серверному товару. Тег доступа берётся из глобального tags_by_method по способу
оплаты, тег отказа — из глобального fail_tags_by_method. Имя позиции (cart) хранится
в БД и переиспользуется на commit Долями (позиции create/commit обязаны совпадать).
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import AppConfig  # noqa: E402
from app.database import Database  # noqa: E402
from app.dolyame import DolyameResult  # noqa: E402
from app.main import create_app  # noqa: E402
from app.shalamo import ShalamoResult  # noqa: E402
from app.tbank import InitResult, StateResult  # noqa: E402

SECRET_TOKEN = "A" * 64
EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.yaml"
)
ALLOWED_IP = "91.194.226.10"

GLOBAL_TAGS = {"card": "paid_card", "dolyami": "paid_dolyami"}
GLOBAL_FAIL_TAGS = {"dolyami": "fail_dolyami"}

# product_id, которого НЕТ в config.products (cart-режим)
CART_PID = "sub_yaest"
CART_NAME = "Подписка Я Есть. Погружение в пробуждение"


def make_config() -> AppConfig:
    raw = yaml.safe_load(open(EXAMPLE, encoding="utf-8"))
    raw["server"]["secret_token"] = SECRET_TOKEN
    raw["server"]["public_url"] = "https://test.local"
    raw["tbank"]["terminal_key"] = "TestKey"
    raw["tbank"]["terminal_password"] = "testpw"
    raw["shalamo"]["api_key"] = "shkey"
    raw["dolyame"] = {
        "base_url": "https://partner.dolyame.test",
        "login": "lg", "password": "pw",
        "commit_on_webhook": True, "fiscalization": "enabled",
    }
    raw["payment_methods"]["dolyami"]["provider"] = "dolyame"
    raw["tags_by_method"] = dict(GLOBAL_TAGS)
    raw["fail_tags_by_method"] = dict(GLOBAL_FAIL_TAGS)
    return AppConfig.model_validate(raw)


class FakeTBank:
    def __init__(self) -> None:
        self.init_calls: list[dict] = []

    async def init_payment(
        self, order_id, amount, description,
        notification_url=None, extra_params=None, receipt=None,
    ) -> InitResult:
        self.init_calls.append(
            {"order_id": order_id, "amount": amount, "description": description,
             "receipt": receipt}
        )
        return InitResult(
            success=True, payment_id=f"pay_{order_id[-6:]}",
            pay_url=f"https://securepay.test/{order_id}",
        )

    async def get_state(self, payment_id) -> StateResult:
        return StateResult(success=True, status="CONFIRMED", amount=15000)


class FakeShalamo:
    def __init__(self) -> None:
        self.assign_ok = True
        self.calls: list[tuple] = []

    async def set_variables(self, contact_id, variables) -> ShalamoResult:
        self.calls.append(("vars", contact_id, variables))
        return ShalamoResult(ok=True)

    async def assign_tag(self, contact_id, tag) -> ShalamoResult:
        self.calls.append(("tag", contact_id, tag))
        return ShalamoResult(ok=self.assign_ok, error=None if self.assign_ok else "HTTP 500")

    @property
    def tag_calls(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "tag"]


class FakeDolyame:
    def __init__(self) -> None:
        self.status = "wait_for_commit"
        self.amount = Decimal("150.00")
        self.create_calls: list[dict] = []
        self.info_calls: list[str] = []
        self.commit_calls: list[str] = []
        self.commit_items: list[dict] = []

    async def create(self, order_id, amount_kopecks, items, client_info=None,
                     notification_url=None, success_url=None, fail_url=None) -> DolyameResult:
        self.create_calls.append({"order_id": order_id, "items": items})
        return DolyameResult(success=True, status="new", order_id=order_id,
                             link=f"https://dolyame.test/{order_id}")

    async def info(self, order_id) -> DolyameResult:
        self.info_calls.append(order_id)
        return DolyameResult(success=True, status=self.status, amount=self.amount, order_id=order_id)

    async def commit(self, order_id, amount_kopecks, items) -> DolyameResult:
        self.commit_calls.append(order_id)
        self.commit_items = items
        self.status = "committed"
        return DolyameResult(success=True, status="committed", amount=self.amount, order_id=order_id)


def _make_env(cfg, tmp_path) -> SimpleNamespace:
    db = Database(str(tmp_path / "test.db"))
    db.init_db()
    tbank, shalamo, dolyame = FakeTBank(), FakeShalamo(), FakeDolyame()
    app = create_app(config=cfg, db=db, tbank=tbank, shalamo=shalamo, dolyame=dolyame)
    return SimpleNamespace(
        cfg=cfg, db=db, tbank=tbank, shalamo=shalamo, dolyame=dolyame,
        client=TestClient(app), secret=SECRET_TOKEN,
    )


@pytest.fixture
def env(tmp_path):
    return _make_env(make_config(), tmp_path)


def _post(env, **over):
    body = {"contact_id": "c1", "product_id": CART_PID, "payment_method": "card",
            "amount": 15000, "cart": CART_NAME}
    body.update(over)
    return env.client.post("/init-payment", json=body, headers={"X-Secret-Token": env.secret})


# ── cart-режим: карта (эквайринг Т-Банка) ────────────────────────────────────


def test_cart_card_created_with_global_tag_and_item_name(env):
    r = _post(env)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "created"
    row = env.db.get_by_order_id(r.json()["order_id"])
    assert row["product_id"] == CART_PID          # ключ дедупликации — от платформы
    assert row["tag_name"] == "paid_card"          # глобальный тег по способу
    assert row["item_name"] == CART_NAME           # имя позиции из cart сохранено
    # описание в Init Т-Банка — имя из cart (товара на сервере нет)
    assert env.tbank.init_calls[-1]["description"] == CART_NAME


def test_cart_method_without_global_tag_rejected(env):
    # sbp есть в payment_methods, но в глобальном tags_by_method его нет.
    r = _post(env, payment_method="sbp")
    assert r.status_code == 400
    assert r.json()["status"] == "invalid_payment_method"


def test_cart_unknown_method_rejected(env):
    r = _post(env, payment_method="nonexistent")
    assert r.status_code == 400
    assert r.json()["status"] == "invalid_payment_method"


# ── cart-режим: Долями (имя позиции совпадает на create и commit) ─────────────


def _webhook(env, order_id, status="wait_for_commit"):
    return env.client.post(
        "/webhook/dolyame",
        json={"id": order_id, "status": status, "amount": 150.0},
        headers={"X-Real-IP": ALLOWED_IP},
    )


def test_cart_dolyame_create_commit_name_match(env):
    r = _post(env, payment_method="dolyami")
    assert r.status_code == 200, r.text
    order_id = r.json()["order_id"]
    created_item = env.dolyame.create_calls[0]["items"][0]
    assert created_item["name"] == CART_NAME       # позиция create = cart

    wr = _webhook(env, order_id)
    assert wr.status_code == 200 and wr.text == "OK"
    assert env.dolyame.commit_calls == [order_id]
    committed_item = env.dolyame.commit_items[0]
    # Долями требует совпадения позиций create/commit — имя берётся из item_name в БД
    assert committed_item["name"] == CART_NAME
    assert committed_item["name"] == created_item["name"]
    row = env.db.get_by_order_id(order_id)
    assert row["tag_assigned_at"] is not None
    assert env.shalamo.tag_calls == [("tag", "c1", "paid_dolyami")]  # глобальный тег


def test_cart_dolyame_negative_assigns_global_fail_tag(env):
    r = _post(env, payment_method="dolyami")
    order_id = r.json()["order_id"]
    env.dolyame.status = "rejected"
    wr = _webhook(env, order_id, status="rejected")
    assert wr.status_code == 200 and wr.text == "OK"
    row = env.db.get_by_order_id(order_id)
    assert row["status"] == "failed"
    assert row["tag_assigned_at"] is None
    assert row["fail_tag_assigned_at"] is not None
    assert env.shalamo.tag_calls == [("tag", "c1", "fail_dolyami")]  # глобальный тег отказа


# ── cart-режим выключен (нет глобальных тегов) → неизвестный товар ────────────


def test_cart_mode_off_unknown_product_is_invalid_product(tmp_path):
    cfg = make_config()
    cfg.tags_by_method.clear()  # глобальные теги не заданы → cart-режим выключен
    env = _make_env(cfg, tmp_path)
    r = _post(env)
    assert r.status_code == 400
    assert r.json()["status"] == "invalid_product"
