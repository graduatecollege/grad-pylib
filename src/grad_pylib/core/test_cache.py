from grad_pylib.core.cache import LazyValueCache


def test_lazy_value_cache_loads_once_until_invalidated() -> None:
    calls: list[str] = []

    def load(value: str) -> str:
        calls.append(value)
        return value.upper()

    cache = LazyValueCache(load)

    assert cache.get("first") == "FIRST"
    assert cache.get("second") == "FIRST"
    assert calls == ["first"]


def test_lazy_value_cache_reloads_after_invalidate() -> None:
    calls: list[str] = []

    def load(value: str) -> str:
        calls.append(value)
        return value.upper()

    cache = LazyValueCache(load)
    cache.get("first")
    cache.invalidate()

    assert cache.get("second") == "SECOND"
    assert calls == ["first", "second"]
