"""Ask whether this version is on PyPI, and exit accordingly.

Two callers, one question:

* the release guard (default) -- exit 0 when the version is already published, so a step that would
  duplicate it skips itself. Re-pushing a tag must not turn into a red run.
* the tag check (`--require`) -- exit 0 only when it *is* published, and when it is not, say what to
  run. CI deliberately does not upload (the token stays in the maintainer's credential store), so
  this is where tagging before uploading gets caught by name, instead of failing later as an
  ownership problem in the registry.

`--require` polls before giving up. Uploading from your own machine and tagging seconds later is the
normal order, and an index can lag behind an upload that landed; a release that fails only because
it was early is a false alarm, and false alarms are how a check stops being believed.

It checks the simple index rather than the project JSON, because the JSON endpoint can serve a
cached 404 for a package that is live -- and "not published" is the answer that causes a duplicate
upload.
"""

from __future__ import annotations

import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOT_FOUND = 404
REQUIRED_ATTEMPTS = 12
REQUIRED_DELAY_SECONDS = 10


def project_name() -> tuple[str, str]:
    """Read the distribution name and version from package metadata."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    name = re.search(r'^name = "([^"]+)"', text, re.M)
    version = re.search(r'^version = "([^"]+)"', text, re.M)
    if not name or not version:
        raise SystemExit("could not read name/version from pyproject.toml")
    return name.group(1), version.group(1)


def is_published(name: str, version: str) -> bool:
    """Ask the simple index whether a file for this version exists."""
    normalised = name.replace("-", "_")
    url = f"https://pypi.org/simple/{name}/"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        if error.code == NOT_FOUND:
            return False
        raise
    marker = f"{normalised}-{version}-"
    return marker in body


def wait_for(name: str, version: str) -> bool:
    """Poll until the version shows up, giving up after a bounded wait."""
    for attempt in range(1, REQUIRED_ATTEMPTS + 1):
        try:
            published = is_published(name, version)
        except Exception as error:  # noqa: BLE001 - the network is not this script's job to fix
            print(f"could not check PyPI ({type(error).__name__}: {error})")
            published = False
        if published:
            print(f"found {name} {version} on attempt {attempt}")
            return True
        if attempt < REQUIRED_ATTEMPTS:
            print(f"not there yet (attempt {attempt}/{REQUIRED_ATTEMPTS}); waiting")
            time.sleep(REQUIRED_DELAY_SECONDS)
    return False


def main(argv: list[str]) -> int:
    """Print the verdict and exit accordingly."""
    required = "--require" in argv
    name, version = project_name()

    if required:
        if wait_for(name, version):
            print(f"{name} {version} is on PyPI: safe to publish the registry entry")
            return 0
        print(
            f"::error::{name} {version} is not on PyPI. Upload the artifacts this run built "
            f"(download the 'dist' artifact, then `uv publish dist/*` with the token from your "
            f"credential store) and re-run this workflow. The registry entry cannot be published "
            f"before the package exists: it proves ownership by finding the mcp-name marker in the "
            f"package's published description."
        )
        return 1

    try:
        published = is_published(name, version)
    except Exception as error:  # noqa: BLE001 - the network is not this script's job to fix
        print(f"could not check PyPI ({type(error).__name__}: {error}); treating as unpublished")
        return 1
    if published:
        print(f"{name} {version} is already on PyPI: skipping the upload")
        return 0
    print(f"{name} {version} is not on PyPI: an upload is needed")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
