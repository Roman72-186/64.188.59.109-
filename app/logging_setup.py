"""Настройка логирования: файл logs/app.log + stdout (для systemd/journalctl).

Секреты (ключи, пароли, токены) НЕ должны попадать в лог. Для этого есть
mask_secrets() — применять к любым словарям перед логированием.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from typing import Any

LOG_DIR = os.environ.get("LOG_DIR", "logs")
LOG_FILE = os.path.join(LOG_DIR, "app.log")

# Поля, значения которых нельзя логировать ни при каких обстоятельствах.
_SECRET_KEYS = {
    "terminal_password",
    "password",
    "secret_token",
    "x-secret-token",
    "api_key",
    "authorization",
    "token",  # подпись Token Т-Банка тоже маскируем
    "ssh_password",
}


def mask_secrets(data: Any) -> Any:
    """Рекурсивно заменить секретные поля на '***' для безопасного логирования."""
    if isinstance(data, dict):
        return {
            k: ("***" if k.lower() in _SECRET_KEYS else mask_secrets(v))
            for k, v in data.items()
        }
    if isinstance(data, (list, tuple)):
        return type(data)(mask_secrets(v) for v in data)
    return data


_configured = False


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Идемпотентно сконфигурировать корневой логгер приложения."""
    global _configured
    logger = logging.getLogger("tbank_proxy")
    if _configured:
        return logger

    os.makedirs(LOG_DIR, exist_ok=True)
    logger.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.propagate = False

    _configured = True
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("tbank_proxy")
