from unittest.mock import Mock
import xml.etree.ElementTree as ET

import pytest

from nfo_backfill import merge_missing, plan_nfo, apply_nfo
from nfo_metadata import IMVDbProvider


def test_merge_preserves_user_values_unknown_fields_and_all_new_credits():
    old = ET.fromstring('<musicvideo><title>My title</title><year>1999</year>'
                        '<artist>My artist</artist><custom>Keep me</custom><premiered/></musicvideo>')
    new = ET.fromstring('<musicvideo><title>Other title</title><year>2000</year>'
                        '<artist>Other artist</artist><premiered>2000-01-02</premiered>'
                        '<director>A</director><director>B</director><tag>Reference</tag></musicvideo>')
    result = merge_missing(old, new)
    assert result.findtext('title') == 'My title'
    assert result.findtext('year') == '1999'
    assert result.findtext('artist') == 'My artist'
    assert result.findtext('custom') == 'Keep me'
    assert result.findtext('premiered') == '2000-01-02'
    assert [n.text for n in result.findall('director')] == ['A', 'B']
    again = merge_missing(result, new)
    assert len(again.findall('tag')) == 1


def test_plan_is_read_only_and_apply_backs_up_exact_original(tmp_path):
    folder = tmp_path / 'Artist'
    folder.mkdir()
    video = folder / 'Song.mp4'
    video.write_bytes(b'video')
    target = video.with_suffix('.nfo')
    original = b'<musicvideo><title>Song</title><artist>Artist</artist></musicvideo>'
    target.write_bytes(original)
    provider = Mock()
    provider.find_by_artist_title.return_value = {
        'id': 123, 'song_title': 'Song', 'year': 2001,
        'artists': [{'name': 'Artist'}], 'sources': [{'source': 'youtube', 'source_data': 'CD-E-LDc384'}]}
    planned, payload, report = plan_nfo(video, provider)
    assert target.read_bytes() == original
    assert list(folder.glob('*.bak-*')) == []
    assert 'not verified' in report['match_basis']
    root = ET.fromstring(payload)
    assert root.find("uniqueid[@type='youtube']") is None
    assert any('youtube.com/watch?v=' in (n.text or '') for n in root.findall('metube/youtube_reference'))
    backup = apply_nfo(planned, payload, tmp_path)
    assert backup.read_bytes() == original
    assert ET.parse(target).findtext('year') == '2001'


def test_apply_rejects_path_outside_library(tmp_path):
    library = tmp_path / 'library'
    library.mkdir()
    with pytest.raises(ValueError):
        apply_nfo(tmp_path / 'outside.nfo', b'<movie/>', library)


def test_title_artist_search_does_not_take_first_unrelated_result():
    provider = IMVDbProvider('test-only', search_enabled=True)
    provider._get = Mock(side_effect=[{'results': [{'id': 123}]},
                                     {'song_title': 'Other song', 'artists': [{'name': 'Artist'}]}])
    assert provider.find_by_artist_title('Artist', 'Song') is None



def test_provenance_container_survives_repeated_backfill():
    old = ET.fromstring('<musicvideo><title>Song</title></musicvideo>')
    proposed = ET.fromstring('<musicvideo><metube><source_url>original</source_url></metube></musicvideo>')
    merged = merge_missing(merge_missing(old, proposed), proposed)
    assert len(merged.findall('metube')) == 1
    assert merged.findtext('metube/source_url') == 'original'



def test_cast_merge_keeps_roles_and_is_idempotent():
    old = ET.fromstring('<musicvideo><actor><name>Existing</name><role>Custom</role></actor></musicvideo>')
    proposed = ET.fromstring('<musicvideo><actor><name>Existing</name><role>Other</role></actor><actor><name>New</name><role>Dancer</role></actor></musicvideo>')
    merged = merge_missing(merge_missing(old, proposed), proposed)
    assert len(merged.findall('actor')) == 2
    assert [n.findtext('role') for n in merged.findall('actor')] == ['Custom', 'Dancer']


from nfo_backfill import plan_current_nfo, apply_reviewed_entry, file_digest


def current_fixture(tmp_path):
    folder = tmp_path / 'Artist'
    folder.mkdir()
    media = folder / 'Song.mp4'
    media.write_bytes(b'video')
    media.with_suffix('.nfo').write_text('<musicvideo><title>Song</title><artist>Artist</artist>'
        '<plot>Old promotional text</plot><uniqueid type="youtube">CD-E-LDc384</uniqueid></musicvideo>')
    provider = Mock()
    provider.lookup.return_value = {'id': 123, 'song_title': 'Song', 'year': 2001,
        'artists': [{'name': 'Artist'}], 'sources': [{'source': 'youtube', 'source_data': 'CD-E-LDc384'}]}
    return media, provider


