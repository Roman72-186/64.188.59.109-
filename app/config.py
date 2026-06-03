"""Загрузка и валидация config.yaml.

Единственный источник настроек проекта. Поведение (товары, способы оплаты,
теги, endpoint'ы shalamo) задаётся конфигом, не кодом. При невалидном конфиге
приложение должно падать на старте с понятной ошибкой.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator


# ── pydantic-модели конфигурации ────────────────────────────────────────────


class TBankConfig(BaseModel):
    terminal_key: str
    terminal_password: str
    api_url: str
    timeout_seconds: float = 15.0


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


class PaymentMethodConfig(BaseModel):
    label: str
    extra_params: dict[str, Any] = Field(default_factory=dict)


class ProductConfig(BaseModel):
    name: str
    amount: int = Field(gt=0, description="сумма в копейках")
    description: str = ""
    payment_methods: list[str]
    tags_by_method: dict[str, str]
    variables: dict[str, Any] = Field(default_factory=dict)

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
        return self

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

    def merged_extra_params(self, method: str) -> dict[str, Any]:
        mc = self.payment_methods.get(method)
        return dict(mc.extra_params) if mc else {}


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
