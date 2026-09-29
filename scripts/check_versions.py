"""Fail if the four copies of the version disagree.

Package metadata, the constant inside the code, the registry manifest and the bundle manifest
each carry one. The constant is the copy that drifts silently -- nothing reads it until a host
asks -- so a build reporting the previous release while every manifest says the new one is a lie
about which build is running, and it survives every check that only reads metadata.

Checked into the repository rather than inlined in the workflow: a `run: |` block is YAML before
it is shell, so an indented heredoc arrives indented and dies with an IndentationError.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def read() -> dict[str, str]:
    """Collect every place a version is written down."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    init = (ROOT / "uvc_ptz_mcp" / "__init__.py").read_text(encoding="utf-8")
    package_match = re.search(r'^version = "([^"]+)"', pyproject, re.M)
    constant_match = re.search(r'__version__ = "([^"]+)"', init)
    if not package_match or not constant_match:
        print("could not find a version in pyproject.toml or __init__.py", file=sys.stderr)
        raise SystemExit(1)
    return {
        "pyproject.toml": package_match.group(1),
        "uvc_ptz_mcp/__init__.py": constant_match.group(1),
        "server.json": json.loads((ROOT / "server.json").read_text(encoding="utf-8"))["version"],
        "mcpb/manifest.json": json.loads(
            (ROOT / "mcpb" / "manifest.json").read_text(encoding="utf-8")
        )["version"],
    }


def main() -> int:
    """Report agreement, or fail naming every copy."""
    found = read()
    if len(set(found.values())) != 1:
        print("version mismatch:", file=sys.stderr)
        for where, version in found.items():
            print(f"  {where}: {version}", file=sys.stderr)
        return 1
    print(f"all copies agree: {next(iter(found.values()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
