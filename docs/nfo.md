# NFO metadata and artist folders

This fork adds an optional NFO selector to MeTube's video form. Select Kodi,
Jellyfin or Emby and optionally save local artwork. The current profiles share
a common XML subset; interoperability still needs checking in your own player.
The browser remembers the NFO profile and artwork selection. They survive queue
retries and are carried into new subscription downloads.

## Identification and output

With `ARTIST_FOLDERS=true` (the default), video downloads go into an artist
directory. Metadata is resolved before yt-dlp expands the filename. The primary
artist from a verified IMVDb recording takes precedence over the original
extractor's artist. Collaborations use the primary artist, without duplicating
the file into every collaborator's directory. If no artist is available, the
folder is `Sin artista`. This applies to videos, not separate audio downloads.

Existing artist folders are reused when their normalized names match uniquely,
including case or punctuation differences such as `AC-DC` and `AC_DC`.
Otherwise yt-dlp creates the new folder while writing the download. Artist names
go through MeTube's path sanitization and output confinement guards. Existing
videos are not moved or renamed.

When IMVDb identifies the recording, its song title is used as the filename's
title. Otherwise a structured extractor track title is used if present. This
avoids repeating an artist prefix that can make Jellyfin group different songs
as versions in a shared artist folder. The original source title is retained
as an NFO reference. Unstructured titles are not split or guessed.

Playlist/channel grouping templates are bypassed for videos when artist folders
are enabled, so batch downloads reuse the same artist directory in the selected
destination. An explicitly selected destination folder remains the parent.

Example:

```text
downloads/
  Metallica/
    Enter Sandman.mp4
    Enter Sandman.nfo
    Enter Sandman-poster.webp
```

The artwork comes from yt-dlp's downloaded thumbnail and keeps its actual file
extension. It is not cropped into a vertical poster. The original thumbnail may
also remain next to the video. Existing NFOs and artwork are preserved. NFOs are
published as complete files rather than partial writes. File read/write mode
follows the video file's mode.

## IMVDb

Register an application at <https://imvdb.com/developers/apps/new> and set
`IMVDB_APP_KEY` on the running container. Never commit the key or pass it as an
image build argument. The browser does not receive it. CI and image publication
do not need it.

IMVDb announced a temporary shutdown of V1 API searches. Therefore this fork
defaults to `IMVDB_SEARCH_ENABLED=false` and `IMVDB_WEB_SEARCH_ENABLED=true`:

1. Query IMVDb's public search form using the artist/title or video title.
2. Inspect at most three candidate video pages and extract their visible IDs.
3. Fetch metadata from `https://imvdb.com/api/v1/video/{id}` using the app key.
4. Accept metadata only when the API lists the exact YouTube ID being downloaded.

Public-page requests never contain the app key, do not follow redirects and
only target the fixed IMVDb host and supported paths. Responses are size-limited.
Lookups use a six-second request timeout and a fifteen-second budget for starting
new requests. Successful identifications are cached for 24 hours under
`STATE_DIR/imvdb-cache`. HTML discovery can require maintenance if the site changes.

The optional field accepts either a numeric public video ID or a full HTTPS
IMVDb video-page URL. It bypasses discovery but still requires the exact YouTube
source match. It is not remembered for the next submission and is deliberately
not carried into subscriptions, where one video's ID must not apply to a feed.

IMVDb also announced retirement of V1 during 2027. The provider lives in
`app/nfo_metadata.py` so it can be adapted when V2's contract is available.

## Generic fallback and references

Network failures, a missing key, no matching recording or ambiguous candidates
keep the download usable. A selected NFO profile produces a generic NFO using
available original title, description, duration, ID, tags and source URL.

A verified recording uses `musicvideo` XML and can add artist, director, year and
a full premiere date. Generic fallback keeps the `musicvideo` root so it fits
the same music-video library. Structured original artist fields are retained
when available. Missing fields are omitted.
YouTube's upload date is labelled separately; it never becomes an invented
original premiere. An IMVDb year alone never becomes January 1st of that year.

