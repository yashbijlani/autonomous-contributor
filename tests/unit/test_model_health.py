"""Unit: model health cache TTL + invalidation."""
from contributor.opencode.client import ModelSpec
from contributor.persistence.database import Database
from contributor.persistence.model_health import ModelHealthStore


def _db() -> Database:
    return Database("sqlite:///:memory:")


def test_put_get_roundtrip():
    store = ModelHealthStore(_db(), ttl_s=3600)
    spec = ModelSpec(provider="p", model="p/m", variant="high")
    store.put(spec, ok=True, latency_s=2.5)
    got = store.get(spec)
    assert got is not None and got.is_ok and got.latency_s == 2.5


def test_ttl_expiry():
    store = ModelHealthStore(_db(), ttl_s=0)
    spec = ModelSpec(provider="p", model="p/m")
    store.put(spec, ok=True, latency_s=1.0)
    assert store.get(spec) is None  # immediately stale
    assert store.get(spec, allow_stale=True) is not None


def test_unavailable_recorded():
    store = ModelHealthStore(_db(), ttl_s=3600)
    spec = ModelSpec(provider="p", model="p/m", variant="xhigh")
    store.put(spec, ok=False, error_class="PROVIDER_SERVER_ERROR")
    got = store.get(spec)
    assert got is not None and not got.is_ok
    assert got.error_class == "PROVIDER_SERVER_ERROR"


def test_invalidate():
    store = ModelHealthStore(_db(), ttl_s=3600)
    spec = ModelSpec(provider="p", model="p/m")
    store.put(spec, ok=True)
    store.invalidate(spec)
    assert store.get(spec) is None


def test_variant_is_part_of_key():
    store = ModelHealthStore(_db(), ttl_s=3600)
    a = ModelSpec(provider="p", model="p/m", variant="high")
    b = ModelSpec(provider="p", model="p/m", variant="xhigh")
    store.put(a, ok=True, latency_s=1.0)
    store.put(b, ok=False, error_class="X")
    assert store.get(a).is_ok
    assert not store.get(b).is_ok
