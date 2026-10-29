"""
In-memory Redis double for the collision-prevention tests: strings, hashes, sets, lists, a GEO
set and pub/sub — the subset Module 10 uses. One store, two faces: `FakeRedis` (sync, like
job_slots.client()) and `.aio` (async, like get_redis()). TTLs are accepted and ignored.
Thread-safe enough for FastAPI's TestClient (publish from the test thread, read in the app's).
"""
import asyncio
import math
import threading
from collections import deque


class FakePubSub:
    def __init__(self, store: "FakeRedis"):
        self.store = store
        self.channels: set = set()
        self.queue: deque = deque()

    async def subscribe(self, *channels):
        with self.store.lock:
            for c in channels:
                self.channels.add(c)
                self.store.subscribers.setdefault(c, set()).add(self)

    async def unsubscribe(self, *channels):
        with self.store.lock:
            for c in (channels or list(self.channels)):
                self.channels.discard(c)
                self.store.subscribers.get(c, set()).discard(self)

    async def get_message(self, ignore_subscribe_messages=False, timeout=0.0):
        waited = 0.0
        while True:
            if self.queue:
                return self.queue.popleft()
            if waited >= (timeout or 0):
                return None
            await asyncio.sleep(0.01)
            waited += 0.01

    async def aclose(self):
        await self.unsubscribe()


class _Pipeline:
    def __init__(self, store):
        self.store, self.ops = store, []

    def __getattr__(self, name):
        def op(*a, **kw):
            self.ops.append((name, a, kw))
            return self
        return op

    def execute(self):
        return [getattr(self.store, n)(*a, **kw) for n, a, kw in self.ops]


class FakeRedis:
    def __init__(self):
        self.kv, self.h, self.sets, self.lists, self.geo = {}, {}, {}, {}, {}
        self.subscribers: dict = {}
        self.published: list = []
        self.lock = threading.RLock()
        self.aio = _Async(self)

    # strings
    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, nx=False, ex=None):
        with self.lock:
            if nx and key in self.kv:
                return None
            self.kv[key] = value if isinstance(value, str) else str(value)
            return True

    def delete(self, *keys):
        for k in keys:
            for d in (self.kv, self.h, self.sets, self.lists, self.geo):
                d.pop(k, None)

    def expire(self, key, ttl):
        return True

    def incr(self, key):
        self.kv[key] = str(int(self.kv.get(key, 0)) + 1)
        return int(self.kv[key])

    def rename(self, src, dst):
        for d in (self.kv, self.h, self.sets, self.lists):
            if src in d:
                d[dst] = d.pop(src)

    def pipeline(self, transaction=True):
        return _Pipeline(self)

    # hashes
    def hset(self, key, field=None, value=None, mapping=None):
        h = self.h.setdefault(key, {})
        if mapping:
            h.update({k: v if isinstance(v, str) else str(v) for k, v in mapping.items()})
        if field is not None:
            h[field] = value if isinstance(value, str) else str(value)

    def hgetall(self, key):
        return dict(self.h.get(key, {}))

    def hmget(self, key, fields):
        h = self.h.get(key, {})
        return [h.get(f) for f in fields]

    def hincrby(self, key, field, n=1):
        h = self.h.setdefault(key, {})
        h[field] = str(int(h.get(field, 0)) + n)
        return int(h[field])

    # sets
    def sadd(self, key, *members):
        self.sets.setdefault(key, set()).update(members)

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    # lists
    def lpush(self, key, *values):
        lst = self.lists.setdefault(key, [])
        for v in values:
            lst.insert(0, v if isinstance(v, str) else str(v))
        return len(lst)

    def ltrim(self, key, start, end):
        lst = self.lists.get(key, [])
        self.lists[key] = lst[start:(end + 1 if end >= 0 else None)]

    def lrange(self, key, start, end):
        lst = self.lists.get(key, [])
        return lst[start:(end + 1 if end >= 0 else None)]

    # geo (members: (lon, lat))
    def geoadd(self, key, values):
        lon, lat, member = values
        self.geo.setdefault(key, {})[member] = (float(lon), float(lat))

    def zrange(self, key, a, b):
        return list(self.geo.get(key, {}))

    def zrem(self, key, member):
        self.geo.get(key, {}).pop(member, None)

    def geosearch(self, name, longitude, latitude, radius, unit="m", withdist=False, sort=None):
        out = []
        for m, (lon, lat) in self.geo.get(name, {}).items():
            d = _haversine(latitude, longitude, lat, lon)
            if d <= radius:
                out.append([m, d])
        out.sort(key=lambda x: x[1])
        return out if withdist else [m for m, _ in out]

    # pub/sub
    def publish(self, channel, data):
        with self.lock:
            self.published.append((channel, data))
            subs = list(self.subscribers.get(channel, ()))
        for ps in subs:
            ps.queue.append({"type": "message", "channel": channel, "data": data})
        return len(subs)

    def pubsub(self):
        return FakePubSub(self)


class _Async:
    """Async face of the same store (get_redis())."""

    def __init__(self, store: FakeRedis):
        self._store = store

    def pubsub(self):
        return self._store.pubsub()

    def __getattr__(self, name):
        fn = getattr(self._store, name)

        async def call(*a, **kw):
            return fn(*a, **kw)
        return call


def _haversine(lat1, lon1, lat2, lon2):
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