def test_refresh_uses_current_schema_and_original_source_without_writing(tmp_path):
    media, provider = current_fixture(tmp_path)
    target = media.with_suffix('.nfo')
    before = target.read_bytes()
    media.with_name('Song-poster.jpg').write_bytes(b'existing artwork')
    genres = Mock(); genres.lookup.return_value = {'genres': ['Pop']}
    description = Mock(); description.lookup.return_value = {'text': 'Song history',
        'url': 'https://es.wikipedia.org/wiki/Song', 'title': 'Song',
        'license_url': 'https://creativecommons.org/licenses/by-sa/4.0/'}
    _, payload, report = plan_current_nfo(media, provider, genres=genres, description=description, library_root=tmp_path)
    root = ET.fromstring(payload)
    assert report['match_basis'] == 'exact_youtube_source'
    assert report['status'] == 'proposed'
    assert root.findtext('tag') == 'Artista: Artist'
    assert root.findtext('genre') == 'Pop'
    assert 'Song history' in root.findtext('plot')
    assert 'Old promotional' not in root.findtext('plot')
    assert root.findtext('thumb') == 'Song-poster.jpg'
    assert root.findtext("uniqueid[@type='youtube']") == 'CD-E-LDc384'
    assert target.read_bytes() == before
    assert list(target.parent.glob('*.bak-*')) == []
    provider.find_by_artist_title.assert_not_called()


def test_unverified_catalog_reference_never_becomes_original_source(tmp_path):
    media, provider = current_fixture(tmp_path)
    media.with_suffix('.nfo').unlink()
    provider.find_by_artist_title.return_value = provider.lookup.return_value
    _, payload, report = plan_current_nfo(media, provider, library_root=tmp_path)
    root = ET.fromstring(payload)
    assert root.find("uniqueid[@type='youtube']") is None
    assert root.find('metube/source_url') is None
    assert 'referencia de IMVDb' in root.findtext('plot')
    assert 'not_source_verified' in report['match_basis']
    provider.lookup.assert_not_called()


def test_known_source_without_imvdb_match_stays_generic(tmp_path):
    media, provider = current_fixture(tmp_path)
    provider.lookup.return_value = None
    _, payload, report = plan_current_nfo(media, provider, library_root=tmp_path)
    root = ET.fromstring(payload)
    assert root.find('year') is None
    assert 'CD-E-LDc384' in root.findtext('plot')
    assert report['match_basis'] == 'generic'
    provider.find_by_artist_title.assert_not_called()


def test_incomplete_provider_result_cannot_be_applied(tmp_path):
    media, provider = current_fixture(tmp_path)
    description = Mock(); description.lookup.side_effect = TimeoutError('private detail')
    _, _, report = plan_current_nfo(media, provider, description=description, library_root=tmp_path)
    report['relative_video'] = 'Artist/Song.mp4'
    assert report['status'] == 'provider_error'
    assert 'private detail' not in str(report)
    with pytest.raises(ValueError):
        apply_reviewed_entry(report, tmp_path)


def test_apply_reviewed_plan_backs_up_exact_bytes_and_rejects_stale_preview(tmp_path):
    media, provider = current_fixture(tmp_path)
    target = media.with_suffix('.nfo')
    original = target.read_bytes()
    _, payload, report = plan_current_nfo(media, provider, library_root=tmp_path)
    report['relative_video'] = 'Artist/Song.mp4'
    applied = apply_reviewed_entry(report, tmp_path)
    from pathlib import Path
    assert Path(applied['backup']).read_bytes() == original
    assert target.read_bytes() == payload
    assert applied['nfo_sha256_after'] == file_digest(target)
    with pytest.raises(ValueError):
        apply_reviewed_entry(report, tmp_path)


@pytest.mark.parametrize('relative', ['../outside.mp4', '/outside.mp4', 'C:/outside.mp4'])
def test_reviewed_plan_rejects_outside_paths(tmp_path, relative):
    with pytest.raises(ValueError):
        apply_reviewed_entry({'relative_video': relative}, tmp_path)


def test_conflicting_source_ids_are_rejected(tmp_path):
    media, provider = current_fixture(tmp_path)
    media.with_suffix('.info.json').write_text('{"extractor_key":"Youtube","id":"yRYFKcMa_Ek"}')
    with pytest.raises(ValueError, match='Conflicting'):
        plan_current_nfo(media, provider, library_root=tmp_path)
    provider.lookup.assert_not_called()


def test_missing_mode_preserves_manual_plot(tmp_path):
    media, provider = current_fixture(tmp_path)
    _, payload, _ = plan_current_nfo(media, provider, mode='missing', library_root=tmp_path)
    assert ET.fromstring(payload).findtext('plot') == 'Old promotional text'


