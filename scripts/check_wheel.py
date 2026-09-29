"""Fail if the built wheel carries anything but the package and its metadata.

Verify the artefact, not the source tree: a stray test directory, a virtualenv or a cached token
can be packaged without anything in the repository looking wrong.

Checked into the repository rather than inlined in the workflow, for the same reason as the
version check: an indented heredoc in a `run: |` block is a YAML problem, not a shell one.
"""

from __future__ import annotations

import glob
import sys
import zipfile

ALLOWED_PREFIXES = ("uvc_ptz_mcp/", "uvc_ptz_camera_mcp-")


def main() -> int:
    """List the wheel and complain about anything unexpected inside it."""
    wheels = sorted(glob.glob("dist/*.whl"))
    if not wheels:
        print("no wheel found in dist/", file=sys.stderr)
        return 1
    problems = 0
    for wheel in wheels:
        names = zipfile.ZipFile(wheel).namelist()
        offenders = [name for name in names if not name.startswith(ALLOWED_PREFIXES)]
        if offenders:
            print(f"{wheel}: unexpected entries: {offenders}", file=sys.stderr)
            problems += 1
        else:
            print(f"{wheel}: {len(names)} entries, package and dist-info only")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
