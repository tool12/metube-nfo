from unittest.mock import Mock
import pytest
from nfo_musicbrainz import MusicBrainzSongs, PreferredGenres

ARTIST = '8ef1df30-ae4f-4dbd-9351-1a32b208a01e'
REC = '2682b1c0-d322-4e14-b8de-d78a413876fe'
GROUP = 'c311efea-3e48-3403-97d7-152b45381f84'


def entry(identifier=REC, title='No One', artist='Alicia Keys', artist_id=ARTIST, **extra):
    return dict(id=identifier, title=title,
                **{'artist-credit': [{'artist': {'id': artist_id, 'name': artist}}]}, **extra)


def test_artist_requires_confirmed_song_and_unique_artist(tmp_path):
    provider = MusicBrainzSongs(tmp_path)
    provider._get = Mock(return_value={'recordings': [entry()]})
    result = provider.resolve_artist({'title': 'Alicia Keys - No One (Official Music Video)'})
    assert result['artist'] == 'Alicia Keys'
    assert result['artist_id'] == ARTIST
    assert provider.resolve_artist({'title': 'Alicia Keys - No One'}) == result
    assert provider._get.call_count == 1


@pytest.mark.parametrize('records', [
    [entry(artist='Another Artist')], [entry(title='Another Song')],
    [entry(), entry(artist_id='11111111-1111-1111-1111-111111111111')]])
def test_wrong_or_ambiguous_artist_song_is_rejected(records):
    provider = MusicBrainzSongs()
    provider._get = Mock(return_value={'recordings': records})
    assert provider.resolve_artist({'title': 'Alicia Keys - No One'}) is None


def test_channel_name_is_not_used_as_artist():
    provider = MusicBrainzSongs()
    provider._get = Mock()
    assert provider.resolve_artist({'title': 'No One', 'channel': 'Alicia Keys'}) is None
    provider._get.assert_not_called()


def test_recording_genres_are_cached_and_apple_is_not_called(tmp_path):
    mb = MusicBrainzSongs(tmp_path)
    mb._get = Mock(side_effect=[{'recordings': [entry()]}, {'genres': [
        {'name': 'soul', 'count': 3}, {'name': 'pop', 'count': 1}]}])
    apple = Mock()
    provider = PreferredGenres(mb, apple)
    result = provider.lookup('Alicia Keys', 'No One (Remix)')
    assert result['provider'] == 'musicbrainz'
    assert result['genres'] == ['soul', 'pop']
    assert provider.lookup('Alicia Keys', 'No One') == result
    apple.lookup.assert_not_called()
    assert mb._get.call_count == 2


def test_single_genres_are_used_when_recording_genres_are_absent():
    mb = MusicBrainzSongs()
    mb._get = Mock(side_effect=[{'recordings': [entry()]}, {'genres': []},
        {'release-groups': [entry(GROUP, **{'primary-type': 'Single'})]},
        {'genres': [{'name': 'soul', 'count': 4}, {'name': 'pop', 'count': 2},
                    {'name': 'Soul', 'count': 1}, {'name': 'noise', 'count': -1}]}])
    result = mb.lookup_genres('Alicia Keys', 'No One')
    assert result['genres'] == ['soul', 'pop']
    assert result['catalog_entity'] == 'release-group'
    assert result['catalog_id'] == GROUP


def test_album_genres_are_not_assigned_to_same_named_song():
    mb = MusicBrainzSongs()
    mb._get = Mock(side_effect=[{'recordings': [entry()]}, {'genres': []},
        {'release-groups': [entry(GROUP, **{'primary-type': 'Album'})]}])
    assert mb.lookup_genres('Alicia Keys', 'No One') is None
    assert mb._get.call_count == 3


@pytest.mark.parametrize('mode', ['empty', 'error', 'disabled'])
def test_apple_is_used_when_musicbrainz_has_no_genres_or_fails(mode):
    mb, apple = Mock(), Mock()
    mb.lookup_genres.return_value = None
    if mode == 'error':
        mb.lookup_genres.side_effect = TimeoutError('private error')
    apple.lookup.return_value = {'genres': ['Pop'], 'provider': 'apple_itunes'}
    result = PreferredGenres(mb, apple, enabled=mode != 'disabled').lookup('Artist', 'Song')
    assert result['provider'] == 'apple_itunes'
    apple.lookup.assert_called_once_with('Artist', 'Song')
    if mode == 'disabled':
        mb.lookup_genres.assert_not_called()


