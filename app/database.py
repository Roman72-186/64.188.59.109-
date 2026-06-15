"""Слой БД (SQLite) + идемпотентность.

Ключевая идея: «оплачено банком» (`paid_at`) и «доступ выдан / тег назначен»
(`tag_assigned_at`) — ДВА НЕЗАВИСИМЫХ факта. Это нужно для PRD §7.3 (товар оплачен,
но тег ещё не назначен) и для идемпотентности webhook (PRD §7.6).

Статусы (`status`): pending -> processing -> confirmed | failed.
  pending    — заказ создан, ждём оплату
  processing — пришёл CONFIRMED, идёт/повторяется назначение тега
  confirmed  — оплачено И тег назначен (полный успех)
  failed     — Init не удался, либо терминальный негативный статус Т-Банка

Защита от дублей — атомарный захват (`atomic_capture`): один UPDATE ... WHERE
с проверкой `tag_assigned_at IS NULL`. SQLite сериализует записи, поэтому захват
атомарен; после успеха `tag_assigned_at` проставлен и повторный захват не проходит.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional

ACTIVE_LINK_MAX_AGE_SECONDS = 3600  # PRD §7.2: активная ссылка живёт 1 час


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id          TEXT    NOT NULL UNIQUE,
    contact_id        TEXT    NOT NULL,
    product_id        TEXT    NOT NULL,
    payment_method    TEXT    NOT NULL,
    amount            INTEGER NOT NULL,            -- в копейках
    status            TEXT    NOT NULL DEFAULT 'pending',
    tbank_payment_id  TEXT,
    tbank_status      TEXT,                         -- последний сырой статус Т-Банка
    pay_url           TEXT,
    tag_name          TEXT,
    item_name         TEXT,                         -- имя позиции (cart): для совпадения позиций Долями create/commit
    paid_at           TEXT,                         -- банк подтвердил оплату (CONFIRMED)
    tag_assigned_at   TEXT,                         -- тег успешно назначен в shalamo
    fail_tag_assigned_at TEXT,                       -- тег ОТКАЗА назначен (отдельный факт)
    last_error        TEXT,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_cpm
    ON payments (contact_id, product_id, payment_method);
CREATE INDEX IF NOT EXISTS idx_payments_cp
    ON payments (contact_id, product_id);
CREATE INDEX IF NOT EXISTS idx_payments_tbank
    ON payments (tbank_payment_id);
"""


