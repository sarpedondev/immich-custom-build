#!/usr/bin/env python3
"""Select an Immich release, prepare patched source, and record successful builds."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
STATE = "build-state.json"


def run(*args, cwd=ROOT):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def version_key(tag):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?", tag)
    if not match:
        raise ValueError(f"Invalid release version: {tag}")
    major, minor, patch, pre = match.groups()
    identifiers = tuple((0, int(x)) if x.isdigit() else (1, x) for x in (pre or "").split("."))
    return (int(major), int(minor), int(patch), pre is None, identifiers)


def is_release_candidate(tag):
    return re.fullmatch(r"v?\d+\.\d+\.\d+-rc(?:[.-]?\d+)?(?:\+[0-9A-Za-z.-]+)?", tag, re.IGNORECASE) is not None


def select_release(releases, current):
    """Leave RCs for stable as soon as possible; never enter RCs from stable."""
    floor = version_key(current)
    stable = []
    candidates = []
    for release in releases:
        if release["draft"]:
            continue
        tag = release["tag_name"]
        try:
            key = version_key(tag)
        except ValueError:
            continue
        if key <= floor:
            continue
        if key[3] and not release["prerelease"]:
            stable.append((key, tag))
        elif is_release_candidate(current) and is_release_candidate(tag):
            candidates.append((key, tag))
    # A newer RC on another release line must not keep us on previews when
    # there is already a stable upgrade available from our current version.
    return max(stable or candidates)[1] if stable or candidates else current


def recipe_key(root, upstream_sha):
    """Exclude generated success state so recording a build doesn't cause another."""
    digest = hashlib.sha256(upstream_sha.encode())
    files = run("git", "ls-files", "-z", cwd=root).split("\0")
    for name in sorted(filter(None, files)):
        if name == STATE:
            continue
        content = (root / name).read_bytes()
        digest.update(name.encode() + b"\0" + str(len(content)).encode() + b"\0" + content)
    return digest.hexdigest()


def select(root, config):
    state_path = root / STATE
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    floor = max(config["minimum_version"], state.get("version", config["minimum_version"]), key=version_key)
    repository = config["upstream_repository"]
    pages = json.loads(run("gh", "api", "--paginate", "--slurp", f"repos/{repository}/releases?per_page=100"))
    version = select_release([release for page in pages for release in page], floor)
    sha = run("gh", "api", f"repos/{repository}/commits/{version}", "--jq", ".sha")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Upstream did not return a commit SHA")
    if state.get("version") == version and state.get("upstream_sha") != sha:
        raise ValueError(f"Previously built upstream tag {version} moved; inspect it before updating build-state.json")
    key = recipe_key(root, sha)
    automatic = os.environ.get("GITHUB_EVENT_NAME") in {"schedule", "repository_dispatch"}
    should_build = not automatic or state.get("build_key") != key
    return dict(current_version=floor, version=version, upstream_sha=sha, recipe_sha=run("git", "rev-parse", "HEAD", cwd=root), build_key=key, image=config["image"], build=str(should_build).lower())


def prepare_source(root, config, destination, sha, repository=None):
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Use an exact 40-character upstream commit SHA")
    if destination.exists():
        raise ValueError(f"{destination} already exists; choose a new source directory")
    destination.mkdir(parents=True)
    run("git", "init", "--quiet", str(destination))
    run("git", "remote", "add", "origin", repository or f"https://github.com/{config['upstream_repository']}.git", cwd=destination)
    # Fetch patch-base blobs too: git apply --3way needs them on newer releases.
    refs = sorted({sha, config["patch_base"]})
    run("git", "fetch", "--quiet", "--depth=1", "origin", *refs, cwd=destination)
    run("git", "checkout", "--quiet", "--detach", sha, cwd=destination)
    for patch in config["patches"]:
        run("git", "apply", "--3way", "--index", "--whitespace=error-all", str(root / patch), cwd=destination)
    print(f"Prepared {sha} with {len(config['patches'])} patch(es) in {destination}")


def record(root):
    state = {
        "version": os.environ["UPSTREAM_VERSION"],
        "upstream_sha": os.environ["UPSTREAM_SHA"],
        "build_key": os.environ["BUILD_KEY"],
        "recipe_commit": os.environ["RECIPE_COMMIT"],
        "image_digest": os.environ["IMAGE_DIGEST"],
        "run_url": os.environ["RUN_URL"],
    }
    (root / STATE).write_text(json.dumps(state, indent=2) + "\n")
    run("git", "config", "user.name", "github-actions[bot]", cwd=root)
    run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com", cwd=root)
    run("git", "add", STATE, cwd=root)
    if run("git", "diff", "--cached", "--name-only", cwd=root):
        run("git", "commit", "-m", f"chore: record successful Immich {state['version']} build", cwd=root)
        # Don't overwrite concurrent pushes; a rejected push is retried next run.
        run("git", "push", "origin", "HEAD:refs/heads/main", cwd=root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("select")
    source = commands.add_parser("source")
    source.add_argument("destination", type=Path)
    source.add_argument("--commit", help="Exact upstream SHA; defaults to the patch base")
    commands.add_parser("record")
    args = parser.parse_args()
    config = json.loads((ROOT / "build.json").read_text())
    if args.command == "source":
        prepare_source(ROOT, config, args.destination.resolve(), args.commit or config["patch_base"])
    elif args.command == "record":
        record(ROOT)
    else:
        result = select(ROOT, config)
        print(json.dumps(result, indent=2))
        if output := os.environ.get("GITHUB_OUTPUT"):
            with open(output, "a") as stream:
                stream.writelines(f"{key}={value}\n" for key, value in result.items())
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(summary, "a") as stream:
                stream.write(
                    f"Current: `{result['current_version']}`\n\n"
                    f"Selected: `{result['version']}` (`{result['upstream_sha']}`)\n\n"
                    f"Build needed: `{result['build']}`\n\nBuild identity: `{result['build_key']}`\n"
                )


if __name__ == "__main__":
    main()
