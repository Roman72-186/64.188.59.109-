"""Unit-тесты конфиг-адаптера shalamo (query-параметры, auth, пропуск переменных).

Реальные запросы не уходят: httpx.AsyncClient подменяется на MockTransport,
который перехватывает запрос и отдаёт заранее заданный ответ.
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx

from app.config import ShalamoAuth, ShalamoConfig, ShalamoEndpoint
from app.shalamo import ShalamoClient


def _client_with_capture(monkeypatch, cfg: ShalamoConfig, *, status=200, text="OK"):
    """Подменяет httpx.AsyncClient на транспорт-перехватчик. Возвращает (client, captured)."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["content"] = request.content
        return httpx.Response(status, text=text)

    transport = httpx.MockTransport(handler)
    real_cls = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_cls(*args, **kwargs)

    monkeypatch.setattr("app.shalamo.httpx.AsyncClient", patched)
    return ShalamoClient(cfg), captured


def _assign_tag_cfg(**overrides) -> ShalamoConfig:
    base = dict(
        api_url="https://app.shalamov.io/api/v1",
        api_key="TESTKEY",
        auth=ShalamoAuth(location="query", param="api_token", value_template="{api_key}"),
        assign_tag=ShalamoEndpoint(
            method="POST",
            path="/attachTagToContact",
            query_template={"contact_id": "{contact_id}", "name": "{tag}"},
        ),
    )
    base.update(overrides)
    return ShalamoConfig(**base)


def test_assign_tag_sends_query_params_and_no_body(monkeypatch):
    client, captured = _client_with_capture(monkeypatch, _assign_tag_cfg())
    res = asyncio.run(client.assign_tag("12345", "paid_card_basic"))

    assert res.ok
    assert captured["method"] == "POST"
    parsed = urlparse(captured["url"])
    assert parsed.path == "/api/v1/attachTagToContact"
    q = parse_qs(parsed.query)
    assert q["api_token"] == ["TESTKEY"]      # auth ушёл в query, не в заголовок
    assert q["contact_id"] == ["12345"]
    assert q["name"] == ["paid_card_basic"]
    assert "authorization" not in {k.lower() for k in captured["headers"]}
    assert captured["content"] == b""          # пустое body_template => тела нет


def test_assign_tag_url_encodes_cyrillic_tag(monkeypatch):
    client, captured = _client_with_capture(monkeypatch, _assign_tag_cfg())
    asyncio.run(client.assign_tag("42", "тег с пробелом"))
    q = parse_qs(urlparse(captured["url"]).query)
    assert q["name"] == ["тег с пробелом"]     # httpx сам URL-кодирует


def test_header_auth_backward_compatible(monkeypatch):
    cfg = _assign_tag_cfg(
        auth=ShalamoAuth(header="Authorization", value_template="Bearer {api_key}"),
    )
    client, captured = _client_with_capture(monkeypatch, cfg)
    asyncio.run(client.assign_tag("1", "t"))
    assert captured["headers"]["authorization"] == "Bearer TESTKEY"
    assert "api_token" not in parse_qs(urlparse(captured["url"]).query)


def test_set_variables_skipped_when_not_configured(monkeypatch):
    client, captured = _client_with_capture(monkeypatch, _assign_tag_cfg())
    res = asyncio.run(client.set_variables("1", {"a": "b"}))
    assert res.ok                              # best-effort: пропуск считается успехом
    assert captured == {}                      # запрос не уходил


def test_non_2xx_is_failure(monkeypatch):
    client, _ = _client_with_capture(monkeypatch, _assign_tag_cfg(), status=400, text="bad")
    res = asyncio.run(client.assign_tag("1", "t"))
    assert not res.ok
    assert res.status_code == 400
