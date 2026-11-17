"""Song-level identification only; never a replacement for exact video credits."""
import hashlib
import json
import logging
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlencode, urlsplit
from urllib.error import HTTPError

from nfo_genres import CatalogRequests, SongGenres, normalized, song_title

log = logging.getLogger('ytdl')
MBID = r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}'


def quoted(value):
    return '"' + str(value)[:200].replace('\\', '\\\\').replace('"', '\\"') + '"'


def wikipedia_relations(item):
    qids, pages = set(), set()
    for relation in item.get('relations') or []:
        resource = str((relation.get('url') or {}).get('resource') or '')
        parsed = urlsplit(resource)
        if parsed.scheme not in ('http', 'https') or parsed.query:
            continue
        if (relation.get('type') == 'wikidata'
                and parsed.netloc in ('www.wikidata.org', 'wikidata.org')
                and re.fullmatch(r'/wiki/Q[1-9][0-9]*', parsed.path)):
            qids.add(parsed.path.rsplit('/', 1)[1])
        elif (relation.get('type') == 'wikipedia'
                and parsed.netloc in ('es.wikipedia.org', 'en.wikipedia.org')
                and parsed.path.startswith('/wiki/')):
            pages.add('https://' + parsed.netloc + parsed.path)
    if len(qids) > 1:
        return None
    return {'wikidata_ids': sorted(qids), 'wikipedia_urls': sorted(pages)} if qids or pages else None


class MusicBrainzSongs(CatalogRequests):
    def __init__(self, cache_dir=None):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.deadline = 0

    def _get(self, endpoint, **params):
        if not re.fullmatch(r'(?:recording|release-group|work)(?:/' + MBID + r')?', endpoint):
            raise ValueError('Unsupported MusicBrainz endpoint')
        url = 'https://musicbrainz.org/ws/2/' + endpoint + '?' + urlencode(dict(params, fmt='json'))
        for attempt in range(2):
            try:
                return self._request(url, 1.1, 'metube-musicbrainz-rate.lock')
            except HTTPError as error:
                # One transient-overload retry, within the existing lookup budget.
                # The shared transport also rate-limits this second request.
                if error.code != 503 or attempt or time.monotonic() + 1.1 >= self.deadline:
                    raise

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
        path = self._path('song-v2', artist, title)
        hit, value = self._read(path)
        if hit:
            return value
        response = self._get('recording', query='artist:' + quoted(artist) + ' AND recording:' + quoted(title), limit=25)
        records = []
        for item in response.get('recordings') or []:
            if (normalized(song_title(item.get('title'))) == normalized(title)
                    and re.fullmatch(MBID, str(item.get('id') or ''))):
                records.extend((item, match) for match in self._artist(item, artist))
        # A different canonical artist can be credited using this artist's name
        # (e.g. a cover). Prefer canonical-name matches when present, retaining
        # credit-name aliases only when no canonical match exists.
        canonical = [(item, match) for item, match in records
                     if normalized(match.get('name')) == normalized(artist)]
        records = canonical or records
        # Homonymous canonical artists still cannot be resolved by names alone.
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

    def _group_ids(self, artist, title, artist_id):
        path = self._path('groups-v2', artist, title)
        hit, value = self._read(path)
        if hit:
            return value or []
        genre_hit, genre = self._read(self._path('genres-v2', artist, title))
        if genre_hit and genre and genre.get('provider') == 'musicbrainz' and genre.get('catalog_entity') == 'release-group':
            ids = [identifier for identifier in genre.get('catalog_ids') or []
                   if re.fullmatch(MBID, str(identifier))]
            if ids:
                return self._save(path, ids[:2])
        response = self._get('release-group', query='arid:' + artist_id + ' AND releasegroup:' + quoted(title), limit=10)
        groups = [item for item in response.get('release-groups') or []
                  if normalized(song_title(item.get('title'))) == normalized(title)
                  and item.get('primary-type') in ('Single', 'EP')
                  and re.fullmatch(MBID, str(item.get('id') or ''))
                  and any(isinstance(credit, dict) and (credit.get('artist') or {}).get('id') == artist_id
                          for credit in item.get('artist-credit') or [])]
        plain = [item for item in groups if normalized(item.get('title')) == normalized(title)]
        ids = list(dict.fromkeys(item['id'] for item in plain or groups))[:2]
        self._save(path, ids or None)
        return ids

    def wikipedia_links(self, artist, title, deadline=None):
        artist, title = str(artist or '').strip(), song_title(title)
        if not artist or not title:
            return None
        path = self._path('wikipedia-links-v2', artist, title)
        hit, value = self._read(path)
        if hit:
            return value
        self.deadline = min(deadline or float('inf'), time.monotonic() + 14)
        song = self._find_song(artist, title)
        if not song:
            return self._save(path, None)
        ids = self._group_ids(artist, title, song['artist_id'])
        for identifier in ids:
            response = self._get('release-group/' + identifier, inc='url-rels')
            links = wikipedia_relations(response)
            if links:
                links.update(musicbrainz_entity='release-group', musicbrainz_id=identifier)
                return self._save(path, links)
        for identifier in song['recording_ids'][:2]:
            response = self._get('recording/' + identifier, inc='url-rels+work-rels')
            links = wikipedia_relations(response)
            if links:
                links.update(musicbrainz_entity='recording', musicbrainz_id=identifier)
                return self._save(path, links)
            for relation in response.get('relations') or []:
                work = relation.get('work') or {}
                if (relation.get('type') == 'performance'
                        and normalized(song_title(work.get('title'))) == normalized(title)
                        and re.fullmatch(MBID, str(work.get('id') or ''))):
                    links = wikipedia_relations(self._get('work/' + work['id'], inc='url-rels'))
                    if links:
                        links.update(musicbrainz_entity='work', musicbrainz_id=work['id'])
                        return self._save(path, links)
                    break
        return self._save(path, None)

    def lookup_genres(self, artist, title):
        artist, title = str(artist or '').strip(), song_title(title)
        if not artist or not title:
            return None
        path = self._path('genres-v2', artist, title)
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
            ids = self._group_ids(artist, title, song['artist_id'])
            if ids:
                value = self._genres('release-group', ids)
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
