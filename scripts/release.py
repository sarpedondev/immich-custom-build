#!/usr/bin/env python3
"""Publish one signed universal APK per successful workflow run."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from build import version_key


def release_identity(upstream, run_number):
    stable = version_key(upstream)[3]
    number = int(run_number)
    if not 0 < number < 2_000_000_000:
        raise ValueError("Workflow run number exceeds Android version code range")
    # Matches Flutter's build-name plus the existing PR_NUMBER version suffix.
    return f"{upstream}-custom.{number}-prcloudflare", not stable


def main():
    env = os.environ
    repo = env["GITHUB_REPOSITORY"]
    upstream = env["UPSTREAM_VERSION"]
    tag, prerelease = release_identity(upstream, env["GITHUB_RUN_NUMBER"])

    def gh(*args):
        return subprocess.check_output(["gh", *args], text=True).strip()

    # A rerun may find a completed, potentially immutable release. Leave it intact.
    releases = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repo}/releases?per_page=100"))
    existing = next((r for page in releases for r in page if r["tag_name"] == tag), None)
    if existing and not existing["draft"]:
        if not any(a["name"] == "immich-cloudflare.apk" for a in existing["assets"]):
            raise RuntimeError("Published release is missing its APK; inspect before retrying")
        print(existing["html_url"])
        return

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        gh("run", "download", env["GITHUB_RUN_ID"], "--repo", repo,
           "--name", f"immich-android-{upstream}-{env['BUILD_KEY']}", "--dir", directory)
        apk = root / "immich-cloudflare.apk"
        (root / "app-release.apk").rename(apk)
        checksum = root / "SHA256SUMS"
        checksum.write_text(f"{hashlib.sha256(apk.read_bytes()).hexdigest()}  {apk.name}\n")
        notes = root / "notes.md"
        notes.write_text(
            f"Patched Immich {upstream} with Cloudflare-safe uploads.\n\n"
            "Install `immich-cloudflare.apk`, or add this repository in Obtainium. "
            "Enable prereleases while using an RC. Future stable upgrades are selected automatically.\n\n"
            f"- Upstream: `immich-app/immich@{env['UPSTREAM_SHA']}`\n"
            f"- Recipe: `{env['RECIPE_COMMIT']}`\n"
            f"- Server: `{env['IMAGE']}@{env['IMAGE_DIGEST']}`\n"
            f"- Android version code: `{100000000 + int(env['GITHUB_RUN_NUMBER'])}`\n\n"
            "APKs use a persistent signing key. An older debug-signed installation may "
            "need a one-time reinstall; this clears local app settings.\n"
        )
        if not existing:
            gh("release", "create", tag, "--repo", repo, "--target", env["RECIPE_COMMIT"],
               "--title", tag, "--notes-file", str(notes), "--draft",
               f"--prerelease={'true' if prerelease else 'false'}")
        gh("release", "upload", tag, str(apk), str(checksum), "--repo", repo, "--clobber")
        gh("release", "edit", tag, "--repo", repo, "--draft=false",
           f"--latest={'false' if prerelease else 'true'}")
        print(f"https://github.com/{repo}/releases/tag/{tag}")


if __name__ == "__main__":
    main()
