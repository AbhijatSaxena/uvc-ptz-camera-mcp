"""DirectShow (Windows) backend: the real device path, for UVC cameras with PTZ controls.

This is the code that was written and tested against the reference camera before it was
returned: device enumeration, the UVC Camera Terminal controls, absolute writes with manual
flags, and per-axis settle times measured on the hardware. It is kept because it works, and
because any UVC PTZ camera exposes the same control surface.

Two things measured on the reference device are encoded here rather than discovered again:

  * **The vendor Extension Unit is unreachable on Windows.** `IKsControl` is refused on the
    device filter, and although `IKsTopologyInfo` enumerates three nodes (capture, streaming,
    camera terminal), `CreateNodeInstance` fails on all of them and no vendor-specific node is
    exposed. Anything beyond the standard controls -- on the reference camera, its built-in
    subject tracking -- therefore cannot be driven from Windows through DirectShow at all.
    A Linux host can do it through `uvcvideo`'s `UVCIOC_CTRL_QUERY`; that is a separate backend.
  * **The reported value is a hint, not evidence.** `read` is documented and treated as such;
    verification is done from frames.

`comtypes` and `pygrabber` are optional extras. When they are absent, `open` raises
BackendUnavailableError with the install hint rather than an ImportError, so the server still starts
and can explain itself.
"""

from __future__ import annotations

import logging
from ctypes import POINTER, c_long
from typing import Any

import numpy as np

from .backend import BackendUnavailableError
from .camera import Axis, AxisSpec
from .frames import capture_dshow

_LOGGER = logging.getLogger(__name__)

# UVC Camera Terminal control ids (DirectShow CameraControlProperty).
CAMERA_PROPERTY_IDS: dict[Axis, int] = {
    Axis.PAN: 0,
    Axis.TILT: 1,
    Axis.ROLL: 2,
    Axis.ZOOM: 3,
}
PROPERTY_NAMES = {value: key for key, value in CAMERA_PROPERTY_IDS.items()}

# Control flags: the reference device advertises manual-only on every axis.
FLAG_MANUAL = 0x0002

# Measured on the reference device: a pan reads as complete in about a second, a 4x zoom in
# about 2.5s, and ~0.4s passes between the write and the first visible motion.
LATENCY_SECONDS = 0.4
SETTLE_SECONDS: dict[Axis, float] = {
    Axis.PAN: 1.0,
    Axis.TILT: 1.0,
    Axis.ROLL: 0.8,
    Axis.ZOOM: 2.5,
}

# Zoom arrives as ratio x100 (100 = 1x). Kept as the device's own units and labelled as such.
ZOOM_SCALE = 100