class Database:
    """Тонкая обёртка над SQLite. Соединение открывается на каждую операцию
    (короткие транзакции), что безопасно при WAL + busy_timeout."""

    def __init__(self, db_path: str = "payments.db") -> None:
        self.db_path = db_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Лёгкие миграции для уже существующих БД (CREATE TABLE IF NOT EXISTS
        не добавляет новые столбцы в существующую таблицу). Идемпотентно."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(payments)")}
        if "fail_tag_assigned_at" not in cols:
            conn.execute("ALTER TABLE payments ADD COLUMN fail_tag_assigned_at TEXT")
        if "item_name" not in cols:
            conn.execute("ALTER TABLE payments ADD COLUMN item_name TEXT")

    # ── чтение ──────────────────────────────────────────────────────────────

    @staticmethod
    def _row(row: Optional[sqlite3.Row]) -> Optional[dict[str, Any]]:
        return dict(row) if row is not None else None

    def get_by_order_id(self, order_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM payments WHERE order_id = ?", (order_id,)
            )
            return self._row(cur.fetchone())

    def get_by_tbank_payment_id(self, payment_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM payments WHERE tbank_payment_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (str(payment_id),),
            )
            return self._row(cur.fetchone())

    def find_active_link(
        self,
        contact_id: str,
        product_id: str,
        payment_method: str,
        max_age_seconds: int = ACTIVE_LINK_MAX_AGE_SECONDS,
    ) -> Optional[dict[str, Any]]:
        """PRD §7.2: неоплаченная ссылка (status='pending', есть pay_url),
        созданная не более max_age_seconds назад."""
        threshold = (
            datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        ).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM payments "
                "WHERE contact_id = ? AND product_id = ? AND payment_method = ? "
                "  AND status = 'pending' AND pay_url IS NOT NULL "
                "  AND created_at >= ? "
                "ORDER BY created_at DESC LIMIT 1",
                (contact_id, product_id, payment_method, threshold),
            )
            return self._row(cur.fetchone())

    def get_pending_credit_orders(
        self, payment_methods: list[str], max_age_seconds: int
    ) -> list[dict[str, Any]]:
        """Заявки Credit Broker (provider=tbank_credit) без финального исхода —
        для фонового опроса GET /info (см. модуль tbank_credit). Условие то же,
        что у идемпотентного захвата: ни тег успеха, ни тег отказа ещё не
        назначены. `max_age_seconds` отсекает давно заброшенные заявки."""
        if not payment_methods:
            return []
        threshold = (
            datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        ).isoformat()
        placeholders = ",".join("?" for _ in payment_methods)
        with self._connect() as conn:
            cur = conn.execute(
                f"SELECT * FROM payments "
                f"WHERE payment_method IN ({placeholders}) "
                f"  AND tag_assigned_at IS NULL AND fail_tag_assigned_at IS NULL "
                f"  AND created_at >= ? "
                f"ORDER BY created_at ASC",
                (*payment_methods, threshold),
            )
            return [dict(r) for r in cur.fetchall()]

    def find_paid_order(
        self, contact_id: str, product_id: str
    ) -> Optional[dict[str, Any]]:
        """PRD §7.3: любой платёж по товару, подтверждённый банком (paid_at IS NOT NULL),
        независимо от того, назначен ли уже тег."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM payments "
                "WHERE contact_id = ? AND product_id = ? AND paid_at IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                (contact_id, product_id),
            )
            return self._row(cur.fetchone())

    # ── запись ──────────────────────────────────────────────────────────────

    def create_payment(
        self,
        order_id: str,
        contact_id: str,
        product_id: str,
        payment_method: str,
        amount: int,
        tag_name: Optional[str],
        item_name: Optional[str] = None,
    ) -> dict[str, Any]:
        now = _utcnow()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO payments "
                "(order_id, contact_id, product_id, payment_method, amount, "
                " status, tag_name, item_name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    order_id,
                    contact_id,
                    product_id,
                    payment_method,
                    amount,
                    tag_name,
                    item_name,
                    now,
                    now,
                ),
            )
        return self.get_by_order_id(order_id)  # type: ignore[return-value]

    def update_init_result(
        self, order_id: str, tbank_payment_id: str, pay_url: str
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE payments SET tbank_payment_id = ?, pay_url = ?, "
                "tbank_status = 'NEW', updated_at = ? WHERE order_id = ?",
                (str(tbank_payment_id), pay_url, _utcnow(), order_id),
            )

    def set_tbank_status(self, order_id: str, tbank_status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE payments SET tbank_status = ?, updated_at = ? "
                "WHERE order_id = ?",
                (tbank_status, _utcnow(), order_id),
            )

    def mark_failed(self, order_id: str, error: Optional[str] = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE payments SET status = 'failed', last_error = ?, "
                "updated_at = ? WHERE order_id = ?",
                (error, _utcnow(), order_id),
            )

    def mark_paid(self, order_id: str, tbank_status: str = "CONFIRMED") -> None:
        """Банк подтвердил оплату. Фиксируем paid_at; статус -> processing,
        ЕСЛИ ещё не confirmed (тег мог быть уже назначен ранее)."""
        now = _utcnow()
        with self._connect() as conn:
            conn.execute(
                "UPDATE payments SET "
                "  paid_at = COALESCE(paid_at, ?), "
                "  tbank_status = ?, "
                "  status = CASE WHEN status = 'confirmed' THEN 'confirmed' "
                "                ELSE 'processing' END, "
                "  updated_at = ? "
                "WHERE order_id = ?",
                (now, tbank_status, now, order_id),
            )

    def atomic_capture(self, order_id: str) -> bool:
        """Захватить платёж под назначение тега. True — захватили (можно назначать),
        False — уже назначено ранее (повторный/параллельный webhook, PRD §7.6).

        Условие: тег ещё не назначен. Статус переводим в 'processing'. Захват
        повторяем для 'pending'/'processing'/'failed' — это позволяет повторную
        обработку после возврата 503 (PRD §7.8)."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE payments SET status = 'processing', updated_at = ? "
                "WHERE order_id = ? AND tag_assigned_at IS NULL "
                "  AND status IN ('pending', 'processing', 'failed')",
                (_utcnow(), order_id),
            )
            return cur.rowcount == 1

    def record_tag_error(self, order_id: str, error: str) -> None:
        """Назначение тега не удалось. Тег НЕ назначен, статус остаётся
        'processing' (платёж пере-обрабатываем), пишем причину для ручного разбора."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE payments SET last_error = ?, updated_at = ? "
                "WHERE order_id = ?",
                (error, _utcnow(), order_id),
            )

    def mark_tag_assigned(self, order_id: str) -> None:
        """Тег успешно назначен -> полный успех (status='confirmed')."""
        now = _utcnow()
        with self._connect() as conn:
            conn.execute(
                "UPDATE payments SET tag_assigned_at = COALESCE(tag_assigned_at, ?), "
                "status = 'confirmed', last_error = NULL, updated_at = ? "
                "WHERE order_id = ?",
                (now, now, order_id),
            )

    # ── тег отказа (отдельный факт, не гейт доступа) ─────────────────────────

    def capture_fail_tag(self, order_id: str) -> bool:
        """Захватить платёж под назначение тега ОТКАЗА. True — захватили,
        False — тег отказа уже назначен (повторный/параллельный webhook) ИЛИ
        успех уже выдан (тег отказа на оплаченный заказ не ставим).

        Условие: ни тег отказа, ни успешный тег ещё не назначены. Статус
        переводим в 'failed'."""
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE payments SET status = 'failed', updated_at = ? "
                "WHERE order_id = ? AND fail_tag_assigned_at IS NULL "
                "  AND tag_assigned_at IS NULL",
                (_utcnow(), order_id),
            )
            return cur.rowcount == 1

    def mark_fail_tag_assigned(self, order_id: str) -> None:
        """Тег отказа успешно назначен. Фиксируем факт; статус остаётся 'failed'."""
        now = _utcnow()
        with self._connect() as conn:
            conn.execute(
                "UPDATE payments SET "
                "fail_tag_assigned_at = COALESCE(fail_tag_assigned_at, ?), "
                "status = 'failed', updated_at = ? WHERE order_id = ?",
                (now, now, order_id),
            )
