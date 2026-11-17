"""Optional song genres, independent of exact music-video identification."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unicodedata
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler


def song_title(value):
    """Ignore version/display suffixes, not arbitrary parts of a song name."""
    value = str(value or '')
    qualifier = r'\b(remix|mix|live|version|edit|remaster\w*|official|video|audio|lyrics?|feat\.?|ft\.?)\b'
    value = re.sub(r'[\[(]([^\])]+)[\])]',
                   lambda m: '' if re.search(qualifier, m[1], re.I) else m[0], value)
    value = re.sub(r'\s+[-–—]\s+.*' + qualifier + r'.*$', '', value, flags=re.I)
    value = re.sub(r'\s+(?:feat\.?|ft\.?)\s+.*$', '', value, flags=re.I)
    return ' '.join(value.split()).strip()


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', str(value)).casefold()
                   if c.isalnum() and not unicodedata.combining(c))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_thread_lock = threading.Lock()


class SongGenres:
    def __init__(self, cache_dir=None):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.deadline = 0

    def _get(self, endpoint, **params):
        # Shared across download processes, including when STATE_DIR is absent.
        lock_path = Path(tempfile.gettempdir()) / 'metube-song-genres-rate.lock'
        with _thread_lock, lock_path.open('a+') as lock:
            try:
                import fcntl
            except ImportError:  # Native Windows development; Linux uses flock.
                fcntl = None
            acquired = False
            try:
                if fcntl:
                    while not acquired:
                        if time.monotonic() >= self.deadline:
                            raise TimeoutError('Genre lookup budget exhausted')
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            acquired = True
                        except BlockingIOError:
                            time.sleep(0.05)
                lock.seek(0)
                try:
                    previous = float(lock.read() or '0')
                except ValueError:
                    previous = 0
                delay = max(0, min(3.1, previous + 3.1 - time.time()))
                if time.monotonic() + delay >= self.deadline:
                    raise TimeoutError('Genre lookup budget exhausted')
                time.sleep(delay)
                lock.seek(0)
                lock.truncate()
                lock.write(str(time.time()))
                lock.flush()
                url = 'https://itunes.apple.com/search?' + urlencode(params)
                request = Request(url, headers={
                    'User-Agent': 'MeTube-NFO/1 (https://github.com/tool12/metube-nfo)',
                    'Accept': 'application/json',
                })
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Genre lookup budget exhausted')
                with build_opener(_NoRedirect()).open(request, timeout=min(5, remaining)) as response:
                    raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ValueError('Genre response too large')
                return json.loads(raw)
            finally:
                if fcntl and acquired:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def lookup(self, artist, title):
        title = song_title(title)
        artist = str(artist or '').strip()
        if not artist or not title:
            return None
        key = hashlib.sha256(('multi-genre-v2:' + normalized(artist) + ':' + normalized(title)).encode()).hexdigest()
        cache = self.cache_dir / (key + '.json') if self.cache_dir else None
        if cache:
            try:
                saved = json.loads(cache.read_text(encoding='utf-8'))
                ttl = 604800 if saved.get('value') else 86400
                if 0 <= time.time() - saved['time'] < ttl:
                    return saved['value']
            except (OSError, ValueError, KeyError, TypeError):
                pass
        self.deadline = time.monotonic() + 10
        results = self._get('search', term=(artist + ' ' + title)[:200], entity='song', limit=50)
        candidates = [item for item in results.get('results') or []
                      if item.get('kind') == 'song'
                      and normalized(item.get('artistName')) == normalized(artist)
                      and normalized(song_title(item.get('trackName'))) == normalized(title)
                      and str(item.get('trackId', '')).isdecimal()]
        # Prefer the unqualified base song over a remix/live/edit entry.
        plain = [item for item in candidates if normalized(item.get('trackName')) == normalized(title)]
        selected = plain or candidates
        # Editions can legitimately classify the same song differently. Keep
        # up to three distinct genres, ordered by frequency then name.
        counts = {}
        names = {}
        for item in selected:
            name = str(item.get('primaryGenreName') or '').strip()
            if name:
                key_name = name.casefold()
                counts[key_name] = counts.get(key_name, 0) + 1
                names.setdefault(key_name, name)
        ranked = sorted(counts, key=lambda name: (-counts[name], name))[:3]
        value = None
        if ranked:
            contributing = [item for item in selected
                            if str(item.get('primaryGenreName') or '').strip().casefold() in ranked]
            catalog_ids = list(dict.fromkeys(str(item['trackId']) for item in contributing))
            value = {'genres': [names[name] for name in ranked], 'provider': 'apple_itunes',
                     'catalog_id': catalog_ids[0], 'catalog_ids': catalog_ids,
                     'match_basis': 'artist_and_base_song_title; version_not_verified'}
        if cache:
            temporary = None
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=cache.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump({'time': time.time(), 'value': value}, stream)
                temporary.replace(cache)
            except OSError:
                pass
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        return value
