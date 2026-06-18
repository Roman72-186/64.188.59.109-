"""Тесты Credit Broker (tbank_credit): /init-payment, /webhook/tbank_credit,
авто-апгрейд по порогу суммы, commit_on_webhook, IP-allowlist, идемпотентность.
"""

from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import AppConfig  # noqa: E402
from app.database import Database  # noqa: E402
from app.main import create_app  # noqa: E402
from app.shalamo import ShalamoResult  # noqa: E402
from app.tbank_credit import CreditResult  # noqa: E402

SECRET_TOKEN = "A" * 64
EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.yaml"
)
ALLOWED_IP = "10.0.0.1"
ALLOWED_SUBNET = "10.0.0.0/24"


def make_config(
    commit_on_webhook: bool = False,
    webhook_allowed_subnet: str = "",
    credit_threshold_kopecks: int | None = None,
    poll_interval_seconds: float = 0,
) -> AppConfig:
    raw = yaml.safe_load(open(EXAMPLE, encoding="utf-8"))
    raw["server"]["secret_token"] = SECRET_TOKEN
    raw["server"]["public_url"] = "https://test.local"
    raw["tbank"]["terminal_key"] = "TestKey"
    raw["tbank"]["terminal_password"] = "testpw"
    raw["shalamo"]["api_key"] = "shkey"
    raw["tbank_credit"] = {
        "shop_id": "shop1",
        "showcase_id": "showcase1",
        "api_password": "pw",
        "promo_code": "promo1",
        "commit_on_webhook": commit_on_webhook,
        "webhook_allowed_subnet": webhook_allowed_subnet,
        "poll_interval_seconds": poll_interval_seconds,
    }
    raw["payment_methods"]["credit"] = {
        "label": "Кредит",
        "provider": "tbank_credit",
    }
    raw["products"]["course_basic"]["payment_methods"].append("credit")
    raw["products"]["course_basic"]["tags_by_method"]["credit"] = "paid_credit_basic"
    if credit_threshold_kopecks is not None:
        raw["credit_threshold_kopecks"] = credit_threshold_kopecks
    return AppConfig.model_validate(raw)


class FakeShalamo:
    def __init__(self) -> None:
        self.assign_ok = True
        self.calls: list[tuple] = []

    async def set_variables(self, contact_id, variables) -> ShalamoResult:
        self.calls.append(("vars", contact_id, variables))
        return ShalamoResult(ok=True)

    async def assign_tag(self, contact_id, tag) -> ShalamoResult:
        self.calls.append(("tag", contact_id, tag))
        return ShalamoResult(ok=self.assign_ok, error=None if self.assign_ok else "err")

    @property
    def tag_calls(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "tag"]


class FakeCredit:
    def __init__(self) -> None:
        self.create_succeeds = True
        self.info_succeeds = True
        self.commit_succeeds = True
        self.info_status = "signed"
        self.create_calls: list[dict] = []
        self.info_calls: list[str] = []
        self.commit_calls: list[str] = []

    async def create(self, order_id, amount_kopecks, items, customer_info=None, webhook_url=None, promo_code=None) -> CreditResult:
        self.create_calls.append({
            "order_id": order_id, "amount_kopecks": amount_kopecks,
            "promo_code": promo_code, "webhook_url": webhook_url,
        })
        if self.create_succeeds:
            return CreditResult(
                success=True,
                status="new",
                application_id="app-uuid-1",
                order_number=order_id,
                link=f"https://forma.tbank.test/{order_id}",
            )
        return CreditResult(success=False, error_code="400", message="rejected")

    async def info(self, order_number: str) -> CreditResult:
        self.info_calls.append(order_number)
        if not self.info_succeeds:
            return CreditResult(success=False, message="timeout")
        return CreditResult(success=True, status=self.info_status, order_number=order_number)

    async def commit(self, order_number: str) -> CreditResult:
        self.commit_calls.append(order_number)
        if not self.commit_succeeds:
            return CreditResult(success=False, message="commit failed")
        return CreditResult(success=True, status="committed", order_number=order_number)