def test_empty_musicbrainz_results_are_cached(tmp_path):
    mb = MusicBrainzSongs(tmp_path)
    mb._get = Mock(return_value={'recordings': []})
    assert mb.lookup_genres('Artist', 'Song') is None
    assert mb.lookup_genres('Artist', 'Song') is None
    mb._get.assert_called_once()


def test_requests_are_fixed_host_and_rate_limited():
    mb = MusicBrainzSongs()
    mb._request = Mock(return_value={})
    mb._get('recording', query='artist:"Alicia Keys"', limit=25)
    url, interval, lock = mb._request.call_args.args
    assert url.startswith('https://musicbrainz.org/ws/2/recording?')
    assert interval == 1.1
    assert lock == 'metube-musicbrainz-rate.lock'
    with pytest.raises(ValueError):
        mb._get('https://evil.invalid/')


def test_wikipedia_uses_confirmed_single_and_cached_identity(tmp_path):
    mb = MusicBrainzSongs(tmp_path)
    mb._get = Mock(side_effect=[{'recordings': [entry()]},
        {'release-groups': [entry(GROUP, **{'primary-type': 'Single'})]},
        {'relations': [{'type': 'wikidata', 'url': {'resource': 'https://www.wikidata.org/wiki/Q123'}}]}])
    value = mb.wikipedia_links('Alicia Keys', 'No One')
    assert value['wikidata_ids'] == ['Q123']
    assert value['musicbrainz_id'] == GROUP
    assert mb.wikipedia_links('Alicia Keys', 'No One (Remix)') == value
    assert mb._get.call_count == 3


def test_wikipedia_never_uses_unconfirmed_artist_song():
    mb = MusicBrainzSongs()
    mb._get = Mock(return_value={'recordings': [entry(artist='Other Artist')]})
    assert mb.wikipedia_links('Alicia Keys', 'No One') is None
    assert mb._get.call_count == 1


def test_wikipedia_work_fallback():
    mb = MusicBrainzSongs()
    mb._get = Mock(side_effect=[{'recordings': [entry()]}, {'release-groups': []},
        {'relations': [{'type': 'performance', 'work': {'id': GROUP, 'title': 'No One'}}]},
        {'relations': [{'type': 'wikipedia', 'url': {'resource': 'https://en.wikipedia.org/wiki/No_One'}}]}])
    value = mb.wikipedia_links('Alicia Keys', 'No One')
    assert value['musicbrainz_entity'] == 'work'
    assert value['wikipedia_urls'] == ['https://en.wikipedia.org/wiki/No_One']


def test_wikipedia_reuses_confirmed_genre_release_group(tmp_path):
    mb = MusicBrainzSongs(tmp_path)
    mb._save(mb._path('genres-v1', 'Alicia Keys', 'No One'), {
        'provider': 'musicbrainz', 'catalog_entity': 'release-group',
        'catalog_ids': [GROUP], 'catalog_id': GROUP})
    mb._get = Mock(side_effect=[{'recordings': [entry()]},
        {'relations': [{'type': 'wikidata', 'url': {'resource': 'https://www.wikidata.org/wiki/Q123'}}]}])
    assert mb.wikipedia_links('Alicia Keys', 'No One')['musicbrainz_id'] == GROUP
    assert mb._get.call_count == 2


def test_wikipedia_relationship_host_validation():
    from nfo_musicbrainz import wikipedia_relations
    assert wikipedia_relations({'relations': [
        {'type': 'wikipedia', 'url': {'resource': 'https://evil.example/wiki/Song'}}]}) is None
    assert wikipedia_relations({'relations': [
        {'type': 'wikidata', 'url': {'resource': 'https://www.wikidata.org/wiki/Q123'}},
        {'type': 'wikidata', 'url': {'resource': 'https://www.wikidata.org/wiki/Q456'}}]}) is None