def _declare_interfaces() -> tuple[Any, Any, Any]:
    """Build the comtypes interface for IAMCameraControl, or explain what is missing."""
    try:
        from comtypes import COMMETHOD, GUID, HRESULT, IUnknown  # noqa: PLC0415
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise BackendUnavailableError(
            "the DirectShow backend needs comtypes and pygrabber: "
            "pip install 'uvc-ptz-camera-mcp[dshow]'"
        ) from error

    class IAMCameraControl(IUnknown):  # noqa: N806 - a locally declared COM interface, so that
        """Camera Terminal controls: pan/tilt/roll/zoom, declared here as comtypes lacks them."""

        _iid_ = GUID("{C6E13370-30AC-11d0-A18C-00A0C9118956}")
        _methods_ = [
            COMMETHOD(
                [],
                HRESULT,
                "GetRange",
                (["in"], c_long, "Property"),
                (["out"], POINTER(c_long), "pMin"),
                (["out"], POINTER(c_long), "pMax"),
                (["out"], POINTER(c_long), "pSteppingDelta"),
                (["out"], POINTER(c_long), "pDefault"),
                (["out"], POINTER(c_long), "pCapsFlags"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "Set",
                (["in"], c_long, "Property"),
                (["in"], c_long, "lValue"),
                (["in"], c_long, "Flags"),
            ),
            COMMETHOD(
                [],
                HRESULT,
                "Get",
                (["in"], c_long, "Property"),
                (["out"], POINTER(c_long), "lValue"),
                (["out"], POINTER(c_long), "Flags"),
            ),
        ]

    return IAMCameraControl, None, None


def enumerate_video_devices() -> tuple[list[str], list[str]]:
    """Every DirectShow video input device on this machine, and any that could not be read.

    Returns (usable names, notes about devices that failed to load). A device whose filter cannot
    be instantiated must not hide the others: on the reference machine a registered-but-unavailable
    virtual camera raised when its moniker was bound, which aborted a whole listing that should
    have returned two working cameras. `--list-devices` exists to answer "what do I pass to
    --device?", and an answer of "nothing works" when two cameras are present is worse than
    useless.
    """
    try:
        from pygrabber.dshow_graph import SystemDeviceEnum  # noqa: PLC0415
        from pygrabber.dshow_ids import DeviceCategories  # noqa: PLC0415
    except ImportError as error:  # pragma: no cover - depends on the environment
        raise BackendUnavailableError(
            "enumerating devices needs comtypes and pygrabber: "
            "pip install 'uvc-ptz-camera-mcp[dshow]'"
        ) from error

    category = (
        DeviceCategories.VideoInputDevice.value
        if hasattr(DeviceCategories.VideoInputDevice, "value")
        else str(DeviceCategories.VideoInputDevice)
    )
    enumerator = SystemDeviceEnum()
    names: list[str] = []
    notes: list[str] = []
    index = 0
    while True:
        try:
            _, name = enumerator.get_filter_by_index(category, index)
        except ValueError:
            break  # past the end of the device list: the normal way this loop finishes
        except Exception as error:  # noqa: BLE001 - one broken device, not a broken machine
            notes.append(f"device {index} could not be read ({type(error).__name__}: {error})")
            index += 1
            continue
        names.append(name)
        index += 1
    return names, notes


class DshowBackend:
    """A UVC PTZ camera driven through DirectShow on Windows."""

    name = "dshow"

    def __init__(self, device: str | None = None) -> None:
        """Remember which device name to look for; opening happens in `open`."""
        self.device_hint = (device or "osmo").strip()
        self.device_name: str | None = None
        self._camera = None
        self._specs: dict[Axis, AxisSpec] = {}
        self._opened = False

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        """Find the camera and read its advertised control ranges."""
        camera_control_class, _, _ = _declare_interfaces()
        try:
            from pygrabber.dshow_graph import SystemDeviceEnum  # noqa: PLC0415
            from pygrabber.dshow_ids import DeviceCategories  # noqa: PLC0415
        except ImportError as error:  # pragma: no cover - depends on the environment
            raise BackendUnavailableError(
                f"pygrabber is required for the DirectShow backend: {error}"
            )

        category = (
            DeviceCategories.VideoInputDevice.value
            if hasattr(DeviceCategories.VideoInputDevice, "value")
            else str(DeviceCategories.VideoInputDevice)
        )
        enumerator = SystemDeviceEnum()

        found: tuple[Any, str] | None = None
        index = 0
        seen: list[str] = []
        while True:
            try:
                filter_, name = enumerator.get_filter_by_index(category, index)
            except ValueError:
                break  # end of the device list
            except Exception as error:  # noqa: BLE001 - skip a device that will not load
                _LOGGER.debug("skipping video device %s: %s", index, error)
                index += 1
                continue
            seen.append(name)
            if self.device_hint.lower() in name.lower():
                found = (filter_, name)
                break
            index += 1

        if found is None:
            listing = ", ".join(seen) if seen else "none"
            raise BackendUnavailableError(
                f"no video input device matching {self.device_hint!r} (found: {listing})"
            )

        filter_, name = found
        self.device_name = name
        try:
            self._camera = filter_.QueryInterface(camera_control_class)
        except Exception as error:  # noqa: BLE001 - the device exists but exposes no PTZ
            raise BackendUnavailableError(
                f"{name!r} exposes no UVC camera controls ({type(error).__name__}); "
                "it may not be a PTZ camera, or not be in webcam mode"
            ) from error

        self._specs = self._read_specs()
        if not self._specs:
            raise BackendUnavailableError(
                f"{name!r} reported no usable pan/tilt/roll/zoom controls"
            )
        self._opened = True
        _LOGGER.info("opened %s with axes: %s", name, ", ".join(a.value for a in self._specs))

    def _read_specs(self) -> dict[Axis, AxisSpec]:
        """Ask the device for each axis's range. Axes that refuse are simply absent."""
        specs: dict[Axis, AxisSpec] = {}
        for axis, prop_id in CAMERA_PROPERTY_IDS.items():
            try:
                minimum, maximum, step, default, _ = self._camera.GetRange(prop_id)
            except Exception:  # noqa: BLE001 - absent axis; exposure/iris/focus behave this way
                continue
            specs[axis] = AxisSpec(
                axis=axis,
                minimum=int(minimum),
                maximum=int(maximum),
                step=int(step) or 1,
                default=int(default),
                unit="ratio_x100" if axis is Axis.ZOOM else "degrees",
            )
        return specs

    def close(self) -> None:
        """Drop the interface. Safe to call twice."""
        self._camera = None
        self._opened = False

    # -- contract ----------------------------------------------------------
    def specs(self) -> dict[Axis, AxisSpec]:
        """Ranges as advertised by this device."""
        return dict(self._specs)

    def settle_seconds(self, axis: Axis) -> float:
        """Latency plus the measured settle time for this axis."""
        return LATENCY_SECONDS + SETTLE_SECONDS.get(axis, 1.0)

    def read_back_lag(self) -> int:
        """Observed lag: the report trailed the command by one interaction."""
        return 1

    def read(self, axis: Axis) -> int | None:
        """Report the device's own value. A hint: measured lying in both directions."""
        if self._camera is None or axis not in self._specs:
            return None
        try:
            value, _ = self._camera.Get(CAMERA_PROPERTY_IDS[axis])
        except Exception:  # noqa: BLE001 - a refused read is 'unknown', not an error
            return None
        return int(value)

    def write(self, axis: Axis, value: int) -> None:
        """Send an absolute target. The caller clamps; this clamps defensively as well."""
        if self._camera is None:
            raise BackendUnavailableError("camera is not open")
        spec = self._specs.get(axis)
        if spec is None:
            raise ValueError(f"this camera has no {axis.value} axis")
        self._camera.Set(CAMERA_PROPERTY_IDS[axis], spec.clamp(value), FLAG_MANUAL)

    def frame(self) -> np.ndarray:
        """One greyscale frame straight from the device, via ffmpeg's DirectShow input."""
        if not self.device_name:
            raise BackendUnavailableError("camera is not open")
        return capture_dshow(self.device_name)

    def describe(self) -> dict:
        """Backend description for the status tool."""
        return {
            "backend": self.name,
            "simulated": False,
            "device": self.device_name,
            "device_hint": self.device_hint,
            "axes": {axis.value: spec.to_dict() for axis, spec in self._specs.items()},
            "measured": {
                "latency_seconds": LATENCY_SECONDS,
                "settle_seconds": {axis.value: value for axis, value in SETTLE_SECONDS.items()},
                "read_back_hint": "the device's report lags one interaction and can echo a "
                "request it never applied; moves are confirmed from frames",
            },
        }
