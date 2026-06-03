"""Общие фикстуры тестов: валидный конфиг, временная БД, фейковые клиенты."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import AppConfig  # noqa: E402
from app.database import Database  # noqa: E402
from app.main import create_app  # noqa: E402
from app.shalamo import ShalamoResult  # noqa: E402
from app.tbank import InitResult, StateResult, build_token  # noqa: E402

TEST_PASSWORD = "testpw"
SECRET_TOKEN = "A" * 64
EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.yaml"
)


def make_config() -> AppConfig:
    raw = yaml.safe_load(open(EXAMPLE, encoding="utf-8"))
    raw["server"]["secret_token"] = SECRET_TOKEN
    raw["server"]["public_url"] = "https://test.local"
    raw["tbank"]["terminal_key"] = "TestKey"
    raw["tbank"]["terminal_password"] = TEST_PASSWORD
    raw["shalamo"]["api_key"] = "shkey"
    return AppConfig.model_validate(raw)


class FakeTBank:
    def __init__(self) -> None:
        self.init_succeeds = True
        self.state_result = StateResult(success=True, status="CONFIRMED", amount=9900)
        self.init_calls: list[dict] = []

    async def init_payment(
        self, order_id, amount, description,
        notification_url=None, extra_params=None, receipt=None
    ) -> InitResult:
        self.init_calls.append(
            {"order_id": order_id, "amount": amount, "receipt": receipt}
        )
        if self.init_succeeds:
            return InitResult(
                success=True,
                payment_id=f"pay_{order_id[-6:]}",
                pay_url=f"https://securepay.test/{order_id}",
            )
        return InitResult(success=False, error_code="99", message="declined")

    async def get_state(self, payment_id) -> StateResult:
        return self.state_result


class FakeShalamo:
    def __init__(self) -> None:
        self.assign_ok = True
        self.vars_ok = True
        self.calls: list[tuple] = []

    async def set_variables(self, contact_id, variables) -> ShalamoResult:
        self.calls.append(("vars", contact_id, variables))
        return ShalamoResult(ok=self.vars_ok, error=None if self.vars_ok else "HTTP 500")

    async def assign_tag(self, contact_id, tag) -> ShalamoResult:
        self.calls.append(("tag", contact_id, tag))
        return ShalamoResult(ok=self.assign_ok, error=None if self.assign_ok else "HTTP 500")

    @property
    def tag_calls(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "tag"]


def _make_env(cfg: AppConfig, tmp_path) -> SimpleNamespace:
    db = Database(str(tmp_path / "test.db"))
    db.init_db()
    tbank = FakeTBank()
    shalamo = FakeShalamo()
    app = create_app(config=cfg, db=db, tbank=tbank, shalamo=shalamo)
    client = TestClient(app)
    return SimpleNamespace(
        cfg=cfg, db=db, tbank=tbank, shalamo=shalamo, client=client,
        password=TEST_PASSWORD, secret=SECRET_TOKEN,
    )


@pytest.fixture
def env(tmp_path):
    return _make_env(make_config(), tmp_path)


@pytest.fixture
def env_factory(tmp_path):
    """Фабрика окружения с произвольным конфигом (для тестов чека и т.п.)."""
    def _factory(cfg: AppConfig) -> SimpleNamespace:
        return _make_env(cfg, tmp_path)
    return _factory


def signed_webhook(payload: dict, password: str = TEST_PASSWORD) -> dict:
    """Добавить корректную подпись Token к webhook-полезной нагрузке."""
    body = dict(payload)
    body["Token"] = build_token(body, password)
    return body
