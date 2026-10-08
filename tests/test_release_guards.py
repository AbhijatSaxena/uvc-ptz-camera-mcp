"""The release path's guards, and the decision they encode.

The tag workflow deliberately does not upload to PyPI: uploading stays a local step so the API token
lives in the maintainer's credential store rather than in a repository secret. That makes two things
worth pinning -- the check that catches tagging before uploading, and the *absence* of a CI upload
step, which is exactly the kind of thing a later contributor adds back without knowing why it went.
"""

from __future__ import annotations

import importlib.util
import pathlib
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_script(name: str) -> Any:  # noqa: ANN401 - a module object has no better type here
    """Load a script from scripts/ by path, since that directory is not a package."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def on_pypi(monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401 - see load_script
    """Provide the release guard, with its polling shortened so a failure is instant."""
    module = load_script("on_pypi")
    monkeypatch.setattr(module, "REQUIRED_ATTEMPTS", 1)
    monkeypatch.setattr(module, "REQUIRED_DELAY_SECONDS", 0)
    return module


def test_the_guard_skips_a_version_that_is_already_published(on_pypi, monkeypatch):
    """A re-pushed tag must not become a red run over a version that is already up."""
    monkeypatch.setattr(on_pypi, "is_published", lambda name, version: True)
    assert on_pypi.main([]) == 0


def test_the_guard_publishes_a_version_that_is_missing(on_pypi, monkeypatch):
    monkeypatch.setattr(on_pypi, "is_published", lambda name, version: False)
    assert on_pypi.main([]) == 1


def test_the_tag_check_passes_once_the_package_is_live(on_pypi, monkeypatch):
    monkeypatch.setattr(on_pypi, "is_published", lambda name, version: True)
    assert on_pypi.main(["--require"]) == 0


def test_the_tag_check_fails_with_the_command_that_fixes_it(on_pypi, monkeypatch, capsys):
    """The whole point of verifying instead of uploading: say what to run, not just 'refused'."""
    monkeypatch.setattr(on_pypi, "is_published", lambda name, version: False)
    assert on_pypi.main(["--require"]) == 1
    printed = capsys.readouterr().out
    assert "::error::" in printed, "GitHub shows the annotation, not a wall of log"
    assert "uv publish dist/*" in printed, "the failure has to name the command to run"
    assert "registry entry cannot be published" in printed, "and why it is checked first"


def test_the_release_workflow_does_not_upload_to_pypi():
    """Uploading from CI needs a publisher configured on PyPI's side -- which is why it is local.

    This asserts an absence on purpose: the upload step worked in no configuration we ever had, and
    failing that way costs a red tag run that reads as an auth fault. Checking the whole file, not
    just the steps, so that reintroducing it is a deliberate act rather than a quiet one.
    """
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "gh-action-pypi-publish" not in text, "CI has no PyPI publisher, so it must not try"
    assert "uv publish" in text, "the workflow should say how the upload is done instead"
    assert "on_pypi.py --require" in body, "the tag must check the package is live first"
    assert "id-token: write" in body, "the registry still authenticates with GitHub OIDC"


def test_the_registry_entry_still_waits_for_the_package():
    """The registry proves ownership from the *published* description, so order is not optional."""
    text = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    assert "needs: verify-pypi" in text, "the registry entry waits for the package check"
    assert "mcp-publisher publish" in text
