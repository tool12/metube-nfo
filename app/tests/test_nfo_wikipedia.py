from unittest.mock import Mock
import pytest
from nfo_wikipedia import SongDescription, article_parts

LINKS = {'wikidata_ids': ['Q123'], 'wikipedia_urls': [],
         'musicbrainz_entity': 'release-group', 'musicbrainz_id': 'confirmed-id'}


def article(language='es', **extra):
    page = dict(ns=0, title='No One', fullurl=f'https://{language}.wikipedia.org/wiki/No_One',
                extract='Descripción de la canción.', revisions=[{'revid': 123}], **extra)
    return {'query': {'pages': [page], 'rightsinfo': {
        'url': 'https://creativecommons.org/licenses/by-sa/4.0/'}}}


def provider(tmp_path=None):
    mb = Mock()
    mb.wikipedia_links.return_value = LINKS
    p = SongDescription(mb, tmp_path)
    p._api = Mock(side_effect=[{'entities': {'Q123': {'sitelinks': {
        'eswiki': {'url': 'https://es.wikipedia.org/wiki/No_One'},
        'enwiki': {'url': 'https://en.wikipedia.org/wiki/No_One'}}}}}, article()])
    return p


def test_linked_spanish_article_and_cache(tmp_path):
    p = provider(tmp_path)
    value = p.lookup('Alicia Keys', 'No One (Remix)')
    assert value['language'] == 'es'
    assert value['revision_id'] == '123'
    assert value['musicbrainz_entity'] == 'release-group'
    assert p.lookup('Alicia Keys', 'No One') == value
    assert p._api.call_count == 2
    p.musicbrainz.wikipedia_links.assert_called_once()
    assert p._api.call_args.kwargs['titles'] == 'No One'
    assert 'search' not in str(p._api.call_args_list)


@pytest.mark.parametrize('bad', [dict(missing=True), dict(pageprops={'disambiguation': ''}), dict(extract='')])
def test_invalid_spanish_article_falls_back_to_english(bad):
    p = provider()
    es = article()
    es['query']['pages'][0].update(bad)
    p._api.side_effect = [p._api.side_effect.__next__(), es, article('en')]
    assert p.lookup('Alicia Keys', 'No One')['language'] == 'en'


@pytest.mark.parametrize('url', ['http://en.wikipedia.org/wiki/Song',
    'https://evil.example/wiki/Song', 'https://en.wikipedia.org.evil.example/wiki/Song',
    'https://en.wikipedia.org/wiki/Song?x=1', 'https://en.wikipedia.org/wiki/'])
def test_arbitrary_article_urls_are_rejected(url):
    assert article_parts(url) is None


def test_no_musicbrainz_link_never_searches_wikipedia(tmp_path):
    p = provider(tmp_path)
    p.musicbrainz.wikipedia_links.return_value = None
    assert p.lookup('Artist', 'Song') is None
    assert p.lookup('Artist', 'Song') is None
    p.musicbrainz.wikipedia_links.assert_called_once()
    p._api.assert_not_called()


def test_wikidata_canonical_link_replaces_direct_alias():
    p = provider()
    p.musicbrainz.wikipedia_links.return_value = dict(LINKS,
        wikipedia_urls=['https://es.wikipedia.org/wiki/No_One_alias'])
    assert p.lookup('Alicia Keys', 'No One')['language'] == 'es'


def test_ambiguous_wikidata_is_omitted():
    p = provider()
    p.musicbrainz.wikipedia_links.return_value = dict(LINKS, wikidata_ids=['Q123', 'Q456'])
    assert p.lookup('Alicia Keys', 'No One') is None
    p._api.assert_not_called()


def test_unlicensed_extract_is_omitted():
    p = provider()
    response = article()
    response['query']['rightsinfo']['url'] = 'https://example.org/license'
    p.musicbrainz.wikipedia_links.return_value = dict(LINKS, wikidata_ids=[],
        wikipedia_urls=['https://es.wikipedia.org/wiki/No_One'])
    p._api.return_value = response
    p._api.side_effect = None
    assert p.lookup('Alicia Keys', 'No One') is None
