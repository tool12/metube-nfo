from unittest.mock import Mock
import pytest
from nfo_genres import SongGenres, song_title


@pytest.mark.parametrize('title', ['Finesse (Remix)', 'Finesse (James Hype Remix) [feat. Cardi B]', 'Finesse - Official Music Video'])
def test_version_qualifiers_are_ignored(title):
    assert song_title(title) == 'Finesse'


def test_meaningful_title_parentheses_are_preserved():
    assert song_title("Song (Don't Go)") == "Song (Don't Go)"
    assert song_title('Live Forever') == 'Live Forever'


def track(title='Finesse', artist='Bruno Mars', genre='Pop', identifier=1):
    return {'kind': 'song', 'artistName': artist, 'trackName': title,
            'primaryGenreName': genre, 'trackId': identifier}


def test_base_song_wins_over_remix_and_result_is_cached(tmp_path):
    provider = SongGenres(tmp_path)
    provider._get = Mock(return_value={'results': [track('Finesse (Remix)', genre='R&B/Soul', identifier=2), track()]})
    result = provider.lookup('Bruno Mars', 'Finesse (Remix)')
    assert result['genres'] == ['Pop']
    assert result['catalog_id'] == '1'
    assert provider.lookup('Bruno Mars', 'Finesse') == result
    assert provider._get.call_count == 1


def test_wrong_artist_or_title_never_supplies_genre():
    provider = SongGenres()
    provider._get = Mock(return_value={'results': [track(artist='Other'), track(title='Grenade')]})
    assert provider.lookup('Bruno Mars', 'Finesse') is None


def test_multiple_version_genres_are_retained_without_base_entry():
    provider = SongGenres()
    provider._get = Mock(return_value={'results': [track('Finesse (Remix)', genre='Dance'), track('Finesse (Live)', genre='Pop', identifier=2)]})
    assert provider.lookup('Bruno Mars', 'Finesse')['genres'] == ['Dance', 'Pop']


def test_no_match_is_cached(tmp_path):
    provider = SongGenres(tmp_path)
    provider._get = Mock(return_value={'results': []})
    assert provider.lookup('Bruno Mars', 'Finesse') is None
    assert provider.lookup('Bruno Mars', 'Finesse (Remix)') is None
    provider._get.assert_called_once()



def test_no_one_retains_three_genres_and_deduplicates_case():
    provider = SongGenres()
    provider._get = Mock(return_value={'results': [
        track('No One', 'Alicia Keys', 'Pop', 1),
        track('No One', 'Alicia Keys', 'pop', 2),
        track('No One', 'Alicia Keys', 'R&B/Soul', 3),
        track('No One', 'Alicia Keys', 'Contemporary R&B', 4),
        track('No One (Live)', 'Alicia Keys', 'Rock', 5),
        track('No One', 'Another Artist', 'Country', 6)]})
    result = provider.lookup('Alicia Keys', 'No One')
    assert result['genres'] == ['Pop', 'Contemporary R&B', 'R&B/Soul']
    assert result['catalog_ids'] == ['1', '2', '3', '4']


def test_genres_are_limited_to_three_by_frequency():
    provider = SongGenres()
    provider._get = Mock(return_value={'results': [
        track(genre='Rock', identifier=1), track(genre='Pop', identifier=2),
        track(genre='Soul', identifier=3), track(genre='Rock', identifier=4),
        track(genre='Dance', identifier=5)]})
    assert provider.lookup('Bruno Mars', 'Finesse')['genres'] == ['Rock', 'Dance', 'Pop']


def test_old_empty_cache_does_not_hide_new_multi_genre_result(tmp_path):
    import hashlib, json, time
    from nfo_genres import normalized
    old_key = hashlib.sha256((normalized('Alicia Keys') + ':' + normalized('No One')).encode()).hexdigest()
    (tmp_path / (old_key + '.json')).write_text(json.dumps({'time': time.time(), 'value': None}))
    provider = SongGenres(tmp_path)
    provider._get = Mock(return_value={'results': [track('No One', 'Alicia Keys', 'Pop')]})
    assert provider.lookup('Alicia Keys', 'No One')['genres'] == ['Pop']
    provider._get.assert_called_once()
