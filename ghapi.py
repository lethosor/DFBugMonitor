import json, time, urllib2

_cache = {}

class CacheEntry:
    def __init__(self, contents):
        self.contents = contents
        self.time = time.time()

    @property
    def expired(self):
        return time.time() > self.time + 3600

def request(url, ignore_cache=False):
    if not url.startswith('https://'):
        url = 'https://api.github.com/' + url
    if url not in _cache or or _cache[url].expired or ignore_cache:
        _cache[url] = CacheEntry(json.load(urllib2.urlopen(url)))
    return _cache[url].contents

def clear_cache():
    _cache.clear()
