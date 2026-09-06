# Immich custom build

A small build recipe for official Immich releases with Cloudflare-safe chunked
uploads in the Android client and server. There is no vendored Immich checkout
or fork history here: the customization is `patches/cloudflare-uploads.patch`.

The patch reproduces the six application files changed by
[`sarpedondev/immich@67aefa791`](https://github.com/sarpedondev/immich/commit/67aefa7910ef7790a758a3b03e090203a1051824),
based on official `v3.2.0-rc.1`. Existing server and Android behavior is preserved.

## Automatic builds

GitHub Actions runs daily at 05:23 UTC, on pushes to `main`, and through **Run
workflow**. It selects the newest stable upstream release, without going below
`minimum_version` in `build.json` or the last successfully built version.
The initial RC therefore stays in use until a newer stable release exists.

Set `include_prereleases` in `build.json` to `true`, or set repository variable
`RELEASE_CHANNEL=prerelease`, to follow RCs. Manual runs offer `configured`,
`stable`, and `prerelease` channels.

For each candidate, the workflow:

1. Resolves the official release to an exact upstream commit.
2. Fetches that commit and the patch base into disposable source checkouts.
3. Applies the patch with `git apply --3way --index`. Conflicts fail the build.
4. Runs server formatting, lint, type checks, and unit tests; generates mobile
   code, runs Dart analysis and unit tests, and builds the Android APK.
5. Builds and pushes the `linux/amd64` server image only after both jobs pass.
6. Commits `build-state.json` with the successful version, source SHA, recipe
   commit, image digest, and build identity.

The daily job skips already successful inputs. Failed versions retry next day.
Pushes and manual runs rebuild. The generated state file is excluded from the
build identity, so the bot's bookkeeping does not create an endless rebuild loop.

## Outputs and GitHub setup

The image name remains `ghcr.io/sarpedondev/immich-server`, configured in
`build.json`. Tags:

- `v<upstream-version>`: release alias usable by Renovate.
- `custom`: most recently successful build.
- `build-<sha256>`: identifies the upstream commit and tracked build-recipe files.

Use an image digest to pin the exact binary image. Release aliases move when a
patch changes, and rebuilding identical source can still use updated external
build dependencies.

This repository needs **Write** access under **Manage Actions access** in the
[existing package settings](https://github.com/users/sarpedondev/packages/container/immich-server/settings).
Add `sarpedondev/immich-custom-build` there. Workflows then publish using their
own `GITHUB_TOKEN`; no personal token is needed. [GitHub package access documentation](https://docs.github.com/en/packages/managing-github-packages-using-github-actions-workflows/publishing-and-installing-a-package-with-github-actions).

Keep the old fork's `docker-fork.yml` workflow disabled so the two repositories
do not publish competing image aliases. Cluster and Renovate configuration are
managed separately.

The Android APK is an Actions artifact retained for 90 days, with version and
build identity in its name. It preserves the existing `cloudflare` application
ID suffix and debug-signing fallback. Installing APK updates over an existing
installation requires a consistent signing key; persistent release signing is
not configured by this repository. APKs are not installed on devices automatically.

## Work locally

Python 3 and Git are enough to fetch and patch source:

```sh
python3 scripts/build.py source /tmp/immich-patched
# Or prepare a particular official release's full commit SHA:
python3 scripts/build.py source /tmp/immich-next --commit <40-character-sha>
```

The destination must not exist. Install upstream's Mise tooling to run its
checks or build the resulting checkout. `python3 scripts/build.py select` uses
an authenticated GitHub CLI to preview the next selected version.

To update the customization, edit a prepared source checkout, then export its
complete diff relative to its clean upstream HEAD:

```sh
git -C /tmp/immich-patched diff --binary --full-index HEAD -- mobile server > patches/cloudflare-uploads.patch
```

If you rebase the patch on a newer release, set `patch_base` in `build.json` to
that checkout's upstream commit and `minimum_version` to its release version too. Commit and push the patch and configuration.

## Tests

```sh
python3 -m unittest discover -s tests -v
actionlint .github/workflows/build.yml
git diff --check
```

Tests cover version selection, release channels, build identity, retry/skip
behavior, and patch application to real temporary Git repositories, including
an upstream conflict.

## License

AGPL-3.0, matching upstream Immich. See `LICENSE`. The complete image source is
reproducible from the recorded upstream commit and this repository's patch.