The original YouTube ID and URL remain in the NFO. IMVDb's ID, page and YouTube
links are also recorded, with catalog links explicitly labelled as references.
For new downloads the exact-source match is required before applying catalog
credits. NFO/artwork errors warn and preserve the downloaded video.

## Existing library: review first

The separate script is read-only by default:

```bash
python tools/regularize_nfo.py /path/to/library --limit 20 --report preview.json
```

It reads `IMVDB_APP_KEY` from the environment or a private project `.env`. It uses
existing NFO artist/title or the folder/filename and requires an unambiguous
normalized artist/title match. Without an original source ID, catalog YouTube
links are references, not proof of the downloaded file's source.

Review the report before explicitly adding `--apply`. Existing NFOs get timestamped
backups before replacement, populated fields and unknown elements are preserved,
and invalid XML/unmatched files are skipped. Keep reports outside the library.
The script neither downloads media nor replaces artwork. Stop other NFO writers
before applying and retain backups to restore individual files.

## Maintenance and checks

The fork keeps the upstream history. Integrate upstream changes, preserving the
fork's GHCR workflow, then run frontend lint/build/tests and backend tests as in
`AGENTS.md`. Metadata lookup and file generation have focused offline tests.

The separate `.github/workflows/nfo.yml` builds Linux amd64 after tests, runs a module smoke test
inside the built image, then publishes `latest` and `sha-<full-commit>` from
`master`. Keep the commit tag for reproducible deployment or rollback. ARM64
publication is not configured for this fork's initial TrueNAS deployment.

## Troubleshooting

- Generic NFO: check the runtime key, public web search, recording's YouTube
  sources and logs. A matching title alone is intentionally insufficient.
- Missing premiere: the catalog may only provide a year. Nothing is fabricated.
- Missing poster: check whether yt-dlp saved a local thumbnail and directory
  permissions. The image may be WebP or PNG rather than JPEG.
- Existing NFO unchanged: this preserves user metadata. Back it up before
  removing it to regenerate a test sidecar.
- Wrong artist folder: review the identified primary artist or extractor's
  artist field. Disable `ARTIST_FOLDERS` to use the original output layout.
- Separate chapters: artist directories apply to their output template too,
  but per-chapter NFOs are not implemented.

Sources: [IMVDb API](https://imvdb.com/developers/api),
[Kodi music-video NFOs](https://kodi.wiki/view/NFO_files/Music_videos),
[upstream MeTube](https://github.com/alexta69/metube).

## JPEG artwork and ACL storage

With NFO artwork enabled, yt-dlp converts the downloaded thumbnail to real JPEG
using FFmpeg before downloading the video. The NFO references a local
`<video-stem>-poster.jpg`. The generated thumbnail uses the final poster name, avoiding a duplicate;
existing user artwork and explicitly configured thumbnail templates are preserved. This applies to new downloads only.

NFSv4 ACL storage can permit file creation while rejecting chmod. For EPERM,
ENOTSUP or ENOSYS, NFO publication retains the inherited ACL/restricted temporary
file permissions instead of discarding the complete XML. Other storage errors
still stop NFO publication and leave the video intact. Diagnostic warnings report
exception types and numeric codes, never exception messages or request headers.

## Clean player metadata

New NFO files omit the full YouTube description, promotional links, lyrics and
YouTube tags. They keep title, structured artists, duration, available verified
premiere/year and director, local artwork and provider IDs. Original URLs,
original display title and IMVDb catalog references live inside a custom
`<metube>` provenance element instead of player-visible `<tag>` fields. This
extension preserves traceability; media players may ignore it. The YouTube
`uniqueid` is retained. Generic fallback does not invent missing credits/dates.

With the default output template the files are video, NFO and
`<video-stem>-poster.jpg`. An explicitly configured thumbnail template is
respected and may intentionally result in an additional thumbnail. Existing
files are neither deleted nor rewritten; test a new download after updating.
