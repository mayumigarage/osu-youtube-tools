"""Minimal stand-in for googleapiclient's YouTube service object."""

import json
from collections import defaultdict

import httplib2
from googleapiclient.errors import HttpError


def http_error(status: int, reason: str) -> HttpError:
    body = json.dumps({"error": {"code": status, "errors": [{"reason": reason}], "message": reason}}).encode()
    return HttpError(httplib2.Response({"status": status}), body)


class _Request:
    def __init__(self, service, method, kwargs):
        self.service, self.method, self.kwargs = service, method, kwargs

    def execute(self, num_retries=0):
        return self.service._handle(self.method, self.kwargs)


class _Resource:
    def __init__(self, service, name):
        self._service, self._name = service, name

    def __getattr__(self, op):
        return lambda **kw: _Request(self._service, f"{self._name}.{op}", kw)


class FakeService:
    """handlers: {"playlistItems.list": callable(kwargs) | list of responses/exceptions (consumed in order)}"""

    def __init__(self, handlers):
        self.handlers = handlers
        self.calls = []
        self._pos = defaultdict(int)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda: _Resource(self, name)

    def _handle(self, method, kwargs):
        self.calls.append((method, kwargs))
        h = self.handlers[method]
        if callable(h):
            result = h(kwargs)
        else:
            result = h[self._pos[method]]
            self._pos[method] += 1
        if isinstance(result, Exception):
            raise result
        return result

    def count(self, method):
        return sum(1 for m, _ in self.calls if m == method)