def test_cli_preview_then_apply_uses_saved_payload_without_providers(tmp_path, monkeypatch):
    import importlib.util
    import json
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('regularize_nfo_cli', Path(__file__).resolve().parents[2] / 'tools' / 'regularize_nfo.py')
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    library = tmp_path / 'library'; library.mkdir()
    media, provider = current_fixture(library)
    before = media.with_suffix('.nfo').read_bytes()
    monkeypatch.setattr(cli, 'providers', lambda cache: (provider, None, None))
    preview = tmp_path / 'preview.json'
    assert cli.main([str(library), '--limit', '1', '--report', str(preview)]) == 0
    assert media.with_suffix('.nfo').read_bytes() == before
    plan = json.loads(preview.read_text(encoding='utf-8'))
    assert plan['results'][0]['relative_video'] == 'Artist/Song.mp4'
    monkeypatch.setattr(cli, 'providers', Mock(side_effect=AssertionError('must stay offline')))
    report = tmp_path / 'applied.json'
    assert cli.main([str(library), '--apply-plan', str(preview), '--report', str(report)]) == 0
    assert json.loads(report.read_text(encoding='utf-8'))['summary'] == {'applied': 1}


def test_cli_rejects_report_inside_library_before_any_lookup(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('regularize_nfo_cli', Path(__file__).resolve().parents[2] / 'tools' / 'regularize_nfo.py')
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    monkeypatch.setattr(cli, 'providers', Mock(side_effect=AssertionError('no network')))
    with pytest.raises(SystemExit):
        cli.main([str(tmp_path), '--report', str(tmp_path / 'bad.json')])
    cli.providers.assert_not_called()


def test_changed_nfo_is_not_overwritten_by_reviewed_plan(tmp_path):
    media, provider = current_fixture(tmp_path)
    _, _, report = plan_current_nfo(media, provider, library_root=tmp_path)
    report['relative_video'] = 'Artist/Song.mp4'
    target = media.with_suffix('.nfo')
    target.write_text('<musicvideo><title>Edited after preview</title></musicvideo>')
    edited = target.read_bytes()
    with pytest.raises(ValueError, match='changed'):
        apply_reviewed_entry(report, tmp_path)
    assert target.read_bytes() == edited
    assert list(target.parent.glob('*.bak-*')) == []


def test_apply_preserves_posix_permissions(tmp_path):
    import os
    import stat
    if os.name == 'nt':
        pytest.skip('POSIX mode checked in Linux CI')
    media, provider = current_fixture(tmp_path)
    target = media.with_suffix('.nfo'); target.chmod(0o640)
    _, _, report = plan_current_nfo(media, provider, library_root=tmp_path)
    report['relative_video'] = 'Artist/Song.mp4'
    apply_reviewed_entry(report, tmp_path)
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_apply_retains_inherited_acl_if_chmod_not_supported(tmp_path, monkeypatch):
    import errno
    media, provider = current_fixture(tmp_path)
    _, payload, report = plan_current_nfo(media, provider, library_root=tmp_path)
    report['relative_video'] = 'Artist/Song.mp4'
    monkeypatch.setattr('nfo_backfill.os.chmod', Mock(side_effect=PermissionError(errno.EPERM, 'ACL')))
    assert apply_reviewed_entry(report, tmp_path)['status'] == 'applied'
    assert media.with_suffix('.nfo').read_bytes() == payload


def test_missing_mode_normalizes_legacy_root_for_reviewed_apply(tmp_path):
    media, provider = current_fixture(tmp_path)
    target = media.with_suffix('.nfo')
    target.write_text('<movie><title>Song</title><artist>Artist</artist><custom>Keep</custom></movie>')
    provider.find_by_artist_title.return_value = provider.lookup.return_value
    _, payload, report = plan_current_nfo(media, provider, mode='missing', library_root=tmp_path)
    report['relative_video'] = 'Artist/Song.mp4'
    assert ET.fromstring(payload).tag == 'musicvideo'
    assert ET.fromstring(payload).findtext('custom') == 'Keep'
    assert apply_reviewed_entry(report, tmp_path)['status'] == 'applied'


def test_cli_refuses_unfinished_preview(tmp_path):
    import importlib.util
    import json
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('regularize_nfo_cli', Path(__file__).resolve().parents[2] / 'tools' / 'regularize_nfo.py')
    cli = importlib.util.module_from_spec(spec); spec.loader.exec_module(cli)
    library = tmp_path / 'library'; library.mkdir()
    preview = tmp_path / 'unfinished.json'
    preview.write_text(json.dumps({'schema': 1, 'operation': 'preview', 'complete': False, 'results': []}))
    with pytest.raises(SystemExit):
        cli.main([str(library), '--apply-plan', str(preview), '--report', str(tmp_path / 'applied.json')])
    assert not (tmp_path / 'applied.json').exists()
