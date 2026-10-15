"""Подстраховка (инцидент shalamo-401): фоновый реконсилятор «оплачено, но тег не
назначен», CRITICAL-сигнал и деградация /health.
См. docs/incidents/2026-06-28-shalamo-auth-401.md."""

from __future__ import annotations

import logging
import os
import time

import yaml

from app.config import AppConfig
from app.database import Database
from app.shalamo import ShalamoResult

EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.yaml"
)


def _cfg(**shalamo_over) -> AppConfig:
    raw = yaml.safe_load(open(EXAMPLE, encoding="utf-8"))
    raw["server"]["secret_token"] = "A" * 64
    raw["server"]["public_url"] = "https://test.local"
    raw["tbank"]["terminal_key"] = "TestKey"
    raw["tbank"]["terminal_password"] = "testpw"
    raw["shalamo"]["api_key"] = "shkey"
    raw["shalamo"].update(shalamo_over)
    return AppConfig.model_validate(raw)


def _add_stranded(db: Database, order_id: str = "o1", contact: str = "c1") -> None:
    """Оплаченный заказ без тега: paid_at стоит, tag_assigned_at пуст."""
    db.create_payment(order_id, contact, "course_basic", "card", 9900, "paid_card_basic")
    db.mark_paid(order_id)


# ── unit: get_paid_untagged_orders ───────────────────────────────────────────

def test_get_paid_untagged_orders_filters(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.init_db()
    _add_stranded(db, "paid_untagged")
    # оплачен И тег уже назначен — не должен попадать
    _add_stranded(db, "paid_tagged", "c2")
    db.atomic_capture("paid_tagged")
    db.mark_tag_assigned("paid_tagged")
    # не оплачен — не должен попадать
    db.create_payment("unpaid", "c3", "course_basic", "card", 9900, "paid_card_basic")

    ids = {o["order_id"] for o in db.get_paid_untagged_orders(3600)}
    assert ids == {"paid_untagged"}

    # max_age=0 -> только что оплаченный уже вне окна
    assert db.get_paid_untagged_orders(0) == []


# ── интеграция: фоновый реконсилятор ─────────────────────────────────────────

def test_reconciler_assigns_stranded_tag(env_factory):
    """reconcile_interval_seconds > 0 -> фоновая задача сама добивает застрявший тег."""
    env = env_factory(_cfg(reconcile_interval_seconds=0.02))
    _add_stranded(env.db)

    with env.client:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if env.db.get_by_order_id("o1")["tag_assigned_at"]:
                break
            time.sleep(0.02)

    row = env.db.get_by_order_id("o1")
    assert row["tag_assigned_at"] is not None
    assert ("tag", "c1", "paid_card_basic") in env.shalamo.tag_calls


def test_reconciler_disabled_by_default(env_factory):
    """reconcile_interval_seconds=0 -> задача не запускается, тег не трогается."""
    env = env_factory(_cfg(reconcile_interval_seconds=0))
    _add_stranded(env.db)
    with env.client:
        time.sleep(0.2)
    assert env.db.get_by_order_id("o1")["tag_assigned_at"] is None
    assert env.shalamo.tag_calls == []


def test_reconciler_critical_when_shalamo_down(env_factory):
    """Когда shalamo недоступен (assign падает), застрявший остаётся без тега и
    пишется агрегированный CRITICAL-сигнал тревоги."""
    env = env_factory(
        _cfg(reconcile_interval_seconds=0.02, stranded_alert_after_seconds=0)
    )
    env.shalamo.assign_ok = False
    _add_stranded(env.db)

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger("tbank_proxy")
    logger.addHandler(handler)
    try:
        with env.client:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if any(r.levelno == logging.CRITICAL for r in records):
                    break
                time.sleep(0.02)
    finally:
        logger.removeHandler(handler)

    assert env.db.get_by_order_id("o1")["tag_assigned_at"] is None
    crit = [r for r in records if r.levelno == logging.CRITICAL]
    assert crit, "ожидался CRITICAL-сигнал о застрявших оплатах"
    assert "СТРАХОВКА" in crit[0].getMessage()


def test_grant_access_skips_double_assign_when_tag_set_concurrently(env_factory):
    """Регрессия на гонку reconciler ↔ webhook-ретрай при восстановлении shalamo.
    Под локом grant_access перепроверяет tag_assigned_at: если параллельный путь
    назначил тег (симулируем во время set_variables) — assign_tag НЕ вызывается,
    иначе двойной тег = двойная авторассылка живому клиенту."""
    env = env_factory(_cfg(reconcile_interval_seconds=0.02))
    _add_stranded(env.db)

    async def vars_then_concurrent_assign(contact_id, variables):
        # другой путь назначил тег ровно пока мы ставим переменные
        env.db.atomic_capture("o1")
        env.db.mark_tag_assigned("o1")
        return ShalamoResult(ok=True)

    env.shalamo.set_variables = vars_then_concurrent_assign

    with env.client:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if env.db.get_by_order_id("o1")["tag_assigned_at"]:
                break
            time.sleep(0.02)

    assert env.db.get_by_order_id("o1")["tag_assigned_at"] is not None
    assert env.shalamo.tag_calls == []  # assign_tag НЕ вызван — двойной рассылки нет


# ── /health деградация ───────────────────────────────────────────────────────

def test_health_degraded_when_stranded(env_factory):
    env = env_factory(
        _cfg(reconcile_interval_seconds=0, stranded_alert_after_seconds=0)
    )
    assert env.client.get("/health").json()["status"] == "ok"

    _add_stranded(env.db)
    body = env.client.get("/health").json()
    assert body["status"] == "degraded"
    assert body["stranded"] == 1
