"""Настройка логирования: файл logs/app.log + stdout (для systemd/journalctl).

Секреты (ключи, пароли, токены) НЕ должны попадать в лог. Для этого есть
mask_secrets() — применять к любым словарям перед логированием.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from typing import Any

LOG_DIR = os.environ.get("LOG_DIR", "logs")
LOG_FILE = os.path.join(LOG_DIR, "app.log")

# Уровень логирования: LOG_LEVEL=DEBUG активирует детальные логи (тела webhook и т.п.)
_LOG_LEVEL_STR = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_LEVEL: int = getattr(logging, _LOG_LEVEL_STR, logging.INFO)

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

# ── Request ID (contextvars) ──────────────────────────────────────────────────
# Устанавливается middleware для каждого HTTP-запроса и вручную в начале каждого
# цикла фоновых задач; инъектируется в каждую запись лога через LogRecord factory.
_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


def get_request_id() -> str:
    """Вернуть текущий request_id (или '-' если не установлен)."""
    return _request_id_var.get()


def set_request_id(rid: str) -> None:
    """Установить request_id для текущего async-контекста."""
    _request_id_var.set(rid)


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


# ── LogRecord factory: инъекция request_id ───────────────────────────────────
# Захватываем оригинальную фабрику ДО вызова setup_logging, чтобы можно было
# цепочкой добавлять новые фабрики без потери атрибутов предыдущих.
_original_factory = logging.getLogRecordFactory()


def _request_id_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
    record = _original_factory(*args, **kwargs)
    record.request_id = _request_id_var.get()  # type: ignore[attr-defined]
    return record


_configured = False


def setup_logging(level: int = LOG_LEVEL) -> logging.Logger:
    """Идемпотентно сконфигурировать корневой логгер приложения."""
    global _configured
    logger = logging.getLogger("tbank_proxy")
    if _configured:
        return logger

    os.makedirs(LOG_DIR, exist_ok=True)
    logger.setLevel(level)

    # Формат включает request_id (инъектируется LogRecord factory).
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(request_id)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Устанавливаем factory здесь (один раз, под защитой _configured).
    logging.setLogRecordFactory(_request_id_factory)

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
