"""Конфиг-адаптер shalamov.io.

Реальный контракт API shalamov.io неизвестен, поэтому пути, метод, тело запроса
и схема авторизации берутся из config.yaml (блок `shalamo`). Когда станет известен
реальный контракт — правится ТОЛЬКО конфиг, код не трогается.

Плейсхолдеры в body_template:
  "{contact_id}" -> str id контакта
  "{tag}"        -> str имя тега
  "{variables}"  -> dict переменных (подставляется как объект, не строка)
  "{name}", "{value}" -> имя и значение одной переменной
                     (режим set_variables_one_per_request)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import ShalamoConfig, ShalamoEndpoint
from .logging_setup import get_logger

log = get_logger()


@dataclass
class ShalamoResult:
    ok: bool
    status_code: int | None = None
    error: str | None = None
    raw: Any = field(default=None)


def _render(template: Any, context: dict[str, Any]) -> Any:
    """Рекурсивно подставить плейсхолдеры в шаблон тела запроса."""
    if isinstance(template, dict):
        return {k: _render(v, context) for k, v in template.items()}
    if isinstance(template, list):
        return [_render(v, context) for v in template]
    if isinstance(template, str):
        # Точное совпадение плейсхолдера -> подставляем значение «как есть»
        # (важно для {variables}: это объект, а не строка).
        if template in ("{contact_id}", "{tag}", "{variables}"):
            key = template.strip("{}")
            return context.get(key, template)
        # Встроенные плейсхолдеры (строковые): {contact_id}, {tag}
        str_ctx = {
            k: v for k, v in context.items() if isinstance(v, (str, int, float))
        }
        try:
            return template.format(**str_ctx)
        except (KeyError, IndexError):
            return template
    return template


class ShalamoClient:
    def __init__(self, config: ShalamoConfig) -> None:
        self.config = config
        self.api_url = config.api_url.rstrip("/")

    def _auth(self) -> tuple[dict[str, str], dict[str, str]]:
        """Вернуть (headers, query_params) для авторизации.

        in: header -> ключ уходит заголовком; in: query -> query-параметром
        (например ?api_token=...). Формат значения — value_template.
        """
        value = self.config.auth.value_template.format(api_key=self.config.api_key)
        if self.config.auth.location == "query":
            return {}, {self.config.auth.param: value}
        return {self.config.auth.header: value}, {}

    @staticmethod
    def _is_success(endpoint: ShalamoEndpoint, status_code: int) -> bool:
        # Базовый критерий: HTTP 2xx. Структура success_when оставлена расширяемой.
        if endpoint.success_when.get("http_2xx", True):
            return 200 <= status_code < 300
        return 200 <= status_code < 300

    async def _call(
        self, endpoint: ShalamoEndpoint, context: dict[str, Any], op: str
    ) -> ShalamoResult:
        url = f"{self.api_url}{endpoint.path}"
        auth_headers, auth_params = self._auth()
        params = {**_render(endpoint.query_template, context), **auth_params}
        body = _render(endpoint.body_template, context)
        headers = dict(auth_headers)
        kwargs: dict[str, Any] = {}
        if params:
            kwargs["params"] = params
        if body:  # пустое тело не отправляем (query-style API)
            headers["Content-Type"] = "application/json"
            kwargs["json"] = body
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                resp = await client.request(
                    endpoint.method.upper(), url, headers=headers, **kwargs
                )
        except Exception as e:  # сеть/таймаут
            ms = (time.perf_counter() - t0) * 1000
            log.error("shalamo %s: ошибка запроса (%.0fms): %s", op, ms, e)
            return ShalamoResult(ok=False, error=str(e))
        ms = (time.perf_counter() - t0) * 1000

        if self._is_success(endpoint, resp.status_code):
            log.info("shalamo %s OK (HTTP %s %.0fms)", op, resp.status_code, ms)
            return ShalamoResult(ok=True, status_code=resp.status_code, raw=resp.text)

        log.error(
            "shalamo %s отказ: HTTP %s body=%s (%.0fms)", op, resp.status_code, resp.text[:500], ms
        )
        return ShalamoResult(
            ok=False,
            status_code=resp.status_code,
            error=f"HTTP {resp.status_code}",
            raw=resp.text,
        )

    async def assign_tag(self, contact_id: str, tag: str) -> ShalamoResult:
        return await self._call(
            self.config.assign_tag,
            {"contact_id": contact_id, "tag": tag},
            op="assign_tag",
        )

    async def set_variables(
        self, contact_id: str, variables: dict[str, Any]
    ) -> ShalamoResult:
        # Endpoint переменных опционален: если не настроен — шаг пропускаем
        # (best-effort, тег это не блокирует).
        if self.config.set_variables is None:
            return ShalamoResult(ok=True, error="set_variables endpoint не настроен")
        if self.config.set_variables_one_per_request:
            return await self._set_variables_one_by_one(contact_id, variables)
        return await self._call(
            self.config.set_variables,
            {"contact_id": contact_id, "variables": variables},
            op="set_variables",
        )

    async def _set_variables_one_by_one(
        self, contact_id: str, variables: dict[str, Any]
    ) -> ShalamoResult:
        """По запросу на переменную, строго по очереди: параллельные записи в один
        контакт shalamov.io теряет — оба ответа 200, сохраняется одна (проверено
        15.10.2026 на receipt_id/receipt_url). Первая ошибка обрывает серию: API,
        скорее всего, лежит, а каждый следующий таймаут задерживает выдачу доступа."""
        items = [(k, v) for k, v in variables.items() if v is not None and v != ""]
        assert self.config.set_variables is not None
        for i, (name, value) in enumerate(items):
            res = await self._call(
                self.config.set_variables,
                {"contact_id": contact_id, "name": name, "value": value},
                op=f"set_variable {name}",
            )
            if not res.ok:
                skipped = [n for n, _ in items[i:]]
                return ShalamoResult(
                    ok=False, status_code=res.status_code,
                    error=f"не записаны: {', '.join(skipped)} ({res.error})",
                )
        return ShalamoResult(ok=True)
