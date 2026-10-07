"""Exit 0 if this version is already in the MCP registry, 1 if it is not.

Used by the release workflow to decide whether to publish. Without it, re-pushing a tag that was
already released produces a red run for a release that is correctly published: the registry rejects
a duplicate with `400 invalid version: cannot publish duplicate version`. A tag can be re-pushed
for innocent reasons -- restoring a repository, moving a tag to a fixed manifest -- and neither
should look like a failed release.

Reads the name and version from `server.json`, which is the same file the publish uses.
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
REGISTRY = "https://registry.modelcontextprotocol.io"
NOT_FOUND = 404


def published_version() -> tuple[str, str]:
    """Read the server name and version from the registry manifest."""
    manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    return manifest["name"], manifest["version"]


def is_published(name: str, version: str) -> bool:
    """Ask the registry whether this exact version exists.

    The name contains a slash, so it has to be percent-encoded: a path with a literal slash
    addresses a different resource and answers 404 whether or not the entry exists.
    """
    encoded = urllib.parse.quote(name, safe="")
    url = f"{REGISTRY}/v0/servers/{encoded}/versions/{version}"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == NOT_FOUND:
            return False
        raise
    record = payload.get("server", payload)
    return str(record.get("version")) == version


def main() -> int:
    """Print the verdict and exit accordingly."""
    name, version = published_version()
    try:
        already = is_published(name, version)
    except Exception as error:  # noqa: BLE001 - the network is not this script's job to fix
        print(f"could not check the registry ({type(error).__name__}: {error}); assuming not")
        return 1
    if already:
        print(f"{name} {version} is already in the registry: skipping the publish")
        return 0
    print(f"{name} {version} is not in the registry: a publish is needed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
