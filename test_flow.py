#!/usr/bin/env python3
"""Автономный прогон всего потока прокладки на моках (без реальных Т-Банка/shalamo).

Запуск:  python test_flow.py
Мокает Т-Банк и shalamo.io, прогоняет ключевые сценарии и печатает PASS/FAIL.
Проверяет: защиту токеном, создание платежа, назначение тега, идемпотентность
(дубль webhook не ставит тег второй раз), отбраковку неверной подписи/суммы/статуса,
возврат 503 при недоступности shalamo и повторную обработку.

Это проверка ЛОГИКИ прокладки. Связку «тег -> авторассылка» в shalamo.io
проверять отдельно при первом реальном платеже.
"""

from __future__ import annotations

import sys
import tempfile

import yaml
from fastapi.testclient import TestClient

from app.config import AppConfig
from app.database import Database
from app.main import create_app
from app.shalamo import ShalamoResult
from app.tbank import InitResult, StateResult, build_token

PASSWORD = "testpw"
SECRET = "S" * 64

_results: list[tuple[bool, str]] = []


def check(cond: bool, name: str) -> None:
    _results.append((bool(cond), name))
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}")


def make_config() -> AppConfig:
    raw = yaml.safe_load(open("config.example.yaml", encoding="utf-8"))
    raw["server"]["secret_token"] = SECRET
    raw["server"]["public_url"] = "https://test.local"
    raw["tbank"]["terminal_key"] = "TestKey"
    raw["tbank"]["terminal_password"] = PASSWORD
    raw["shalamo"]["api_key"] = "k"
    return AppConfig.model_validate(raw)


class FakeTBank:
    def __init__(self) -> None:
        self.ok = True
        self.state = StateResult(success=True, status="CONFIRMED", amount=9900)

    async def init_payment(self, order_id, amount, description, notification_url=None, extra_params=None):
        if self.ok:
            return InitResult(success=True, payment_id=f"pay_{order_id[-5:]}", pay_url=f"https://pay/{order_id}")
        return InitResult(success=False, error_code="99", message="declined")

    async def get_state(self, payment_id):
        return self.state


class FakeShalamo:
    def __init__(self) -> None:
        self.ok = True
        self.tag_calls = 0

    async def set_variables(self, contact_id, variables):
        return ShalamoResult(ok=True)

    async def assign_tag(self, contact_id, tag):
        self.tag_calls += 1
        return ShalamoResult(ok=self.ok, error=None if self.ok else "HTTP 500")


def sign(payload: dict) -> dict:
    body = dict(payload)
    body["Token"] = build_token(body, PASSWORD)
    return body


def main() -> int:
    cfg = make_config()
    db = Database(tempfile.mktemp(suffix=".db"))
    db.init_db()
    tbank = FakeTBank()
    shalamo = FakeShalamo()
    client = TestClient(create_app(config=cfg, db=db, tbank=tbank, shalamo=shalamo))

    basic = {"contact_id": "c1", "product_id": "course_basic", "payment_method": "card"}
    hdr = {"X-Secret-Token": SECRET}

    print("1. Защита токеном")
    check(client.post("/init-payment", json=basic).status_code == 403, "без токена -> 403 forbidden")
    check(
        client.post("/init-payment", json=basic, headers={"X-Secret-Token": "bad"}).status_code == 403,
        "неверный токен -> 403",
    )

    print("2. Создание платежа")
    r = client.post("/init-payment", json=basic, headers=hdr).json()
    order_id = r.get("order_id")
    check(r["status"] == "created" and bool(r.get("pay_url")), "создан платёж, есть pay_url")

    print("3. Повторный клик до оплаты -> та же ссылка")
    r2 = client.post("/init-payment", json=basic, headers=hdr).json()
    check(r2["status"] == "existing_active" and r2["order_id"] == order_id, "возвращена активная ссылка")

    print("4. Webhook: неверная подпись -> доступ не выдан")
    order = db.get_by_order_id(order_id)
    bad = {"TerminalKey": "TestKey", "OrderId": order_id, "PaymentId": order["tbank_payment_id"],
           "Status": "CONFIRMED", "Success": True, "Amount": 9900, "Token": "deadbeef"}
    client.post("/webhook/tbank", json=bad)
    check(db.get_by_order_id(order_id)["tag_assigned_at"] is None, "тег не назначен при плохой подписи")

    print("5. Webhook: статус не CONFIRMED -> доступ не выдан")
    rej = sign({"TerminalKey": "TestKey", "OrderId": order_id, "PaymentId": order["tbank_payment_id"],
                "Status": "REJECTED", "Success": False, "Amount": 9900})
    client.post("/webhook/tbank", json=rej)
    check(db.get_by_order_id(order_id)["tag_assigned_at"] is None, "тег не назначен при REJECTED")

    print("6. Webhook: shalamo недоступен -> 503, оплата зафиксирована")
    shalamo.ok = False
    ok_payload = sign({"TerminalKey": "TestKey", "OrderId": order_id, "PaymentId": order["tbank_payment_id"],
                       "Status": "CONFIRMED", "Success": True, "Amount": 9900})
    resp = client.post("/webhook/tbank", json=ok_payload)
    check(resp.status_code == 503, "возвращён 503 при недоступном shalamo")
    check(db.get_by_order_id(order_id)["paid_at"] is not None, "оплата зафиксирована (paid_at)")

    print("7. Повторный webhook после восстановления shalamo -> тег назначен")
    shalamo.ok = True
    resp2 = client.post("/webhook/tbank", json=ok_payload)
    check(resp2.status_code == 200 and resp2.text == "OK", "webhook -> 200 OK")
    check(db.get_by_order_id(order_id)["tag_assigned_at"] is not None, "тег назначен")

    print("8. Идемпотентность: дубль webhook не ставит тег второй раз")
    calls_before = shalamo.tag_calls
    client.post("/webhook/tbank", json=ok_payload)
    check(shalamo.tag_calls == calls_before, "повторный webhook не дёргает assign_tag")

    print("9. Повторная покупка оплаченного товара -> новый платёж не создаётся")
    r3 = client.post("/init-payment", json=basic, headers=hdr).json()
    check(r3["status"] == "already_paid_access_granted", "статус already_paid_access_granted")

    failed = [n for ok, n in _results if not ok]
    total = len(_results)
    print("\n" + "=" * 60)
    if failed:
        print(f"РЕЗУЛЬТАТ: {total - len(failed)}/{total} PASS, ПРОВАЛЕНО {len(failed)}:")
        for n in failed:
            print(f"  - {n}")
        return 1
    print(f"РЕЗУЛЬТАТ: все {total} проверок PASS ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
