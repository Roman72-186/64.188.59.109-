"""Шлёт подписанный тестовый webhook на боевой сервер.

Использует НЕсуществующий OrderId — никакого тега никому не назначит,
только проверяет доступность endpoint'а /webhook/tbank и верность подписи Token.

Запуск (локально; config.yaml указывает на боевой public_url):
    venv\\Scripts\\python check_webhook.py

Ожидаемо:
    -> 200 'OK'
а в логе сервера (journalctl -u tbank-proxy -f):
    webhook Т-Банк: order=TEST-WEBHOOK-CHECK ... status=CONFIRMED
    webhook: заказ не найден order=TEST-WEBHOOK-CHECK ...
"""

import httpx

from app.config import get_config
from app.tbank import build_token

cfg = get_config()
payload = {
    "TerminalKey": cfg.tbank.terminal_key,
    "OrderId": "TEST-WEBHOOK-CHECK",  # фейковый заказ → побочных эффектов нет
    "Success": True,
    "Status": "CONFIRMED",
    "PaymentId": "0",
    "ErrorCode": "0",
    "Amount": 100,
}
payload["Token"] = build_token(payload, cfg.tbank.terminal_password)

url = cfg.server.public_url.rstrip("/") + "/webhook/tbank"
print("POST", url)
r = httpx.post(url, json=payload, timeout=10)
print("->", r.status_code, repr(r.text))
