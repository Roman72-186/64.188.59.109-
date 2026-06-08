"""Тесты прямого Долями: маршрутизация /init-payment, /webhook/dolyame, двухфазность.

Всё на моках (DolyameClient/ShalamoClient подменяются). Покрыто:
  • роутинг провайдера (provider: dolyame -> прямой API, не эквайринг Т-Банка);
  • commit на wait_for_commit -> тег; идемпотентность (commit ровно один раз);
  • 503 при сбое shalamo и повторная обработка;
  • IP-allowlist webhook (подсеть Долями);
  • сверка суммы, терминальный отказ, commit_on_webhook=false;
  • конвертация копейки<->рубли и инвариант суммы позиции.
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
from app.dolyame import DolyameResult, build_item, kopecks_to_rubles  # noqa: E402
from app.main import create_app  # noqa: E402
from app.shalamo import ShalamoResult  # noqa: E402

SECRET_TOKEN = "A" * 64
EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.yaml"
)
ALLOWED_IP = "91.194.226.10"  # внутри 91.194.226.0/23


def make_config(commit_on_webhook: bool = True) -> AppConfig:
    raw = yaml.safe_load(open(EXAMPLE, encoding="utf-8"))
    raw["server"]["secret_token"] = SECRET_TOKEN
    raw["server"]["public_url"] = "https://test.local"
    raw["tbank"]["terminal_key"] = "TestKey"
    raw["tbank"]["terminal_password"] = "testpw"
    raw["shalamo"]["api_key"] = "shkey"
    raw["dolyame"] = {
        "base_url": "https://partner.dolyame.test",
        "login": "lg",
        "password": "pw",
        "commit_on_webhook": commit_on_webhook,
    }
    raw["payment_methods"]["dolyami"]["provider"] = "dolyame"
    return AppConfig.model_validate(raw)


class FakeShalamo:
    def __init__(self) -> None:
        self.assign_ok = True
        self.vars_ok = True
        self.calls: list[tuple] = []

    async def set_variables(self, contact_id, variables) -> ShalamoResult:
        self.calls.append(("vars", contact_id, variables))
        return ShalamoResult(ok=self.vars_ok)

    async def assign_tag(self, contact_id, tag) -> ShalamoResult:
        self.calls.append(("tag", contact_id, tag))
        return ShalamoResult(ok=self.assign_ok, error=None if self.assign_ok else "HTTP 500")

    @property
    def tag_calls(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "tag"]


class FakeDolyame:
    """Мок прямого Долями с моделью статуса: commit переводит wait_for_commit->committed."""

    def __init__(self) -> None:
        self.create_succeeds = True
        self.info_succeeds = True
        self.commit_succeeds = True
        self.status = "wait_for_commit"
        self.amount = Decimal("99.00")
        self.create_calls: list[dict] = []
        self.info_calls: list[str] = []
        self.commit_calls: list[str] = []

    async def create(
        self, order_id, amount_kopecks, items,
        client_info=None, notification_url=None, success_url=None, fail_url=None,
    ) -> DolyameResult:
        self.create_calls.append(
            {"order_id": order_id, "amount_kopecks": amount_kopecks, "items": items}
        )
        if self.create_succeeds:
            return DolyameResult(
                success=True, status="new", order_id=order_id,
                link=f"https://dolyame.test/{order_id}",
            )
        return DolyameResult(success=False, error_code="422", message="rejected", order_id=order_id)

    async def info(self, order_id) -> DolyameResult:
        self.info_calls.append(order_id)
        if not self.info_succeeds:
            return DolyameResult(success=False, error_code="500", order_id=order_id)
        return DolyameResult(
            success=True, status=self.status, amount=self.amount, order_id=order_id
        )

    async def commit(self, order_id, amount_kopecks, items) -> DolyameResult:
        self.commit_calls.append(order_id)
        if not self.commit_succeeds:
            return DolyameResult(success=False, error_code="409", order_id=order_id)
        self.status = "committed"  # переход статуса: следующий info вернёт committed
        return DolyameResult(
            success=True, status="committed", amount=self.amount, order_id=order_id
        )


def _make_env(cfg: AppConfig, tmp_path) -> SimpleNamespace:
    db = Database(str(tmp_path / "test.db"))
    db.init_db()
    shalamo = FakeShalamo()
    dolyame = FakeDolyame()
    app = create_app(config=cfg, db=db, shalamo=shalamo, dolyame=dolyame)
    return SimpleNamespace(
        cfg=cfg, db=db, shalamo=shalamo, dolyame=dolyame,
        client=TestClient(app), secret=SECRET_TOKEN,
    )


@pytest.fixture
def env(tmp_path):
    return _make_env(make_config(), tmp_path)


BASIC = {"contact_id": "c1", "product_id": "course_basic", "payment_method": "dolyami"}


def _init_order(env) -> dict:
    r = env.client.post("/init-payment", json=BASIC, headers={"X-Secret-Token": env.secret})
    assert r.status_code == 200, r.text
    return env.db.get_by_order_id(r.json()["order_id"])


def _webhook(env, order_id, ip=ALLOWED_IP, status=None):
    body = {"id": order_id, "status": status or "wait_for_commit", "amount": 99.0}
    # X-Real-IP — доверенный заголовок (nginx ставит $remote_addr); им и проверяем allowlist.
    return env.client.post(
        "/webhook/dolyame", json=body, headers={"X-Real-IP": ip}
    )


# ── /init-payment роутинг ────────────────────────────────────────────────────


def test_init_routes_to_dolyame(env):
    r = env.client.post("/init-payment", json=BASIC, headers={"X-Secret-Token": env.secret})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "created"
    assert data["pay_url"].startswith("https://dolyame.test/")
    assert len(env.dolyame.create_calls) == 1
    # link сохранён как pay_url
    assert env.db.get_by_order_id(data["order_id"])["pay_url"] == data["pay_url"]


def test_init_dolyame_create_failure_502(env):
    env.dolyame.create_succeeds = False
    r = env.client.post("/init-payment", json=BASIC, headers={"X-Secret-Token": env.secret})
    assert r.status_code == 502
    assert r.json()["status"] == "payment_creation_failed"


# ── webhook: двухфазность и тег ──────────────────────────────────────────────


def test_webhook_commit_then_tag(env):
    order = _init_order(env)
    r = _webhook(env, order["order_id"])
    assert r.status_code == 200 and r.text == "OK"
    assert env.dolyame.commit_calls == [order["order_id"]]
    row = env.db.get_by_order_id(order["order_id"])
    assert row["status"] == "confirmed"
    assert row["tag_assigned_at"] is not None
    assert env.shalamo.tag_calls == [("tag", "c1", "paid_dolyami_basic")]


def test_webhook_commit_called_once_on_duplicate(env):
    order = _init_order(env)
    _webhook(env, order["order_id"])
    _webhook(env, order["order_id"])  # дубль
    # commit ровно один раз (на втором webhook статус уже committed)
    assert env.dolyame.commit_calls == [order["order_id"]]
    assert len(env.shalamo.tag_calls) == 1


def test_webhook_503_then_reprocess_commits_once(env):
    order = _init_order(env)
    env.shalamo.assign_ok = False
    r = _webhook(env, order["order_id"])
    assert r.status_code == 503
    row = env.db.get_by_order_id(order["order_id"])
    assert row["tag_assigned_at"] is None
    assert row["paid_at"] is not None  # оплата зафиксирована после commit

    env.shalamo.assign_ok = True
    r2 = _webhook(env, order["order_id"])
    assert r2.status_code == 200 and r2.text == "OK"
    assert env.db.get_by_order_id(order["order_id"])["tag_assigned_at"] is not None
    # commit НЕ продублирован при повторной обработке
    assert env.dolyame.commit_calls == [order["order_id"]]


def test_webhook_commit_disabled_grants_on_hold(tmp_path):
    env = _make_env(make_config(commit_on_webhook=False), tmp_path)
    order = _init_order(env)
    r = _webhook(env, order["order_id"])
    assert r.status_code == 200 and r.text == "OK"
    assert env.dolyame.commit_calls == []  # commit не вызывался
    assert env.db.get_by_order_id(order["order_id"])["tag_assigned_at"] is not None


# ── webhook: безопасность и валидация ────────────────────────────────────────


def test_webhook_forbidden_ip(env):
    order = _init_order(env)
    r = _webhook(env, order["order_id"], ip="8.8.8.8")
    assert r.status_code == 403
    assert env.dolyame.info_calls == []  # до /info не дошли
    assert env.db.get_by_order_id(order["order_id"])["tag_assigned_at"] is None


def test_webhook_amount_mismatch_no_tag(env):
    order = _init_order(env)
    env.dolyame.amount = Decimal("1.00")  # /info вернёт не ту сумму
    r = _webhook(env, order["order_id"])
    assert r.status_code == 200 and r.text == "OK"
    assert env.db.get_by_order_id(order["order_id"])["tag_assigned_at"] is None
    assert env.dolyame.commit_calls == []  # холд не захватываем при неверной сумме


def test_webhook_rejected_status_fails(env):
    order = _init_order(env)
    env.dolyame.status = "rejected"
    r = _webhook(env, order["order_id"], status="rejected")
    assert r.status_code == 200 and r.text == "OK"
    row = env.db.get_by_order_id(order["order_id"])
    assert row["tag_assigned_at"] is None
    assert row["status"] == "failed"
    assert env.dolyame.commit_calls == []


def test_webhook_unknown_order_ok(env):
    r = _webhook(env, "ghost-order")
    assert r.status_code == 200 and r.text == "OK"
    assert env.dolyame.info_calls == []  # неизвестный заказ — /info не зовём


def test_webhook_info_unavailable_503(env):
    order = _init_order(env)
    env.dolyame.info_succeeds = False
    r = _webhook(env, order["order_id"])
    assert r.status_code == 503


# ── конфиг ───────────────────────────────────────────────────────────────────


def test_provider_dolyame_requires_block():
    raw = yaml.safe_load(open(EXAMPLE, encoding="utf-8"))
    raw["server"]["secret_token"] = SECRET_TOKEN
    raw["server"]["public_url"] = "https://x"
    raw["tbank"]["terminal_key"] = "K"
    raw["tbank"]["terminal_password"] = "P"
    raw["shalamo"]["api_key"] = "k"
    raw["payment_methods"]["dolyami"]["provider"] = "dolyame"  # без блока dolyame
    with pytest.raises(Exception) as exc:
        AppConfig.model_validate(raw)
    assert "dolyame" in str(exc.value)


# ── юниты конвертации ────────────────────────────────────────────────────────


def test_kopecks_to_rubles():
    assert kopecks_to_rubles(9900) == Decimal("99.00")
    assert kopecks_to_rubles(9999) == Decimal("99.99")
    assert kopecks_to_rubles(1) == Decimal("0.01")


def test_build_item_invariant():
    item = build_item("Курс", 9900, quantity=1)
    assert item["price"] == 99.0 and item["quantity"] == 1
    # amount == Σ(quantity*price)
    assert Decimal(str(item["price"])) * item["quantity"] == kopecks_to_rubles(9900)
