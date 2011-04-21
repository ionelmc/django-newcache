"""
Django 1.3+ refactor of `newcache` that reduces redundancies between
the original module and the new (Django 1.3+) built-in support for
versioning, key prefixes, and PylibMC.
"""
import time

from django.core.cache.backends.memcached import BaseMemcachedCache, MemcachedCache, PyLibMCCache
from django.conf import settings

CACHE_HERD_TIMEOUT = getattr(settings, 'CACHE_HERD_TIMEOUT', 60)

class Marker(object):
    pass

MARKER = Marker()

class BaseNewCache(BaseMemcachedCache):
    """
    Base class that adds "thundering herd" mitigation to the Django 1.3+
    memcached cache classes.
    
    Use one of the subclasses below.
    """
    def _pack_value(self, value, timeout):
        """
        Packs a value to include a marker (to indicate that it's a packed
        value), the value itself, and the value's timeout information.
        """
        herd_timeout = (timeout or self.default_timeout) + int(time.time())
        return (MARKER, value, herd_timeout)

    def _unpack_value(self, value, default=None):
        """
        Unpacks a value and returns a tuple whose first element is the value,
        and whose second element is whether it needs to be herd refreshed.
        """
        try:
            marker, unpacked, herd_timeout = value
        except (ValueError, TypeError):
            return value, False
        if not isinstance(marker, Marker):
            return value, False
        if herd_timeout < int(time.time()):
            return unpacked, True
        return unpacked, False

    def add(self, key, value, timeout=0, version=None, herd=True):
        # If the user chooses to use the herd mechanism, then encode some
        # timestamp information into the object to be persisted into memcached
        if herd and timeout != 0:
            if timeout is None:
                timeout = self.default_timeout
            packed = self._pack_value(value, timeout)
            real_timeout = self._get_memcache_timeout(timeout +
                CACHE_HERD_TIMEOUT)
        else:
            packed = value
            real_timeout = self._get_memcache_timeout(timeout)

        key = self.make_key(key, version=version)
        return self._cache.add(key, packed, real_timeout)

    def get(self, key, default=None, version=None):
        key = self.make_key(key, version=version)
        packed = self._cache.get(encoded_key)
        if packed is None:
            return default

        val, refresh = self._unpack_value(packed)

        # If the cache has expired according to the embedded timeout, then
        # shove it back into the cache for a while, but act as if it was a
        # cache miss.
        if refresh:
            self._cache.set(key, val,
                self._get_memcache_timeout(CACHE_HERD_TIMEOUT))
            return default

        return val

    def set(self, key, value, timeout=0, version=None, herd=True):
        # If the user chooses to use the herd mechanism, then encode some
        # timestamp information into the object to be persisted into memcached
        if herd and timeout != 0:
            if timeout is None:
                timeout = self.default_timeout
            packed = self._pack_value(value, timeout)
            real_timeout = self._get_memcache_timeout(timeout +
                CACHE_HERD_TIMEOUT)
        else:
            packed = value
            real_timeout = self._get_memcache_timeout(timeout)

        key = self.make_key(key, version=version)
        return self._cache.set(key, packed, real_timeout)

    def get_many(self, keys, version=None):
        # First, map all of the keys through our key function
        rvals = map(lambda k: self.make_key(k, version=version), keys)

        packed_resp = self._cache.get_multi(rvals)

        resp = {}
        reinsert = {}

        for key, packed in packed_resp.iteritems():
            # If it was a miss, treat it as a miss to our response & continue
            if packed is None:
                resp[key] = packed
                continue

            val, refresh = self._unpack_value(packed)
            if refresh:
                reinsert[key] = val
                resp[key] = None
            else:
                resp[key] = val

        # If there are values to re-insert for a short period of time, then do
        # so now.
        if reinsert:
            self._cache.set_multi(reinsert,
                self._get_memcache_timeout(CACHE_HERD_TIMEOUT))

        # Build a reverse map of encoded keys to the original keys, so that
        # the returned dict's keys are what users expect (in that they match
        # what the user originally entered)
        reverse = dict(zip(rvals, keys))

        return dict(((reverse[k], v) for k, v in resp.iteritems()))

    def set_many(self, data, timeout=0, version=None, herd=True):
        safe_data = {}
        if herd and timeout != 0:
            for key, value in data.items():
                key = self.make_key(key, version=version)
                safe_data[key] = self._pack_value(value, timeout)
        else:
            for key, value in data.items():
                key = self.make_key(key, version=version)
                safe_data[key] = value
        self._cache.set_multi(safe_data, self._get_memcache_timeout(timeout))

class MemcachedNewCache(BaseNewCache, MemcachedCache):
    pass

class PyLibMCNewCache(BaseNewCache, PyLibMCCache):
    @property
    def _cache(self):
        """
        Overrides the default `_cache` property by allowing binary
        protocol connections if `binary` is in the OPTIONS dict.
        """
        # PylibMC uses cache options as the 'behaviors' attribute.
        # It also needs to use threadlocals, because some versions of
        # PylibMC don't play well with the GIL.
        client = getattr(self._local, 'client', None)
        if client:
            return client

        if self._options and self._options.get("binary", False):
            client = self._lib.Client(self._servers, binary=True)
        else:
            client = self._lib.Client(self._servers)

        if self._options:
            client.behaviors = self._options

        self._local.client = client

        return client
