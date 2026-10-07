"""Layer 1e: how configuration is resolved between flags and the environment.

Hosts configure a local server through the environment as often as through flags, and they write
an *empty string* for an option the user left blank. Treating that empty string as a value would
make `UVC_PTZ_BACKEND=""` an invalid choice and stop the server from starting — on a host whose
configuration was simply incomplete. That case is the reason this file exists.
"""

from __future__ import annotations

import pytest

from uvc_ptz_mcp import __main__ as cli


def test_no_flag_and_no_environment_means_auto(monkeypatch):
    monkeypatch.delenv(cli.BACKEND_ENV, raising=False)
    assert cli.resolve_backend(None) == "auto"


def test_environment_supplies_the_backend(monkeypatch):
    monkeypatch.setenv(cli.BACKEND_ENV, "simulator")
    assert cli.resolve_backend(None) == "simulator"


def test_a_flag_beats_the_environment(monkeypatch):
    monkeypatch.setenv(cli.BACKEND_ENV, "simulator")
    assert cli.resolve_backend("dshow") == "dshow"


def test_an_empty_environment_value_means_unset(monkeypatch):
    """The failure this guards: a host maps an unanswered prompt into the environment."""
    monkeypatch.setenv(cli.BACKEND_ENV, "")
    assert cli.resolve_backend(None) == "auto"
    monkeypatch.setenv(cli.DEVICE_ENV, "   ")
    assert cli.resolve_device(None) is None


def test_an_invalid_environment_value_falls_back_and_says_so(monkeypatch, capsys):
    monkeypatch.setenv(cli.BACKEND_ENV, "usb")
    assert cli.resolve_backend(None) == "auto"
    captured = capsys.readouterr()
    assert "UVC_PTZ_BACKEND" in captured.err, "the warning belongs on stderr, never stdout"
    assert "usb" in captured.err


def test_device_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv(cli.DEVICE_ENV, "osmo")
    assert cli.resolve_device(None) == "osmo"
    assert cli.resolve_device("other") == "other"


def test_the_printed_config_reflects_the_environment(monkeypatch, capsys):
    """`--print-config` must describe what will actually run, not just what was typed."""
    monkeypatch.setenv(cli.BACKEND_ENV, "simulator")
    monkeypatch.setenv(cli.DEVICE_ENV, "osmo")
    assert cli.main(["--print-config"]) == 0
    printed = capsys.readouterr().out
    assert "--backend" in printed and "simulator" in printed
    assert "--device" in printed and "osmo" in printed


def test_version_needs_no_environment(monkeypatch, capsys):
    monkeypatch.setenv(cli.BACKEND_ENV, "")
    assert cli.main(["--version"]) == 0
    assert "uvc-ptz-camera-mcp" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["auto", "dshow", "simulator"])
def test_the_three_backends_are_all_expressible(monkeypatch, value):
    monkeypatch.setenv(cli.BACKEND_ENV, value)
    assert cli.resolve_backend(None) == value
