"""Song-level identification only; never a replacement for exact video credits."""
import hashlib
import json
import logging
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlencode

from nfo_genres import CatalogRequests, SongGenres, normalized, song_title

log = logging.getLogger('ytdl')
MBID = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'


def quoted(value):
    return '"' + str(value)[:200].replace('\\', '\\\\').replace('"', '\\"') + '"'


class MusicBrainzSongs(CatalogRequests):
    def __init__(self, cache_dir=None):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.deadline = 0

    def _get(self, endpoint, **params):
        if not re.fullmatch(r'(?:recording|release-group)(?:/' + MBID + r')?', endpoint):
            raise ValueError('Unsupported MusicBrainz endpoint')
        url = 'https://musicbrainz.org/ws/2/' + endpoint + '?' + urlencode(dict(params, fmt='json'))
        return self._request(url, 1.1, 'metube-musicbrainz-rate.lock')

    def _path(self, kind, artist, title):
        key = hashlib.sha256((kind + ':' + normalized(artist) + ':' + normalized(title)).encode()).hexdigest()
        return self.cache_dir / (key + '.json') if self.cache_dir else None

    def _read(self, path):
        if path:
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
                ttl = 604800 if data.get('value') else 86400
                if 0 <= time.time() - data['time'] < ttl:
                    return True, data['value']
            except (OSError, ValueError, TypeError, KeyError):
                pass
        return False, None

    def _save(self, path, value):
        temporary = None
        if path:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump({'time': time.time(), 'value': value}, stream)
                temporary.replace(path)
            except OSError:
                pass
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        return value

    @staticmethod
    def _artist(item, expected):
        matches = []
        for credit in item.get('artist-credit') or []:
            if not isinstance(credit, dict):
                continue
            artist = credit.get('artist') or {}
            if (normalized(expected) in {normalized(artist.get('name')), normalized(credit.get('name'))}
                    and re.fullmatch(MBID, str(artist.get('id') or ''))):
                matches.append(artist)
        return matches

    def _find_song(self, artist, title):
        path = self._path('song-v1', artist, title)
        hit, value = self._read(path)
        if hit:
            return value
        response = self._get('recording', query='artist:' + quoted(artist) + ' AND recording:' + quoted(title), limit=25)
        records = []
        for item in response.get('recordings') or []:
            if (normalized(song_title(item.get('title'))) == normalized(title)
                    and re.fullmatch(MBID, str(item.get('id') or ''))):
                records.extend((item, match) for match in self._artist(item, artist))
        # Homonymous artists cannot be resolved by an artist/title name alone.
        artists = {match['id'] for _, match in records}
        value = None
        if len(artists) == 1:
            plain = [(item, match) for item, match in records if normalized(item.get('title')) == normalized(title)]
            selected = plain or records
            value = {'artist': selected[0][1]['name'], 'artist_id': selected[0][1]['id'],
                     'song_title': title,
                     'recording_ids': list(dict.fromkeys(item['id'] for item, _ in selected))[:2]}
        return self._save(path, value)

    def resolve_artist(self, info):
        # Splitting is merely a search hint. No folder is accepted until both
        # names are confirmed against a unique MusicBrainz artist identity.
        title = str(info.get('_metube_source_title') or info.get('title') or '')
        parts = re.split(r'\s+[-–—]\s+', title, maxsplit=1)
        if len(parts) != 2 or not all(part.strip() for part in parts):
            return None
        artist, title = parts[0].strip(), song_title(parts[1])
        self.deadline = time.monotonic() + 8
        return self._find_song(artist, title) if title else None

    def _genres(self, entity, identifiers):
        scores, names = {}, {}
        contributing = []
        for identifier in identifiers[:2]:
            response = self._get(entity + '/' + identifier, inc='genres')
            found = False
            for genre in response.get('genres') or []:
                name = str(genre.get('name') or '').strip()
                count = int(genre.get('count') or 0)
                if name and count > 0:
                    key = name.casefold()
                    scores[key] = scores.get(key, 0) + count
                    names.setdefault(key, name)
                    found = True
            if found:
                contributing.append(identifier)
        ranked = sorted(scores, key=lambda name: (-scores[name], name))[:3]
        if ranked:
            return {'genres': [names[name] for name in ranked], 'provider': 'musicbrainz',
                    'catalog_entity': entity, 'catalog_id': contributing[0], 'catalog_ids': contributing,
                    'match_basis': 'artist_and_base_song_title; version_not_verified'}
        return None

    def lookup_genres(self, artist, title):
        artist, title = str(artist or '').strip(), song_title(title)
        if not artist or not title:
            return None
        path = self._path('genres-v1', artist, title)
        hit, value = self._read(path)
        if hit:
            return value
        self.deadline = time.monotonic() + 18
        song = self._find_song(artist, title)
        if not song:
            return self._save(path, None)
        value = self._genres('recording', song['recording_ids'])
        if not value:
            # Song singles/EPs often carry genres absent on their recordings.
            response = self._get('release-group', query='artist:' + quoted(artist) + ' AND releasegroup:' + quoted(title), limit=10)
            ids = []
            for item in response.get('release-groups') or []:
                if (normalized(song_title(item.get('title'))) == normalized(title)
                        and item.get('primary-type') in ('Single', 'EP')
                        and re.fullmatch(MBID, str(item.get('id') or ''))
                        and any(a['id'] == song['artist_id'] for a in self._artist(item, artist))):
                    ids.append(item['id'])
            if ids:
                value = self._genres('release-group', list(dict.fromkeys(ids)))
        return self._save(path, value)


class PreferredGenres:
    def __init__(self, musicbrainz, apple=None, enabled=True):
        self.musicbrainz = musicbrainz
        self.apple = apple if apple is not None else SongGenres()
        self.enabled = enabled

    def lookup(self, artist, title):
        if self.enabled:
            try:
                result = self.musicbrainz.lookup_genres(artist, title)
                if result:
                    return result
            except Exception as error:
                # No exception messages, request headers or credential values.
                log.warning('MusicBrainz genre lookup failed (%s); trying Apple', type(error).__name__)
        return self.apple.lookup(artist, title)
