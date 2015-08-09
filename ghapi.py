import json, urllib2

_cache = {}

def request(url, ignore_cache=False):
    if not url.startswith('https://'):
        url = 'https://api.github.com/' + url
    if url not in _cache or ignore_cache:
        _cache[url] = json.load(urllib2.urlopen(url))
    return _cache[url]

def clear_cache():
    _cache.clear()