def _make_env(cfg: AppConfig, tmp_path) -> SimpleNamespace:
    db = Database(str(tmp_path / "test.db"))
    db.init_db()
    credit = FakeCredit()
    shalamo = FakeShalamo()
    app = create_app(config=cfg, db=db, shalamo=shalamo, tbank_credit=credit)
    client = TestClient(app)
    return SimpleNamespace(cfg=cfg, db=db, credit=credit, shalamo=shalamo, client=client)


# ── /init-payment с provider=tbank_credit ─────────────────────────────────────

def test_credit_init_returns_link(tmp_path):
    env = _make_env(make_config(), tmp_path)
    r = env.client.post(
        "/init-payment",
        json={"contact_id": "c1", "product_id": "course_basic",
              "payment_method": "credit", "amount": 5000000},
        headers={"X-Secret-Token": SECRET_TOKEN},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "created"
    assert "forma.tbank.test" in body["pay_url"]
    assert len(env.credit.create_calls) == 1
    assert env.credit.create_calls[0]["amount_kopecks"] == 5000000


def test_credit_init_fails_when_create_fails(tmp_path):
    env = _make_env(make_config(), tmp_path)
    env.credit.create_succeeds = False
    r = env.client.post(
        "/init-payment",
        json={"contact_id": "c1", "product_id": "course_basic",
              "payment_method": "credit", "amount": 5000000},
        headers={"X-Secret-Token": SECRET_TOKEN},
    )
    assert r.status_code == 502
    assert r.json()["status"] == "payment_creation_failed"


def test_credit_init_does_not_send_webhook_url(tmp_path):
    """Create вызывается БЕЗ webhookURL: Т-Банк отклоняет Create, если домен
    webhookURL не совпадает с доменом витрины клиента (не доменом прокладки).
    Статус заявки получаем через GET /info — webhook (если домен совпадёт)
    и/или фоновый опрос (poll_interval_seconds)."""
    env = _make_env(make_config(), tmp_path)
    env.client.post(
        "/init-payment",
        json={"contact_id": "c1", "product_id": "course_basic",
              "payment_method": "credit", "amount": 5000000},
        headers={"X-Secret-Token": SECRET_TOKEN},
    )
    assert env.credit.create_calls[0]["webhook_url"] is None


def test_credit_init_uses_default_promo_code(tmp_path):
    """Способ без promo_code -> promoCode берётся из tbank_credit.promo_code."""
    env = _make_env(make_config(), tmp_path)
    env.client.post(
        "/init-payment",
        json={"contact_id": "c1", "product_id": "course_basic",
              "payment_method": "credit", "amount": 5000000},
        headers={"X-Secret-Token": SECRET_TOKEN},
    )
    assert env.credit.create_calls[0]["promo_code"] == "promo1"


def test_credit_init_uses_method_promo_code_override(tmp_path):
    """Способ с promo_code -> переопределяет tbank_credit.promo_code (напр.
    разные сроки рассрочки = разные продукты в ЛК)."""
    cfg = make_config()
    raw = cfg.model_dump()
    raw["payment_methods"]["installment_3"] = {
        "label": "Рассрочка на 3 месяца",
        "provider": "tbank_credit",
        "promo_code": "installment_0_0_3_3,4_1,7",
    }
    raw["products"]["course_basic"]["payment_methods"].append("installment_3")
    raw["products"]["course_basic"]["tags_by_method"]["installment_3"] = "paid_installment_basic"
    cfg = AppConfig.model_validate(raw)

    env = _make_env(cfg, tmp_path)
    env.client.post(
        "/init-payment",
        json={"contact_id": "c1", "product_id": "course_basic",
              "payment_method": "installment_3", "amount": 5000000},
        headers={"X-Secret-Token": SECRET_TOKEN},
    )
    assert env.credit.create_calls[0]["promo_code"] == "installment_0_0_3_3,4_1,7"


def test_promo_code_requires_tbank_credit_provider(tmp_path):
    """promo_code на способе с provider != tbank_credit -> ошибка конфига."""
    cfg = make_config()
    raw = cfg.model_dump()
    raw["payment_methods"]["card"]["promo_code"] = "should_not_be_here"
    with pytest.raises(ValueError):
        AppConfig.model_validate(raw)


# ── авто-апгрейд по порогу ────────────────────────────────────────────────────

def test_auto_upgrade_above_threshold(tmp_path):
    """amount >= threshold → автоматически использует credit-метод."""
    env = _make_env(make_config(credit_threshold_kopecks=3000000), tmp_path)
    r = env.client.post(
        "/init-payment",
        json={"contact_id": "c1", "product_id": "course_basic",
              "payment_method": "card", "amount": 3000000},
        headers={"X-Secret-Token": SECRET_TOKEN},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "created"
    assert len(env.credit.create_calls) == 1  # пошло через Credit Broker


def test_no_upgrade_below_threshold(tmp_path):
    """amount < threshold → обычный способ, Credit Broker не вызывается."""
    env = _make_env(make_config(credit_threshold_kopecks=3000000), tmp_path)
    from app.tbank import InitResult
    from app.shalamo import ShalamoResult

    class FakeTBank:
        async def init_payment(self, **_) -> InitResult:
            return InitResult(success=True, payment_id="p1", pay_url="https://tbank.test/p1")
        async def get_state(self, _):
            pass

    app = create_app(
        config=env.cfg, db=env.db, tbank=FakeTBank(),
        shalamo=env.shalamo, tbank_credit=env.credit,
    )
    c = TestClient(app)
    r = c.post(
        "/init-payment",
        json={"contact_id": "c1", "product_id": "course_basic",
              "payment_method": "card", "amount": 2999999},
        headers={"X-Secret-Token": SECRET_TOKEN},
    )
    assert r.status_code == 200
    assert len(env.credit.create_calls) == 0  # Credit Broker не использовался


# ── /webhook/tbank_credit — успешный сценарий ─────────────────────────────────

def _create_credit_order(env: SimpleNamespace) -> str:
    r = env.client.post(
        "/init-payment",
        json={"contact_id": "c1", "product_id": "course_basic",
              "payment_method": "credit", "amount": 5000000},
        headers={"X-Secret-Token": SECRET_TOKEN},
    )
    return r.json()["order_id"]


def test_webhook_signed_assigns_tag(tmp_path):
    env = _make_env(make_config(), tmp_path)
    order_id = _create_credit_order(env)
    env.credit.info_status = "signed"

    r = env.client.post(
        "/webhook/tbank_credit",
        json={"orderNumber": order_id, "status": "signed"},
    )
    assert r.status_code == 200
    assert len(env.shalamo.tag_calls) == 1
    assert env.shalamo.tag_calls[0][2] == "paid_credit_basic"

    order = env.db.get_by_order_id(order_id)
    assert order["tag_assigned_at"] is not None
    assert order["paid_at"] is not None


def test_webhook_commit_on_webhook_true(tmp_path):
    """commit_on_webhook=True → Commit вызывается перед тегом."""
    env = _make_env(make_config(commit_on_webhook=True), tmp_path)
    order_id = _create_credit_order(env)
    env.credit.info_status = "signed"

    r = env.client.post(
        "/webhook/tbank_credit",
        json={"orderNumber": order_id, "status": "signed"},
    )
    assert r.status_code == 200
    assert len(env.credit.commit_calls) == 1
    assert env.credit.commit_calls[0] == order_id
    assert len(env.shalamo.tag_calls) == 1


def test_webhook_idempotent(tmp_path):
    """Повторный webhook — тег назначается только один раз."""
    env = _make_env(make_config(), tmp_path)
    order_id = _create_credit_order(env)
    env.credit.info_status = "signed"

    env.client.post("/webhook/tbank_credit", json={"orderNumber": order_id, "status": "signed"})
    env.client.post("/webhook/tbank_credit", json={"orderNumber": order_id, "status": "signed"})

    assert len(env.shalamo.tag_calls) == 1


def test_webhook_rejected_no_tag(tmp_path):
    """canceled/rejected → тег доступа не назначается."""
    env = _make_env(make_config(), tmp_path)
    order_id = _create_credit_order(env)
    env.credit.info_status = "rejected"

    r = env.client.post(
        "/webhook/tbank_credit",
        json={"orderNumber": order_id, "status": "rejected"},
    )
    assert r.status_code == 200
    assert len(env.shalamo.tag_calls) == 0
    order = env.db.get_by_order_id(order_id)
    assert order["status"] == "failed"


def test_webhook_rejected_assigns_fail_tag(tmp_path):
    """rejected (реальное отклонение банком) -> тег отказа (не тег доступа)."""
    cfg = make_config()
    raw = cfg.model_dump()
    raw["products"]["course_basic"]["fail_tags_by_method"]["credit"] = "fail_credit_basic"
    cfg = AppConfig.model_validate(raw)

    env = _make_env(cfg, tmp_path)
    order_id = _create_credit_order(env)
    env.credit.info_status = "rejected"

    r = env.client.post(
        "/webhook/tbank_credit",
        json={"orderNumber": order_id, "status": "rejected"},
    )
    assert r.status_code == 200
    order = env.db.get_by_order_id(order_id)
    assert order["status"] == "failed"
    assert order["tag_assigned_at"] is None
    assert order["fail_tag_assigned_at"] is not None
    assert len(env.shalamo.tag_calls) == 1


def test_webhook_canceled_does_not_assign_fail_tag(tmp_path):
    """canceled = заявка брошена/протухла: доступ не выдаём, но тег «оплата не
    прошла» НЕ ставим — клиент просто не довёл оформление."""
    cfg = make_config()
    raw = cfg.model_dump()
    raw["products"]["course_basic"]["fail_tags_by_method"]["credit"] = "fail_credit_basic"
    cfg = AppConfig.model_validate(raw)

    env = _make_env(cfg, tmp_path)
    order_id = _create_credit_order(env)
    env.credit.info_status = "canceled"

    r = env.client.post(
        "/webhook/tbank_credit",
        json={"orderNumber": order_id, "status": "canceled"},
    )
    assert r.status_code == 200
    order = env.db.get_by_order_id(order_id)
    assert order["status"] == "failed"
    assert order["tag_assigned_at"] is None
    assert order["fail_tag_assigned_at"] is None
    assert len(env.shalamo.tag_calls) == 0


def test_webhook_503_when_info_fails(tmp_path):
    """Если /info недоступен — 503, чтобы Credit Broker повторил webhook."""
    env = _make_env(make_config(), tmp_path)
    order_id = _create_credit_order(env)
    env.credit.info_succeeds = False

    r = env.client.post(
        "/webhook/tbank_credit",
        json={"orderNumber": order_id, "status": "signed"},
    )
    assert r.status_code == 503


def test_webhook_503_when_commit_fails(tmp_path):
    """commit_on_webhook=True, commit падает — 503."""
    env = _make_env(make_config(commit_on_webhook=True), tmp_path)
    order_id = _create_credit_order(env)
    env.credit.info_status = "signed"
    env.credit.commit_succeeds = False

    r = env.client.post(
        "/webhook/tbank_credit",
        json={"orderNumber": order_id, "status": "signed"},
    )
    assert r.status_code == 503
    assert len(env.shalamo.tag_calls) == 0


def test_webhook_503_when_shalamo_fails(tmp_path):
    """shalamo.assign_tag падает — 503."""
    env = _make_env(make_config(), tmp_path)
    order_id = _create_credit_order(env)
    env.credit.info_status = "signed"
    env.shalamo.assign_ok = False

    r = env.client.post(
        "/webhook/tbank_credit",
        json={"orderNumber": order_id, "status": "signed"},
    )
    assert r.status_code == 503


# ── IP-allowlist ───────────────────────────────────────────────────────────────

def test_webhook_ip_allowed(tmp_path):
    env = _make_env(make_config(webhook_allowed_subnet=ALLOWED_SUBNET), tmp_path)
    order_id = _create_credit_order(env)
    env.credit.info_status = "signed"

    r = env.client.post(
        "/webhook/tbank_credit",
        json={"orderNumber": order_id, "status": "signed"},
        headers={"X-Real-IP": ALLOWED_IP},
    )
    assert r.status_code == 200
    assert len(env.shalamo.tag_calls) == 1


def test_webhook_ip_blocked(tmp_path):
    env = _make_env(make_config(webhook_allowed_subnet=ALLOWED_SUBNET), tmp_path)
    order_id = _create_credit_order(env)

    r = env.client.post(
        "/webhook/tbank_credit",
        json={"orderNumber": order_id, "status": "signed"},
        headers={"X-Real-IP": "1.2.3.4"},
    )
    assert r.status_code == 403
    assert len(env.shalamo.tag_calls) == 0


def test_webhook_no_subnet_allows_all(tmp_path):
    """Если webhook_allowed_subnet пустой — IP не проверяется."""
    env = _make_env(make_config(webhook_allowed_subnet=""), tmp_path)
    order_id = _create_credit_order(env)
    env.credit.info_status = "signed"

    r = env.client.post(
        "/webhook/tbank_credit",
        json={"orderNumber": order_id, "status": "signed"},
        headers={"X-Real-IP": "1.2.3.4"},
    )
    assert r.status_code == 200
    assert len(env.shalamo.tag_calls) == 1


# ── повторная обработка после 503 (PRD §7.8) ─────────────────────────────────

def test_reprocess_after_503(tmp_path):
    """Первый webhook → 503 (shalamo упал); второй webhook → 200, тег назначен."""
    env = _make_env(make_config(), tmp_path)
    order_id = _create_credit_order(env)
    env.credit.info_status = "signed"

    env.shalamo.assign_ok = False
    r1 = env.client.post("/webhook/tbank_credit", json={"orderNumber": order_id})
    assert r1.status_code == 503

    env.shalamo.assign_ok = True
    r2 = env.client.post("/webhook/tbank_credit", json={"orderNumber": order_id})
    assert r2.status_code == 200
    assert len(env.shalamo.tag_calls) == 2  # 1 неудача + 1 успех
    assert env.db.get_by_order_id(order_id)["tag_assigned_at"] is not None


# ── фоновый опрос /info (poll_interval_seconds) ───────────────────────────────

def test_poll_assigns_tag_without_webhook(tmp_path):
    """poll_interval_seconds > 0 -> фоновая задача сама опрашивает /info и
    назначает тег без входящего /webhook/tbank_credit."""
    env = _make_env(make_config(poll_interval_seconds=0.02), tmp_path)
    order_id = _create_credit_order(env)
    env.credit.info_status = "signed"

    with env.client:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            order = env.db.get_by_order_id(order_id)
            if order["tag_assigned_at"]:
                break
            time.sleep(0.02)

    order = env.db.get_by_order_id(order_id)
    assert order["tag_assigned_at"] is not None
    assert order["paid_at"] is not None
    assert len(env.shalamo.tag_calls) == 1


def test_poll_assigns_fail_tag_for_rejected(tmp_path):
    """Поллер применяет ту же логику тега отказа, что и webhook."""
    cfg = make_config(poll_interval_seconds=0.02)
    raw = cfg.model_dump()
    raw["products"]["course_basic"]["fail_tags_by_method"]["credit"] = "fail_credit_basic"
    cfg = AppConfig.model_validate(raw)

    env = _make_env(cfg, tmp_path)
    order_id = _create_credit_order(env)
    env.credit.info_status = "rejected"

    with env.client:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            order = env.db.get_by_order_id(order_id)
            if order["fail_tag_assigned_at"]:
                break
            time.sleep(0.02)

    order = env.db.get_by_order_id(order_id)
    assert order["fail_tag_assigned_at"] is not None
    assert order["status"] == "failed"


def test_poll_disabled_by_default(tmp_path):
    """poll_interval_seconds=0 (по умолчанию) -> фоновая задача не запускается,
    статус заявки не меняется без входящего webhook."""
    env = _make_env(make_config(poll_interval_seconds=0), tmp_path)
    order_id = _create_credit_order(env)
    env.credit.info_status = "signed"

    with env.client:
        time.sleep(0.1)

    order = env.db.get_by_order_id(order_id)
    assert order["tag_assigned_at"] is None
    assert len(env.credit.info_calls) == 0
