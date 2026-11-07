import pytest

from osu2yt.matching import search as search_mod


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests must never hit YouTube; patch these explicitly where a test needs results."""
    def blocked(*a, **k):
        raise AssertionError("network access in tests")
    monkeypatch.setattr(search_mod, "search", blocked)
    monkeypatch.setattr(search_mod, "fetch_video", blocked)
