"""Загрузка и валидация config.yaml.

Единственный источник настроек проекта. Поведение (товары, способы оплаты,
теги, endpoint'ы shalamo) задаётся конфигом, не кодом. При невалидном конфиге
приложение должно падать на старте с понятной ошибкой.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator


# ── pydantic-модели конфигурации ────────────────────────────────────────────


class TerminalCredentials(BaseModel):
    """Реквизиты дополнительного терминала Т-Банка (отдельного магазина).

    Используется, когда отдельный способ оплаты (напр. Долями) должен идти
    через отдельный магазин, чтобы платёжная форма показывала только его.
    api_url/timeout_seconds наследуются от основного терминала, если не заданы.
    """

    terminal_key: str
    terminal_password: str
    api_url: str | None = None
    timeout_seconds: float | None = None


class TBankConfig(BaseModel):
    terminal_key: str
    terminal_password: str
    api_url: str
    timeout_seconds: float = 15.0
    # Доп. терминалы (отдельные магазины) под конкретные способы оплаты.
    # Ключ словаря — имя, на которое ссылается payment_methods[*].terminal.
    extra_terminals: dict[str, TerminalCredentials] = Field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedTerminal:
    """Разрешённые реквизиты терминала (наследование от основного уже применено)."""

    terminal_key: str
    terminal_password: str
    api_url: str
    timeout_seconds: float


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    public_url: str
    secret_token: str

    @model_validator(mode="after")
    def _check_secret(self) -> "ServerConfig":
        if not self.secret_token or self.secret_token == "CHANGE_ME":
            raise ValueError("server.secret_token не задан (всё ещё CHANGE_ME)")
        return self


class ShalamoAuth(BaseModel):
    # Где передавать ключ: "header" (по умолчанию) или "query" (как ?api_token=...).
    location: str = Field("header", alias="in")
    header: str = "Authorization"        # имя заголовка при in: header
    param: str = "api_token"             # имя query-параметра при in: query
    value_template: str = "Bearer {api_key}"  # шаблон значения ({api_key})

    model_config = {"populate_by_name": True}


class ShalamoEndpoint(BaseModel):
    method: str = "POST"
    path: str
    # Параметры в query-строке (?a=b). Плейсхолдеры как в body_template.
    query_template: dict[str, Any] = Field(default_factory=dict)
    # Тело JSON. Если пусто — тело не отправляется (для query-style API).
    body_template: dict[str, Any] = Field(default_factory=dict)
    success_when: dict[str, Any] = Field(default_factory=lambda: {"http_2xx": True})


class ShalamoConfig(BaseModel):
    api_url: str
    api_key: str
    timeout_seconds: float = 3.0
    auth: ShalamoAuth = Field(default_factory=ShalamoAuth)
    assign_tag: ShalamoEndpoint
    # Переменные контакта — best-effort. Если endpoint не задан, шаг пропускается.
    set_variables: ShalamoEndpoint | None = None


class DolyameConfig(BaseModel):
    """Прямой Partner API Долями (partner.dolyame.ru): mTLS + Basic.

    Отдельный провайдер оплаты, не эквайринг Т-Банка (нужна Долями-only форма).
    cert_path/key_path — клиентский сертификат mTLS, ОБЯЗАТЕЛЕН на живой вызов;
    пустые значения допустимы для каркаса на моках (TLS-handshake тогда не пройдёт).
    """

    base_url: str = "https://partner.dolyame.ru"
    login: str
    password: str
    cert_path: str = ""
    key_path: str = ""
    timeout_seconds: float = 5.0
    # Фискализация через Долями: "disabled" (по умолчанию) | "enabled".
    fiscalization: str = "disabled"
    # Двухфазность: при webhook со статусом wait_for_commit синхронно вызвать commit
    # (захват холда), затем назначить тег. False — назначать тег уже на wait_for_commit
    # (холд без захвата). По умолчанию True (рекомендация: commit → committed → тег).
    commit_on_webhook: bool = True
    # Подсеть, с которой принимаются webhook'и Долями (CIDR). Прочие источники игнорим.
    webhook_allowed_subnet: str = "91.194.226.0/23"

    def fiscalization_settings(self) -> dict[str, Any]:
        """Объект fiscalization_settings для тела запросов Долями (oneOf по type)."""
        if self.fiscalization == "enabled":
            return {"type": "enabled"}
        return {"type": "disabled"}


class PaymentMethodConfig(BaseModel):
    label: str
    extra_params: dict[str, Any] = Field(default_factory=dict)
    # Имя терминала из tbank.extra_terminals. None = основной терминал.
    terminal: str | None = None
    # Провайдер оплаты: "tbank" (эквайринг, по умолчанию) | "dolyame" (прямой Partner API).
    provider: str = "tbank"


class ReceiptConfig(BaseModel):
    """Чек 54-ФЗ для Т-Банка (передаётся в Init как объект Receipt).

    Если блока нет в конфиге — чек не отправляется (для терминалов без
    фискализации, напр. тестового). Боевой терминал с подключённой кассой
    требует Receipt, иначе Init -> ошибка 309 {request.validate.expected.receipt}.

    Контакт получателя чека (Email или Phone) обязателен по 54-ФЗ: берётся из
    запроса /init-payment (поля email/phone), иначе из fallback ниже.
    """

    enabled: bool = True
    taxation: str  # система налогообложения: usn_income | osn | usn_income_outcome | ...
    tax: str = "none"  # НДС по умолчанию: none | vat0 | vat10 | vat20 | vat110 | vat120
    email: str | None = None  # fallback-контакт для чека, если бот не передал
    phone: str | None = None
    payment_method: str | None = None  # признак способа расчёта, напр. full_prepayment
    payment_object: str | None = None  # признак предмета расчёта, напр. service


class ProductConfig(BaseModel):
    name: str
    amount: int = Field(gt=0, description="сумма в копейках")
    description: str = ""
    payment_methods: list[str]
    tags_by_method: dict[str, str]
    # Тег «отказ оплаты» по способу (товар+способ -> тег). ОПЦИОНАЛЬНО: назначается
    # только если для способа задан тег. Сейчас используется для Долями (terminal
    # negative: rejected/canceled). Если способа здесь нет — тег отказа не шлётся,
    # поведение прежнее. Это не гейт доступа, а триггер авторассылки «оплата не прошла».
    fail_tags_by_method: dict[str, str] = Field(default_factory=dict)
    variables: dict[str, Any] = Field(default_factory=dict)
    tax: str | None = None  # переопределение ставки НДS для чека (иначе receipt.tax)

    @model_validator(mode="after")
    def _check_tags(self) -> "ProductConfig":
        # У каждого разрешённого способа оплаты должен быть тег.
        missing = [m for m in self.payment_methods if m not in self.tags_by_method]
        if missing:
            raise ValueError(
                f"для способов оплаты {missing} не задан тег в tags_by_method"
            )
        return self


class AppConfig(BaseModel):
    tbank: TBankConfig
    server: ServerConfig
    shalamo: ShalamoConfig
    payment_methods: dict[str, PaymentMethodConfig]
    products: dict[str, ProductConfig]
    # Чек 54-ФЗ. Если не задан — Receipt в Init не отправляется.
    receipt: ReceiptConfig | None = None
    # Прямой Partner API Долями. Если не задан — способы с provider='dolyame' запрещены.
    dolyame: DolyameConfig | None = None

    @model_validator(mode="after")
    def _cross_checks(self) -> "AppConfig":
        # Каждый способ оплаты товара должен существовать в payment_methods.
        for pid, product in self.products.items():
            for method in product.payment_methods:
                if method not in self.payment_methods:
                    raise ValueError(
                        f"товар '{pid}': способ оплаты '{method}' "
                        f"отсутствует в payment_methods"
                    )
        # Ссылка способа оплаты на терминал должна существовать в extra_terminals.
        for name, mc in self.payment_methods.items():
            if mc.terminal and mc.terminal not in self.tbank.extra_terminals:
                raise ValueError(
                    f"способ оплаты '{name}': терминал '{mc.terminal}' "
                    f"отсутствует в tbank.extra_terminals"
                )
            if mc.provider not in ("tbank", "dolyame"):
                raise ValueError(
                    f"способ оплаты '{name}': неизвестный provider '{mc.provider}' "
                    f"(допустимо: tbank | dolyame)"
                )
            # Прямой Долями требует блока dolyame в конфиге.
            if mc.provider == "dolyame" and self.dolyame is None:
                raise ValueError(
                    f"способ оплаты '{name}': provider='dolyame', но блок 'dolyame' "
                    f"в конфиге отсутствует"
                )
        return self

    def provider_for_method(self, method: str) -> str:
        """Провайдер оплаты способа: 'tbank' (эквайринг) | 'dolyame' (прямой API)."""
        mc = self.payment_methods.get(method)
        return mc.provider if mc else "tbank"

    # ── терминалы (основной + доп. магазины) ────────────────────────────────

    def resolved_terminals(self) -> dict[str, ResolvedTerminal]:
        """Все терминалы по terminal_key: основной + extra (наследование применено)."""
        out: dict[str, ResolvedTerminal] = {
            self.tbank.terminal_key: ResolvedTerminal(
                terminal_key=self.tbank.terminal_key,
                terminal_password=self.tbank.terminal_password,
                api_url=self.tbank.api_url,
                timeout_seconds=self.tbank.timeout_seconds,
            )
        }
        for t in self.tbank.extra_terminals.values():
            out[t.terminal_key] = ResolvedTerminal(
                terminal_key=t.terminal_key,
                terminal_password=t.terminal_password,
                api_url=t.api_url or self.tbank.api_url,
                timeout_seconds=(
                    t.timeout_seconds
                    if t.timeout_seconds is not None
                    else self.tbank.timeout_seconds
                ),
            )
        return out

    def terminal_key_for_method(self, method: str) -> str:
        """terminal_key, через который проводится этот способ оплаты."""
        mc = self.payment_methods.get(method)
        if mc and mc.terminal:
            return self.tbank.extra_terminals[mc.terminal].terminal_key
        return self.tbank.terminal_key

    def password_for_terminal_key(self, terminal_key: str) -> str | None:
        """Пароль терминала по его TerminalKey (для проверки подписи webhook)."""
        t = self.resolved_terminals().get(terminal_key)
        return t.terminal_password if t else None

    # ── удобные методы доступа ──────────────────────────────────────────────

    def get_product(self, product_id: str) -> ProductConfig | None:
        return self.products.get(product_id)

    def is_method_allowed(self, product_id: str, method: str) -> bool:
        product = self.products.get(product_id)
        return bool(product and method in product.payment_methods)

    def tag_for(self, product_id: str, method: str) -> str | None:
        product = self.products.get(product_id)
        if not product:
            return None
        return product.tags_by_method.get(method)

    def fail_tag_for(self, product_id: str, method: str) -> str | None:
        """Тег «отказ оплаты» для способа (None, если не задан → тег не шлётся)."""
        product = self.products.get(product_id)
        if not product:
            return None
        return product.fail_tags_by_method.get(method)

    def merged_extra_params(self, method: str) -> dict[str, Any]:
        mc = self.payment_methods.get(method)
        return dict(mc.extra_params) if mc else {}

    def build_receipt(
        self,
        product_id: str,
        email: str | None = None,
        phone: str | None = None,
        amount: int | None = None,
    ) -> dict[str, Any] | None:
        """Собрать объект Receipt (54-ФЗ) для Init Т-Банка.

        Возвращает None, если чек выключен/не настроен (тогда Receipt не шлётся).
        Чек состоит из одной позиции = оплачиваемый товар (сумма в копейках,
        Amount = Price * Quantity). Контакт получателя — email/phone из запроса,
        иначе fallback из конфига. `amount` переопределяет сумму товара (если
        платформа передала свою сумму в /init-payment) — иначе Init и Receipt
        разойдутся и Т-Банк отклонит платёж.
        """
        if self.receipt is None or not self.receipt.enabled:
            return None
        product = self.products.get(product_id)
        if product is None:
            return None
        item_amount = amount if amount is not None else product.amount
        r = self.receipt
        item: dict[str, Any] = {
            "Name": product.name[:128],
            "Price": item_amount,
            "Quantity": 1,
            "Amount": item_amount,
            "Tax": product.tax or r.tax,
        }
        if r.payment_method:
            item["PaymentMethod"] = r.payment_method
        if r.payment_object:
            item["PaymentObject"] = r.payment_object
        receipt: dict[str, Any] = {"Taxation": r.taxation, "Items": [item]}
        contact_email = email or r.email
        contact_phone = phone or r.phone
        if contact_email:
            receipt["Email"] = contact_email
        if contact_phone:
            receipt["Phone"] = contact_phone
        return receipt


DEFAULT_CONFIG_PATH = os.environ.get("CONFIG_PATH", "config.yaml")


def load_config(path: str | None = None) -> AppConfig:
    """Прочитать и провалидировать YAML-конфиг. Бросает понятную ошибку."""
    cfg_path = path or DEFAULT_CONFIG_PATH
    if not os.path.exists(cfg_path):
        raise FileNotFoundError(
            f"Не найден конфиг '{cfg_path}'. Скопируй config.example.yaml -> config.yaml "
            f"и заполни значения."
        )
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    try:
        return AppConfig.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"Ошибка в конфиге '{cfg_path}':\n{e}") from e


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """Кэшированный доступ к конфигу для рантайма приложения."""
    return load_config()
