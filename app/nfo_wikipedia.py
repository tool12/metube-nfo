"""Bounded Wikipedia introduction, only via a confirmed MusicBrainz song link."""
import hashlib
import json
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlencode, urlsplit, unquote

from nfo_genres import CatalogRequests, normalized, song_title


def article_parts(url):
    parsed = urlsplit(str(url))
    if (parsed.scheme != 'https' or parsed.netloc not in ('es.wikipedia.org', 'en.wikipedia.org')
            or parsed.query or not parsed.path.startswith('/wiki/') or not parsed.path[6:]):
        return None
    return parsed.netloc[:2], unquote(parsed.path[6:]).replace('_', ' ')


class SongDescription(CatalogRequests):
    def __init__(self, musicbrainz, cache_dir=None, language='es'):
        self.musicbrainz = musicbrainz
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.language = language if language in ('es', 'en') else 'es'
        self.deadline = 0

    def _api(self, service, **params):
        hosts = {'wikidata': 'www.wikidata.org', 'es': 'es.wikipedia.org', 'en': 'en.wikipedia.org'}
        if service not in hosts:
            raise ValueError('Unsupported encyclopedia host')
        url = 'https://' + hosts[service] + '/w/api.php?' + urlencode(dict(params, format='json', formatversion=2))
        return self._request(url, 1.1, 'metube-wikimedia-rate.lock')

    def lookup(self, artist, title):
        artist, title = str(artist or '').strip(), song_title(title)
        if not artist or not title:
            return None
        key = hashlib.sha256(('description-v2:' + self.language + ':' + normalized(artist) + ':' + normalized(title)).encode()).hexdigest()
        cache = self.cache_dir / (key + '.json') if self.cache_dir else None
        if cache:
            try:
                saved = json.loads(cache.read_text(encoding='utf-8'))
                ttl = 604800 if saved.get('value') else 86400
                if 0 <= time.time() - saved['time'] < ttl:
                    return saved['value']
            except (OSError, ValueError, TypeError, KeyError):
                pass
        self.deadline = time.monotonic() + 22
        links = self.musicbrainz.wikipedia_links(artist, title, deadline=self.deadline)
        value = self._extract(links) if links else None
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
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
        return value

    def _extract(self, links):
        urls = [url for url in links.get('wikipedia_urls') or [] if article_parts(url)]
        qids = links.get('wikidata_ids') or []
        if qids:
            if len(qids) != 1 or not re.fullmatch(r'Q[1-9][0-9]*', qids[0]):
                return None
            data = self._api('wikidata', action='wbgetentities', ids=qids[0],
                             props='sitelinks/urls', sitefilter='eswiki|enwiki')
            entity = (data.get('entities') or {}).get(qids[0]) or {}
            for site in ('eswiki', 'enwiki'):
                url = ((entity.get('sitelinks') or {}).get(site) or {}).get('url')
                if url and article_parts(url):
                    language = article_parts(url)[0]
                    urls = [u for u in urls if article_parts(u)[0] != language]
                    urls.append(url)
        urls = list(dict.fromkeys(urls))
        urls.sort(key=lambda url: (article_parts(url)[0] != self.language, url))
        # Only one article per language; ambiguous same-language links are not
        # resolved by a guessed title search.
        for language in (self.language, 'en' if self.language == 'es' else 'es'):
            choices = [url for url in urls if article_parts(url)[0] == language]
            if len(choices) != 1:
                continue
            _, title = article_parts(choices[0])
            result = self._api(language, action='query', titles=title, redirects=1,
                               prop='extracts|info|pageprops|revisions', inprop='url',
                               exintro=1, explaintext=1, exchars=1200, rvprop='ids', rvlimit=1,
                               meta='siteinfo', siprop='rightsinfo')
            query = result.get('query') or {}
            pages = query.get('pages') or []
            if len(pages) != 1:
                continue
            page = pages[0]
            url = page.get('fullurl') or ''
            if (page.get('missing') or page.get('ns') != 0 or not article_parts(url)
                    or 'disambiguation' in (page.get('pageprops') or {})):
                continue
            text = str(page.get('extract') or '').strip()
            if not text:
                continue
            if len(text) > 1200:
                text = text[:1200].rsplit(' ', 1)[0] + '…'
            license_url = (query.get('rightsinfo') or {}).get('url') or ''
            if not license_url.startswith('https://creativecommons.org/licenses/by-sa/'):
                continue
            revisions = page.get('revisions') or []
            return {'text': text, 'url': url, 'language': language,
                    'title': page.get('title') or title, 'license_url': license_url,
                    'revision_id': str(revisions[0].get('revid') or '') if revisions else '',
                    'musicbrainz_entity': links.get('musicbrainz_entity'),
                    'musicbrainz_id': links.get('musicbrainz_id')}
        return None
