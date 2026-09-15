"""Подставной requests: ни один тест не ходит в сеть."""
import json as _json


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else _json.dumps(payload or {}, ensure_ascii=False)
        self.headers = headers or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("ответ не JSON")
        return self._payload


class FakeSession:
    """Отдаёт заранее разложенные ответы и записывает все вызовы."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        if not self.responses:
            return FakeResponse(200, {"ok": True, "result": []})
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)
