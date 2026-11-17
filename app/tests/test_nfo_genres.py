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


def test_conflicting_version_genres_are_omitted():
    provider = SongGenres()
    provider._get = Mock(return_value={'results': [track('Finesse (Remix)', genre='Dance'), track('Finesse (Live)', genre='Pop', identifier=2)]})
    assert provider.lookup('Bruno Mars', 'Finesse') is None


def test_no_match_is_cached(tmp_path):
    provider = SongGenres(tmp_path)
    provider._get = Mock(return_value={'results': []})
    assert provider.lookup('Bruno Mars', 'Finesse') is None
    assert provider.lookup('Bruno Mars', 'Finesse (Remix)') is None
    provider._get.assert_called_once()
