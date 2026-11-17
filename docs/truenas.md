# TrueNAS deployment

Use a TrueNAS release with Docker-based Apps (24.10 or later), an Apps pool and
host directories writable by the container user. The exact host dataset paths
and UID/GID are installation-specific; the example intentionally uses placeholders.

## Published image

The workflow publishes `ghcr.io/tool12/metube-nfo:latest` after tests and a
successful Linux amd64 container build on `master`. It also publishes an
immutable `sha-<full-commit>` tag. A configured workflow does not mean an image
has already been published: check the Actions run and Packages before installing.

On the first publication, GitHub may create the package as private. To enable
anonymous pulls, its owner must make the container package Public in its package
settings. Otherwise configure registry authentication in TrueNAS. The IMVDb key
is unrelated to registry access.

## Install

1. Create separate config and test-download directories. Back up any existing
   Compose/configuration before changing an installed app.
2. Open Apps → Discover Apps → menu → Install via YAML.
3. Paste `compose.truenas.example.yaml`, replace `/mnt/POOL/...` with your paths
   and adjust `PUID`/`PGID` to the account with directory access.
4. Replace the runtime key placeholder privately in TrueNAS. Leave API search
   disabled and web search enabled while IMVDb's API search is suspended.
5. Save, wait for the container to run and open `http://<NAS-IP>:8082`.

The example uses `CHOWN_DIRS=false` so the entrypoint does not recursively change
ownership of your library. Directory permissions must already allow the selected
UID/GID to write. `/state` holds queue/config/cache separately from `/downloads`.

## Test

Download an authorized video with NFO enabled and artwork selected. Start in a
test directory, leaving the optional IMVDb field empty to exercise discovery.
Check the artist folder, video, XML sidecar and local image. Read the NFO's
original source and catalog references. Try a video with no matching recording
to exercise the generic fallback. Import into the actual player to check the
shared XML profile; player interoperability is not implied by a successful build.

## Update and rollback

Keep the previous commit tag, configuration and state backup. After testing a
new published commit tag, update the app's `image` field. Pin the former tag to
roll back. Stopping/removing the app need not delete its host-path data; keep
the configuration and test downloads until you have reviewed the result.

## Troubleshooting

- Pull denied / manifest unknown: verify publication, tag and package visibility.
- Permission denied: verify UID/GID, directory ACLs and parent traversal.
- Port occupied: change only the host side, for example `8083:8081/tcp`.
- Download fails before NFO: inspect yt-dlp logs separately from IMVDb lookup.
- Generic sidecar or missing artwork: see [the NFO guide](nfo.md).

This is a procedure, not evidence of an installation on a particular NAS.
Sources: [TrueNAS Custom Apps](https://apps.truenas.com/managing-apps/installing-custom-apps/)
and [GitHub Container Registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

### Named-group ACLs and container identity

If an explicit UID/GID write probe succeeds but the application fails, compare
`/proc/<pid>/status` supplementary groups with the dataset ACL. In the reported
TrueNAS deployment, including the ACL group in the supplementary group list
allowed writes while the same primary GID with an empty list failed. Configure
Docker to start directly as the desired identity and retain that group:

```yaml
    user: "${METUBE_UID}:${METUBE_GID}"
    group_add:
      - "${METUBE_GID}"
```

Set the placeholders to the account/group granted access by the dataset ACL;
they are installation-specific. Keep CHOWN_DIRS=false to preserve library
ownership. The non-root entrypoint does not run gosu, so Docker's supplementary
groups remain in effect. Internal nightly yt-dlp updates are disabled in this
mode; update the container image instead. Do not clear the state directory to
work around UI errors: it contains queue, history and metadata cache.
