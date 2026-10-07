"""Exit 0 if this version is already on PyPI, 1 if it is not.

Used by the release workflow to decide whether to upload. A release is cut in two ways -- the
token is used locally when one is available, and the workflow can publish when it is not -- and
without this check the second path fails on a version the first path already uploaded, turning a
successful release into a red run.

Checks the simple index rather than the project JSON: the JSON endpoint can serve a cached 404
for a package that is live, and "not published" is the answer that causes a duplicate upload.
"""

from __future__ import annotations

import pathlib
import re
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]


def project_name() -> str:
    """The distribution name, from packagine metadata."""
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
        if error.code == 404:
            return False
        raise
    marker = f"{normalised}-{version}-"
    return marker in body


def main() -> int:
    """Print the verdict and exit accordingly."""
    name, version = project_name()
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
    sys.exit(main())
