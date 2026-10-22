"""Instant cache invalidation across processes (app/core/cache_bus.py)."""
import json

from app.core import cache_bus, settings_service
from app.modules.m06_hazards.clustering import service as clustering_service
from app.modules.m10_collision.collision import thresholds


def _msg(topic: str, origin: str = "other-host:1:abc") -> str:
    return json.dumps({"t": topic, "o": origin})


def test_message_from_another_process_clears_the_section():
    settings_service._cache["maps"] = (1e12, object())
    settings_service._cache["security"] = (1e12, object())
    assert cache_bus.handle(_msg("settings:maps")) == "settings:maps"
    assert "maps" not in settings_service._cache
    assert "security" in settings_service._cache            # only the saved section
    settings_service._cache.clear()


def test_own_messages_are_ignored():
    settings_service._cache["maps"] = (1e12, object())
    assert cache_bus.handle(_msg("settings:maps", cache_bus._origin())) is None
    assert "maps" in settings_service._cache
    settings_service._cache.clear()


def test_clustering_topic_clears_rules_and_section():
    clustering_service._cache["snapshot"] = (1e12, object())
    settings_service._cache["clustering"] = (1e12, object())
    cache_bus.handle(_msg("clustering"))
    assert not clustering_service._cache and "clustering" not in settings_service._cache


def test_collision_section_expires_the_thresholds():
    thresholds._loaded_at = 123.0
    cache_bus.handle(_msg("settings:collision"))
    assert thresholds._loaded_at == 0.0


def test_reconnect_clears_everything():
    settings_service._cache["a"] = (1e12, object())
    clustering_service._cache["snapshot"] = (1e12, object())
    cache_bus.apply_local(cache_bus.ALL)
    assert not settings_service._cache and not clustering_service._cache


def test_garbage_is_ignored():
    assert cache_bus.handle("not json") is None
    assert cache_bus.handle(json.dumps({"x": 1})) is None


def test_publish_without_redis_never_raises(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "REDIS_URL", "redis://127.0.0.1:1/0")
    cache_bus.publish("settings:maps")                        # no running loop → sync path, fails quietly
