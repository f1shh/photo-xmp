#!/usr/bin/env python3
"""Build verified darktable XMP parameter blobs and sidecars.

This module deliberately covers a small, tested set of module encodings.  It is
not a generic serializer for every darktable module. Earlier encodings were
derived on 5.2.1; White Balance and Color Calibration were confirmed from 5.6.1
source, and the covered set was render-verified on 5.6.1. Compatibility is
governed primarily by
each history item's module/blend/mask version, not by a global application
allowlist.  Detect the runtime, use a neutral blend blob from that runtime, and
render-test the resulting sidecar.
"""

from __future__ import annotations

import argparse
import html
import math
import os
import platform
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import warnings
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

RENDER_TESTED_DARKTABLE = ("5.2.1", "5.6.1")
TEST_PROVENANCE_DARKTABLE = "5.6.1"
XMP_VERSION = 5
IOP_ORDER_VERSION = 5
BLENDOP_VERSION = 14
BLEND_BLOB_LENGTH = 420
BLEND_PARAMS_OFFSET = 68
BLEND_BOOST_OFFSET = 324

MASK_CIRCLE = 1
MASK_PATH = 2
MASK_GROUP = 4
MASK_GRADIENT = 16
MASK_ELLIPSE = 32
MASK_BRUSH = 64
MASK_OBJECT = 256

MASK_MODE_ENABLED = 1
MASK_MODE_DRAWN = 2
MASK_MODE_PARAMETRIC = 4

BLEND_CS_NONE = 0
BLEND_CS_RAW = 1
BLEND_CS_LAB = 2
BLEND_CS_RGB_DISPLAY = 3
BLEND_CS_RGB_SCENE = 4
BLEND_CHANNELS = {
    BLEND_CS_RAW: {
        "gray_in": 0, "gray_out": 4,
    },
    BLEND_CS_LAB: {
        "l_in": 0, "a_in": 1, "b_in": 2,
        "l_out": 4, "a_out": 5, "b_out": 6,
        "c_in": 8, "h_in": 9, "c_out": 12, "h_out": 13,
    },
    BLEND_CS_RGB_DISPLAY: {
        "gray_in": 0, "red_in": 1, "green_in": 2, "blue_in": 3,
        "gray_out": 4, "red_out": 5, "green_out": 6, "blue_out": 7,
        "hue_in": 8, "saturation_in": 9, "lightness_in": 10,
        "hue_out": 12, "saturation_out": 13, "lightness_out": 14,
    },
    BLEND_CS_RGB_SCENE: {
        "gray_in": 0, "red_in": 1, "green_in": 2, "blue_in": 3,
        "gray_out": 4, "red_out": 5, "green_out": 6, "blue_out": 7,
        "jz_in": 8, "cz_in": 9, "hz_in": 10,
        "jz_out": 12, "cz_out": 13, "hz_out": 14,
    },
}

COLOR_EQUALIZER_HUES = (
    "red", "orange", "yellow", "green",
    "cyan", "blue", "lavender", "magenta",
)
TONE_EQUALIZER_BANDS = (
    "noise", "ultra_deep_blacks", "deep_blacks", "blacks",
    "shadows", "midtones", "highlights", "whites", "speculars",
)
COLOR_BALANCE_RGB_FIELDS = (
    "shadows_Y", "shadows_C", "shadows_H",
    "midtones_Y", "midtones_C", "midtones_H",
    "highlights_Y", "highlights_C", "highlights_H",
    "global_Y", "global_C", "global_H",
    "shadows_weight", "white_fulcrum", "highlights_weight",
    "chroma_shadows", "chroma_highlights", "chroma_global",
    "chroma_midtones", "saturation_global", "saturation_highlights",
    "saturation_midtones", "saturation_shadows", "hue_angle",
    "brilliance_global", "brilliance_highlights", "brilliance_midtones",
    "brilliance_shadows", "mask_grey_fulcrum", "vibrance",
    "grey_fulcrum", "contrast",
)
COLOR_BALANCE_RGB_INDEX = {name: index for index, name in enumerate(COLOR_BALANCE_RGB_FIELDS)}

WHITE_BALANCE_PRESETS = {
    "unknown": -1,
    "as-shot": 0,
    "spot": 1,
    "user": 2,
    "d65": 3,
    "d65-late": 4,
}
COLOR_CALIBRATION_ILLUMINANTS = {
    "pipeline": 0,
    "incandescent": 1,
    "daylight": 2,
    "equal-energy": 3,
    "fluorescent": 4,
    "led": 5,
    "blackbody": 6,
    "custom": 7,
    "detect-surfaces": 8,
    "detect-edges": 9,
    "camera": 10,
}
COLOR_CALIBRATION_ADAPTATIONS = {
    "linear-bradford": 0,
    "cat16": 1,
    "full-bradford": 2,
    "xyz": 3,
    "none": 4,
}
COLOR_CALIBRATION_FLUORESCENTS = {f"f{index}": index - 1 for index in range(1, 13)}
COLOR_CALIBRATION_LEDS = {
    "b1": 0, "b2": 1, "b3": 2, "b4": 3, "b5": 4,
    "bh1": 5, "rgb1": 6, "v1": 7, "v2": 8,
}
COLOR_CALIBRATION_FLUORESCENT_XY = (
    (0.31310, 0.33727), (0.37208, 0.37529), (0.40910, 0.39430),
    (0.44018, 0.40329), (0.31379, 0.34531), (0.37790, 0.38835),
    (0.31292, 0.32933), (0.34588, 0.35875), (0.37417, 0.37281),
    (0.34609, 0.35986), (0.38052, 0.37713), (0.43695, 0.40441),
)
COLOR_CALIBRATION_LED_XY = (
    (0.4560, 0.4078), (0.4357, 0.4012), (0.3756, 0.3723),
    (0.3422, 0.3502), (0.3118, 0.3236), (0.4474, 0.4066),
    (0.4557, 0.4211), (0.4560, 0.4548), (0.3781, 0.3775),
)


def _finite(values: Iterable[float], label: str) -> list[float]:
    result = [float(value) for value in values]
    if not all(math.isfinite(value) for value in result):
        raise ValueError(f"{label} must contain only finite numbers")
    return result


def _require_length(blob: bytes | bytearray, expected: int, label: str) -> bytes:
    blob = bytes(blob)
    if len(blob) != expected:
        raise AssertionError(f"{label}: expected {expected} bytes, got {len(blob)}")
    return blob


def detect_darktable_version(executable: str = "darktable-cli") -> str:
    """Return the installed semantic version reported by darktable-cli."""
    resolved = resolve_darktable_executable(executable)
    completed = subprocess.run(
        [str(resolved), "--version"], check=True, text=True, capture_output=True
    )
    output = completed.stdout + completed.stderr
    match = re.search(r"(?<![\d.])(\d+\.\d+\.\d+)(?![\d.])", output)
    if not match:
        raise RuntimeError(f"could not parse darktable version from: {output.strip()}")
    return match.group(1)


def resolve_darktable_executable(executable: str = "darktable-cli") -> Path:
    """Resolve darktable-cli, including the official Windows App Paths entry."""
    candidate = Path(executable).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    found = shutil.which(executable)
    if found is not None:
        return Path(found).resolve()
    if platform.system() == "Windows":
        names = [Path(executable).name]
        if not names[0].lower().endswith(".exe"):
            names.append(names[0] + ".exe")
        try:
            import winreg
        except ImportError:
            winreg = None
        if winreg is not None:
            for name in names:
                key_name = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}"
                for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                    try:
                        with winreg.OpenKey(hive, key_name) as key:
                            registered = Path(winreg.QueryValue(key, None))
                    except OSError:
                        continue
                    if registered.is_file():
                        return registered.resolve()
        for variable in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
            base = os.environ.get(variable)
            if not base:
                continue
            for name in names:
                installed = Path(base) / "darktable" / "bin" / name
                if installed.is_file():
                    return installed.resolve()
    raise FileNotFoundError(f"darktable executable not found: {executable}")


def check_darktable_version(
    executable: str = "darktable-cli",
    tested: str | Sequence[str] = RENDER_TESTED_DARKTABLE,
) -> str:
    """Report runtime provenance without treating it as an XMP allowlist."""
    installed = detect_darktable_version(executable)
    known = (tested,) if isinstance(tested, str) else tuple(tested)
    if installed not in known:
        message = (
            f"darktable {installed} is not in the render-tested runtime list "
            f"({', '.join(known)}); continue with a module-level smoke render "
            "and reject conversion warnings or behavioral discrepancies"
        )
        warnings.warn(message, RuntimeWarning, stacklevel=2)
    return installed


def assert_darktable_version(
    executable: str = "darktable-cli",
    expected: str | Sequence[str] = RENDER_TESTED_DARKTABLE,
) -> str:
    """Compatibility wrapper retained for older workflow builders.

    Despite its historical name it no longer rejects a runtime solely by its
    application version; it reports an untested runtime and lets the required
    render validation decide compatibility.
    """
    return check_darktable_version(executable, expected)


def _enum_value(value: str | int, choices: Mapping[str, int], label: str) -> int:
    if isinstance(value, str):
        try:
            return choices[value]
        except KeyError as exc:
            raise ValueError(
                f"unknown {label} {value!r}; use one of {', '.join(choices)}"
            ) from exc
    raw = int(value)
    if raw not in choices.values():
        raise ValueError(f"unknown {label} enum {raw}")
    return raw


def pack_white_balance_v4(
    red: float, green: float, blue: float, fourth: float = 1.0,
    *, preset: str | int = "user",
) -> bytes:
    """Pack darktable ``temperature`` v4 channel multipliers.

    Kelvin and tint are intentionally not accepted here: their conversion to
    camera multipliers depends on the current image's input matrix.  For RAW,
    inherit the same image's coefficients and adjust them, or supply coefficients
    obtained for that camera/image.
    """
    coefficients = _finite([red, green, blue], "white balance RGB coefficients")
    fourth = float(fourth)
    if any(value <= 0.0 or value > 8.0 for value in coefficients):
        raise ValueError("white balance RGB coefficients must be in (0, 8]")
    if not math.isnan(fourth) and (not math.isfinite(fourth) or fourth <= 0.0 or fourth > 8.0):
        raise ValueError("white balance fourth coefficient must be NaN or in (0, 8]")
    preset_value = _enum_value(preset, WHITE_BALANCE_PRESETS, "white balance preset")
    return _require_length(
        struct.pack("<4fi", *coefficients, fourth, preset_value), 20, "temperature v4"
    )


def unpack_white_balance_v4(blob: bytes) -> dict[str, object]:
    """Decode a ``temperature`` v4 blob for same-image adjustment."""
    red, green, blue, fourth, preset = struct.unpack(
        "<4fi", _require_length(blob, 20, "temperature v4")
    )
    return {
        "red": red, "green": green, "blue": blue, "fourth": fourth,
        "preset": preset,
    }


def relative_white_balance_coefficients(
    base: Sequence[float] = (1.0, 1.0, 1.0, 1.0),
    *, warmth_ev: float = 0.0, tint_ev: float = 0.0, normalize_green: bool = False,
) -> tuple[float, float, float, float]:
    """Apply a relative warmth/tint move to known channel multipliers.

    Positive ``warmth_ev`` raises red relative to blue. Positive ``tint_ev``
    raises red and blue relative to green (magenta); negative values move toward
    green. These are relative log2 controls, not Kelvin or a camera-independent
    replacement for an as-shot RAW white balance.
    """
    if len(base) != 4:
        raise ValueError("white balance base needs four channel multipliers")
    red, green, blue = _finite(base[:3], "white balance RGB base")
    fourth = float(base[3])
    if not math.isnan(fourth) and not math.isfinite(fourth):
        raise ValueError("white balance fourth coefficient must be finite or NaN")
    warmth_ev, tint_ev = _finite([warmth_ev, tint_ev], "relative white balance")
    red *= 2.0 ** (warmth_ev + tint_ev)
    blue *= 2.0 ** (-warmth_ev + tint_ev)
    if normalize_green:
        if green <= 0.0:
            raise ValueError("cannot normalize white balance with a non-positive green channel")
        red, blue = red / green, blue / green
        if not math.isnan(fourth):
            fourth /= green
        green = 1.0
    result = (red, green, blue, fourth)
    if any(value <= 0.0 or value > 8.0 for value in result[:3]):
        raise ValueError("relative white balance produced RGB coefficients outside (0, 8]")
    if not math.isnan(fourth) and (fourth <= 0.0 or fourth > 8.0):
        raise ValueError("relative white balance produced an invalid fourth coefficient")
    return result


def _vector4(
    values: Sequence[float], label: str, *, fourth: float = 0.0,
) -> tuple[float, float, float, float]:
    if len(values) == 3:
        values = (*values, fourth)
    if len(values) != 4:
        raise ValueError(f"{label} needs three or four values")
    result = tuple(_finite(values, label))
    if any(value < -2.0 or value > 2.0 for value in result):
        raise ValueError(f"{label} values must be in [-2, 2]")
    return result


def daylight_xy(temperature_kelvin: float) -> tuple[float, float]:
    """Match darktable's CIE daylight-locus CCT-to-xy conversion."""
    temperature = _finite([temperature_kelvin], "daylight temperature")[0]
    if not 4000.0 <= temperature <= 25000.0:
        raise ValueError("daylight temperature must be in [4000, 25000] K")
    if temperature <= 7000.0:
        x = ((-4.6070e9 / temperature + 2.9678e6) / temperature + 0.09911e3) / temperature + 0.244063
    else:
        x = ((-2.0064e9 / temperature + 1.9018e6) / temperature + 0.24748e3) / temperature + 0.237040
    y = (-3.0 * x + 2.87) * x - 0.275
    return x, y


def blackbody_xy(temperature_kelvin: float) -> tuple[float, float]:
    """Match darktable's Planckian-locus CCT-to-xy conversion."""
    temperature = _finite([temperature_kelvin], "blackbody temperature")[0]
    if not 1667.0 <= temperature <= 25000.0:
        raise ValueError("blackbody temperature must be in [1667, 25000] K")
    if temperature <= 4000.0:
        x = ((-0.2661239e9 / temperature - 0.2343589e6) / temperature + 0.8776956e3) / temperature + 0.179910
    else:
        x = ((-3.0258469e9 / temperature + 2.1070379e6) / temperature + 0.2226347e3) / temperature + 0.240390
    if temperature <= 2222.0:
        y = ((-1.1063814 * x - 1.34811020) * x + 2.18555832) * x - 0.20219683
    elif temperature <= 4000.0:
        y = ((-0.9549476 * x - 1.37418593) * x + 2.09137015) * x - 0.16748867
    else:
        y = ((3.0817580 * x - 5.87338670) * x + 3.75112997) * x - 0.37001483
    return x, y


def color_calibration_illuminant_xy(
    illuminant: str | int, *, temperature_kelvin: float = 5003.0,
    fluorescent: str | int = "f3", led: str | int = "b5",
    custom_xy: Sequence[float] | None = None,
) -> tuple[float, float]:
    """Return the xy chromaticity stored beside a standard illuminant."""
    value = _enum_value(illuminant, COLOR_CALIBRATION_ILLUMINANTS, "illuminant")
    if value == COLOR_CALIBRATION_ILLUMINANTS["pipeline"]:
        return 0.34567, 0.35850  # darktable pipeline white (D50)
    if value == COLOR_CALIBRATION_ILLUMINANTS["incandescent"]:
        return 0.44757, 0.40745
    if value == COLOR_CALIBRATION_ILLUMINANTS["equal-energy"]:
        return 1.0 / 3.0, 1.0 / 3.0
    if value == COLOR_CALIBRATION_ILLUMINANTS["daylight"]:
        return daylight_xy(temperature_kelvin)
    if value == COLOR_CALIBRATION_ILLUMINANTS["blackbody"]:
        return blackbody_xy(temperature_kelvin)
    if value == COLOR_CALIBRATION_ILLUMINANTS["fluorescent"]:
        index = _enum_value(fluorescent, COLOR_CALIBRATION_FLUORESCENTS, "fluorescent")
        return COLOR_CALIBRATION_FLUORESCENT_XY[index]
    if value == COLOR_CALIBRATION_ILLUMINANTS["led"]:
        index = _enum_value(led, COLOR_CALIBRATION_LEDS, "LED")
        return COLOR_CALIBRATION_LED_XY[index]
    if value == COLOR_CALIBRATION_ILLUMINANTS["custom"]:
        if custom_xy is None or len(custom_xy) != 2:
            raise ValueError("custom illuminant requires custom_xy=(x, y)")
        x, y = _finite(custom_xy, "custom illuminant xy")
        if x <= 0.0 or y <= 0.0 or x + y >= 1.0:
            raise ValueError("custom illuminant needs x > 0, y > 0, and x + y < 1")
        return x, y
    raise ValueError(
        "camera/detection illuminants are source-dependent; inherit them from "
        "the same image's darktable history instead of synthesizing them"
    )


def pack_color_calibration_v3(
    *,
    red: Sequence[float] = (1.0, 0.0, 0.0),
    green: Sequence[float] = (0.0, 1.0, 0.0),
    blue: Sequence[float] = (0.0, 0.0, 1.0),
    saturation: Sequence[float] = (0.0, 0.0, 0.0),
    lightness: Sequence[float] = (0.0, 0.0, 0.0),
    grey: Sequence[float] = (0.0, 0.0, 0.0),
    normalize_red: bool = False, normalize_green: bool = False,
    normalize_blue: bool = False, normalize_saturation: bool = False,
    normalize_lightness: bool = False, normalize_grey: bool = True,
    illuminant: str | int = "pipeline",
    fluorescent: str | int = "f3", led: str | int = "b5",
    adaptation: str | int | None = None,
    x: float | None = None, y: float | None = None,
    temperature_kelvin: float = 5003.0, gamut: float | None = None,
    clip: bool | None = None, algorithm_version: int = 2,
) -> bytes:
    """Pack darktable ``channelmixerrgb`` (Color Calibration) v3.

    The default is a no-op identity channel mixer with chromatic adaptation
    bypassed. Selecting a real illuminant defaults to CAT16, gamut compression
    1.0, and negative-RGB clipping. Camera and auto-detected illuminants must be
    inherited from the exact source image rather than synthesized.
    """
    illuminant_value = _enum_value(
        illuminant, COLOR_CALIBRATION_ILLUMINANTS, "illuminant"
    )
    if illuminant_value in {8, 9, 10}:
        raise ValueError(
            "camera/detection illuminants are source-dependent; inherit the "
            "same-image channelmixerrgb history entry"
        )
    if adaptation is None:
        adaptation = "none" if illuminant_value == 0 else "cat16"
    adaptation_value = _enum_value(
        adaptation, COLOR_CALIBRATION_ADAPTATIONS, "chromatic adaptation"
    )
    temperature_kelvin = _finite(
        [temperature_kelvin], "color calibration temperature"
    )[0]
    if not 1667.0 <= temperature_kelvin <= 25000.0:
        raise ValueError("color calibration temperature must be in [1667, 25000] K")
    custom_xy = None if x is None and y is None else (x, y)
    if (x is None) != (y is None):
        raise ValueError("pass both x and y, or neither")
    computed_x, computed_y = color_calibration_illuminant_xy(
        illuminant_value, temperature_kelvin=temperature_kelvin,
        fluorescent=fluorescent, led=led, custom_xy=custom_xy,
    )
    if illuminant_value != COLOR_CALIBRATION_ILLUMINANTS["custom"] and custom_xy is not None:
        raise ValueError("x/y overrides require illuminant='custom'")
    gamut_value = 0.0 if gamut is None and adaptation_value == 4 else (1.0 if gamut is None else float(gamut))
    gamut_value = _finite([gamut_value], "color calibration gamut")[0]
    if not 0.0 <= gamut_value <= 12.0:
        raise ValueError("color calibration gamut must be in [0, 12]")
    clip_value = adaptation_value != 4 if clip is None else bool(clip)
    if algorithm_version not in (0, 1, 2):
        raise ValueError("color calibration algorithm_version must be 0, 1, or 2")
    fluorescent_value = _enum_value(
        fluorescent, COLOR_CALIBRATION_FLUORESCENTS, "fluorescent"
    )
    led_value = _enum_value(led, COLOR_CALIBRATION_LEDS, "LED")
    arrays = (
        _vector4(red, "red channel"),
        _vector4(green, "green channel"),
        _vector4(blue, "blue channel"),
        _vector4(saturation, "saturation channel"),
        _vector4(lightness, "lightness channel"),
        _vector4(grey, "grey channel"),
    )
    floats = [value for array in arrays for value in array]
    integers = [
        normalize_red, normalize_green, normalize_blue, normalize_saturation,
        normalize_lightness, normalize_grey, illuminant_value, fluorescent_value,
        led_value, adaptation_value,
    ]
    blob = struct.pack(
        "<24f10i4f2i", *floats, *(int(value) for value in integers),
        computed_x, computed_y, temperature_kelvin, gamut_value,
        int(clip_value), int(algorithm_version),
    )
    return _require_length(blob, 160, "channelmixerrgb v3")


def unpack_color_calibration_v3(blob: bytes) -> dict[str, object]:
    """Decode a ``channelmixerrgb`` v3 blob for inspection or modification."""
    values = struct.unpack(
        "<24f10i4f2i", _require_length(blob, 160, "channelmixerrgb v3")
    )
    return {
        "red": values[0:4], "green": values[4:8], "blue": values[8:12],
        "saturation": values[12:16], "lightness": values[16:20],
        "grey": values[20:24],
        "normalize_red": bool(values[24]), "normalize_green": bool(values[25]),
        "normalize_blue": bool(values[26]), "normalize_saturation": bool(values[27]),
        "normalize_lightness": bool(values[28]), "normalize_grey": bool(values[29]),
        "illuminant": values[30], "fluorescent": values[31], "led": values[32],
        "adaptation": values[33], "x": values[34], "y": values[35],
        "temperature_kelvin": values[36], "gamut": values[37],
        "clip": bool(values[38]), "algorithm_version": values[39],
    }


def repack_color_calibration_v3(
    base_blob: bytes,
    *,
    matrix: Sequence[float] | None = None,
    saturation: Sequence[float] | None = None,
    lightness: Sequence[float] | None = None,
    grey: Sequence[float] | None = None,
    illuminant: str | int | None = None,
    fluorescent: str | int | None = None,
    led: str | int | None = None,
    adaptation: str | int | None = None,
    custom_xy: Sequence[float] | None = None,
    temperature_kelvin: float | None = None,
    gamut: float | None = None,
    clip: bool | None = None,
    normalize_red: bool | None = None,
    normalize_green: bool | None = None,
    normalize_blue: bool | None = None,
    normalize_saturation: bool | None = None,
    normalize_lightness: bool | None = None,
    normalize_grey: bool | None = None,
    algorithm_version: int | None = None,
) -> bytes:
    """Modify a same-image ``channelmixerrgb`` v3 blob without losing its baseline.

    This is the safe route for camera or auto-detected illuminants: their stored
    xy/temperature values remain untouched unless the caller explicitly selects
    a new, synthesizable illuminant. The input blob must come from the exact
    photograph being edited.
    """
    base = unpack_color_calibration_v3(base_blob)
    if matrix is not None:
        if len(matrix) != 9:
            raise ValueError("color calibration matrix needs nine row-major values")
        red, green, blue = matrix[0:3], matrix[3:6], matrix[6:9]
    else:
        red, green, blue = base["red"], base["green"], base["blue"]

    illuminant_value = (
        int(base["illuminant"]) if illuminant is None
        else _enum_value(illuminant, COLOR_CALIBRATION_ILLUMINANTS, "illuminant")
    )
    if custom_xy is not None and illuminant_value != COLOR_CALIBRATION_ILLUMINANTS["custom"]:
        raise ValueError("custom_xy requires illuminant='custom'")
    fluorescent_value = (
        int(base["fluorescent"]) if fluorescent is None
        else _enum_value(fluorescent, COLOR_CALIBRATION_FLUORESCENTS, "fluorescent")
    )
    led_value = (
        int(base["led"]) if led is None
        else _enum_value(led, COLOR_CALIBRATION_LEDS, "LED")
    )
    if adaptation is None:
        adaptation_value = (
            int(base["adaptation"])
            if illuminant is None
            else COLOR_CALIBRATION_ADAPTATIONS[
                "none" if illuminant_value == COLOR_CALIBRATION_ILLUMINANTS["pipeline"]
                else "cat16"
            ]
        )
    else:
        adaptation_value = _enum_value(
            adaptation, COLOR_CALIBRATION_ADAPTATIONS, "chromatic adaptation"
        )
    temperature_value = (
        float(base["temperature_kelvin"])
        if temperature_kelvin is None else float(temperature_kelvin)
    )
    if not math.isfinite(temperature_value) or not 1667.0 <= temperature_value <= 25000.0:
        raise ValueError("color calibration temperature must be in [1667, 25000] K")

    illuminant_changed = illuminant is not None
    if (
        temperature_kelvin is not None
        and illuminant_value not in {
            COLOR_CALIBRATION_ILLUMINANTS["daylight"],
            COLOR_CALIBRATION_ILLUMINANTS["blackbody"],
        }
    ):
        raise ValueError(
            "temperature_kelvin only applies to daylight or blackbody illuminants"
        )
    locus_changed = (
        illuminant_changed or temperature_kelvin is not None
        or fluorescent is not None or led is not None or custom_xy is not None
    )
    if locus_changed:
        if illuminant_value in {8, 9, 10}:
            raise ValueError(
                "camera/detection illuminant xy is source-dependent; preserve it "
                "unchanged or explicitly select a standard/custom illuminant"
            )
        xy = color_calibration_illuminant_xy(
            illuminant_value, temperature_kelvin=temperature_value,
            fluorescent=fluorescent_value, led=led_value, custom_xy=custom_xy,
        )
    else:
        xy = (float(base["x"]), float(base["y"]))

    arrays = (
        _vector4(red, "red channel"),
        _vector4(green, "green channel"),
        _vector4(blue, "blue channel"),
        _vector4(base["saturation"] if saturation is None else saturation, "saturation channel"),
        _vector4(base["lightness"] if lightness is None else lightness, "lightness channel"),
        _vector4(base["grey"] if grey is None else grey, "grey channel"),
    )
    gamut_value = float(base["gamut"] if gamut is None else gamut)
    if not math.isfinite(gamut_value) or not 0.0 <= gamut_value <= 12.0:
        raise ValueError("color calibration gamut must be in [0, 12]")
    clip_value = bool(base["clip"] if clip is None else clip)
    floats = [value for array in arrays for value in array]
    normalizations = (
        normalize_red, normalize_green, normalize_blue, normalize_saturation,
        normalize_lightness, normalize_grey,
    )
    normalization_names = (
        "normalize_red", "normalize_green", "normalize_blue",
        "normalize_saturation", "normalize_lightness", "normalize_grey",
    )
    integers = [
        *(
            base[name] if override is None else bool(override)
            for name, override in zip(normalization_names, normalizations)
        ),
        illuminant_value, fluorescent_value, led_value,
        adaptation_value,
    ]
    algorithm_value = (
        int(base["algorithm_version"])
        if algorithm_version is None else int(algorithm_version)
    )
    if algorithm_value not in (0, 1, 2):
        raise ValueError("color calibration algorithm_version must be 0, 1, or 2")
    blob = struct.pack(
        "<24f10i4f2i", *floats, *(int(value) for value in integers),
        *xy, temperature_value, gamut_value, clip_value, algorithm_value,
    )
    return _require_length(blob, 160, "channelmixerrgb v3")


def pack_exposure_v6(
    exposure: float,
    *,
    black: float = 0.0,
    mode: int = 0,
    deflicker_percentile: float = 50.0,
    deflicker_target_level: float = -4.0,
    compensate_exposure_bias: int = 0,
) -> bytes:
    values = _finite(
        [black, exposure, deflicker_percentile, deflicker_target_level], "exposure"
    )
    blob = struct.pack(
        "<iffffi", int(mode), *values, int(compensate_exposure_bias)
    )
    return _require_length(blob, 24, "exposure v6")


def unpack_exposure_v6(blob: bytes) -> dict[str, object]:
    mode, black, exposure, percentile, target, compensate = struct.unpack(
        "<iffffi", _require_length(blob, 24, "exposure v6")
    )
    return {
        "exposure": exposure, "black": black, "mode": mode,
        "deflicker_percentile": percentile,
        "deflicker_target_level": target,
        "compensate_exposure_bias": compensate,
    }


def pack_tone_equalizer_v2(
    bands: Sequence[float],
    *,
    blending: float = 3.0,
    smoothing: float = math.sqrt(2.0),
    feathering: float = 7.0,
    quantization: float = 0.0,
    contrast_boost: float = 0.0,
    exposure_boost: float = -1.57,
    details: int = 4,
    method: int = 4,
    iterations: int = 3,
) -> bytes:
    if len(bands) != 9:
        raise ValueError(
            "tone equalizer needs 9 bands in this order: "
            + ", ".join(TONE_EQUALIZER_BANDS)
        )
    floats = _finite(
        [
            *bands, blending, smoothing, feathering, quantization, contrast_boost,
            exposure_boost,
        ],
        "tone equalizer",
    )
    blob = struct.pack("<15f3i", *floats, int(details), int(method), int(iterations))
    return _require_length(blob, 72, "tone equalizer v2")


def unpack_tone_equalizer_v2(blob: bytes) -> dict[str, object]:
    values = struct.unpack("<15f3i", _require_length(blob, 72, "tone equalizer v2"))
    return {
        "bands": list(values[:9]), "blending": values[9],
        "smoothing": values[10], "feathering": values[11],
        "quantization": values[12], "contrast_boost": values[13],
        "exposure_boost": values[14], "details": values[15],
        "method": values[16], "iterations": values[17],
    }


def tone_equalizer_bands(**overrides: float) -> list[float]:
    """Return neutral nine-band values with readable named overrides."""
    result = [0.0] * len(TONE_EQUALIZER_BANDS)
    indexes = {name: index for index, name in enumerate(TONE_EQUALIZER_BANDS)}
    for name, value in overrides.items():
        if name not in indexes:
            raise KeyError(
                f"unknown tone equalizer band {name!r}; use one of "
                + ", ".join(TONE_EQUALIZER_BANDS)
            )
        result[indexes[name]] = float(value)
    return _finite(result, "tone equalizer bands")


def _checked_curve_nodes(
    nodes: Sequence[Sequence[float]], label: str
) -> list[tuple[float, float]]:
    if not 2 <= len(nodes) <= 20:
        raise ValueError(f"{label} needs between 2 and 20 nodes")
    checked = [(float(x), float(y)) for x, y in nodes]
    _finite((value for point in checked for value in point), label)
    if any(not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0) for x, y in checked):
        raise ValueError(f"{label} coordinates must be in [0, 1]")
    if any(right[0] <= left[0] for left, right in zip(checked, checked[1:])):
        raise ValueError(f"{label} x coordinates must be strictly increasing")
    return checked


def pack_rgb_curve_v1(
    nodes: Sequence[Sequence[float]] | None = None,
    *,
    red: Sequence[Sequence[float]] | None = None,
    green: Sequence[Sequence[float]] | None = None,
    blue: Sequence[Sequence[float]] | None = None,
    curve_types: Sequence[int] = (2, 2, 2),
    autoscale: int = 0,
    compensate_middle_grey: int = 0,
    preserve_colors: int = 1,
) -> bytes:
    """Pack linked or independent darktable RGB curves.

    The historical positional ``nodes`` form remains the linked-channel API.
    For independent curves omit it and provide all of ``red``, ``green`` and
    ``blue``; the packer then enables manual per-channel mode by default.
    """
    if len(curve_types) != 3:
        raise ValueError("curve_types must contain exactly three integers")
    independent = any(channel is not None for channel in (red, green, blue))
    if nodes is not None and independent:
        raise ValueError("pass linked nodes or independent red/green/blue curves, not both")
    if nodes is None:
        if any(channel is None for channel in (red, green, blue)):
            raise ValueError("independent RGB curves require red, green, and blue")
        curves = [
            _checked_curve_nodes(red or (), "red RGB curve"),
            _checked_curve_nodes(green or (), "green RGB curve"),
            _checked_curve_nodes(blue or (), "blue RGB curve"),
        ]
        if autoscale == 0:
            autoscale = 1
    else:
        checked = _checked_curve_nodes(nodes, "RGB curve")
        curves = [checked, checked, checked]

    blob = bytearray()
    for curve in curves:
        padded = curve + [(0.0, 0.0)] * (20 - len(curve))
        for x, y in padded:
            blob.extend(struct.pack("<ff", x, y))
    blob.extend(
        struct.pack(
            "<3i3iiii",
            *(len(curve) for curve in curves),
            *(int(value) for value in curve_types),
            int(autoscale), int(compensate_middle_grey), int(preserve_colors),
        )
    )
    return _require_length(blob, 516, "RGB curve v1")


def unpack_rgb_curve_v1(blob: bytes) -> dict[str, object]:
    raw = _require_length(blob, 516, "RGB curve v1")
    all_nodes = struct.unpack_from("<120f", raw, 0)
    counts_and_tail = struct.unpack_from("<9i", raw, 480)
    counts = counts_and_tail[:3]
    curves = []
    for channel, count in enumerate(counts):
        if not 2 <= count <= 20:
            raise ValueError(f"RGB curve channel {channel} has invalid node count {count}")
        offset = channel * 40
        curves.append([
            (all_nodes[offset + index * 2], all_nodes[offset + index * 2 + 1])
            for index in range(count)
        ])
    return {
        "red": curves[0], "green": curves[1], "blue": curves[2],
        "curve_types": counts_and_tail[3:6], "autoscale": counts_and_tail[6],
        "compensate_middle_grey": counts_and_tail[7],
        "preserve_colors": counts_and_tail[8],
    }


def pack_diffuse_v2(
    *, iterations: int = 1, sharpness: float = 0.0, radius: int = 8,
    regularization: float = 0.0, variance_threshold: float = 0.0,
    anisotropy: Sequence[float] = (0.0, 0.0, 0.0, 0.0),
    threshold: float = 0.0, speed: Sequence[float] = (0.0, 0.0, 0.0, 0.0),
    radius_center: int = 0,
) -> bytes:
    if len(anisotropy) != 4 or len(speed) != 4:
        raise ValueError("diffuse anisotropy and speed each need four orders")
    if not 0 <= int(iterations) <= 500 or not 0 <= int(radius) <= 2048:
        raise ValueError("diffuse iterations/radius are outside darktable's range")
    if not 0 <= int(radius_center) <= 1024:
        raise ValueError("diffuse radius_center must be in [0, 1024]")
    floats = _finite(
        [sharpness, regularization, variance_threshold, *anisotropy, threshold, *speed],
        "diffuse or sharpen",
    )
    if not -1.0 <= floats[0] <= 1.0:
        raise ValueError("diffuse sharpness must be in [-1, 1]")
    if not 0.0 <= floats[1] <= 4.0 or not -2.0 <= floats[2] <= 2.0:
        raise ValueError("diffuse regularization or variance threshold is out of range")
    if any(not -10.0 <= value <= 10.0 for value in floats[3:7]):
        raise ValueError("diffuse anisotropy values must be in [-10, 10]")
    if not 0.0 <= floats[7] <= 8.0 or any(
        not -1.0 <= value <= 1.0 for value in floats[8:12]
    ):
        raise ValueError("diffuse threshold or speed is out of range")
    return _require_length(
        struct.pack(
            "<ifi11fi", int(iterations), floats[0], int(radius),
            *floats[1:], int(radius_center),
        ),
        60, "diffuse v2",
    )


def unpack_diffuse_v2(blob: bytes) -> dict[str, object]:
    values = struct.unpack("<ifi11fi", _require_length(blob, 60, "diffuse v2"))
    return {
        "iterations": values[0], "sharpness": values[1], "radius": values[2],
        "regularization": values[3], "variance_threshold": values[4],
        "anisotropy": values[5:9], "threshold": values[9],
        "speed": values[10:14], "radius_center": values[14],
    }


def pack_haze_removal_v3(
    strength: float = 0.2, *, distance: float = 0.2,
    compatibility_mode: bool = False, adaptive: bool = True,
) -> bytes:
    strength, distance = _finite([strength, distance], "haze removal")
    if not -1.0 <= strength <= 1.0 or not 0.0 <= distance <= 1.0:
        raise ValueError("haze strength must be [-1,1] and distance [0,1]")
    return _require_length(
        struct.pack("<2f2i", strength, distance, int(compatibility_mode), int(adaptive)),
        16, "haze removal v3",
    )


def unpack_haze_removal_v3(blob: bytes) -> dict[str, object]:
    strength, distance, compatibility_mode, adaptive = struct.unpack(
        "<2f2i", _require_length(blob, 16, "haze removal v3")
    )
    return {
        "strength": strength, "distance": distance,
        "compatibility_mode": bool(compatibility_mode),
        "adaptive": bool(adaptive),
    }


def pack_crop_v3(
    left: float = 0.0, top: float = 0.0, right: float = 1.0, bottom: float = 1.0,
    *, ratio_n: int = -1, ratio_d: int = -1,
) -> bytes:
    left, top, right, bottom = _finite([left, top, right, bottom], "crop")
    if not (0.0 <= left < right <= 1.0 and 0.0 <= top < bottom <= 1.0):
        raise ValueError("crop needs 0 <= left < right <= 1 and 0 <= top < bottom <= 1")
    if right - left < 0.01 or bottom - top < 0.01:
        raise ValueError("crop width and height must each be at least 0.01")
    if (ratio_n == 0) != (ratio_d == 0):
        raise ValueError("crop ratio numerator and denominator must both be zero or nonzero")
    return _require_length(
        struct.pack("<4f2i", left, top, right, bottom, int(ratio_n), int(ratio_d)),
        24, "crop v3",
    )


def unpack_crop_v3(blob: bytes) -> dict[str, object]:
    left, top, right, bottom, ratio_n, ratio_d = struct.unpack(
        "<4f2i", _require_length(blob, 24, "crop v3")
    )
    return {
        "left": left, "top": top, "right": right, "bottom": bottom,
        "ratio_n": ratio_n, "ratio_d": ratio_d,
    }


def pack_perspective_v5(
    *, rotation: float = 0.0, lensshift_v: float = 0.0,
    lensshift_h: float = 0.0, shear: float = 0.0, focal_length: float = 28.0,
    crop_factor: float = 1.0, lens_dependence: float = 100.0, aspect: float = 1.0,
    mode: int = 0, crop_mode: int = 1, crop: Sequence[float] = (0.0, 1.0, 0.0, 1.0),
    drawn_lines: Sequence[Sequence[float]] = (),
    quad_lines: Sequence[float] = (0.0,) * 8,
) -> bytes:
    head = _finite(
        [rotation, lensshift_v, lensshift_h, shear, focal_length, crop_factor,
         lens_dependence, aspect], "perspective",
    )
    if not -180.0 <= rotation <= 180.0:
        raise ValueError("perspective rotation must be in [-180, 180]")
    if not -2.0 <= lensshift_v <= 2.0 or not -2.0 <= lensshift_h <= 2.0:
        raise ValueError("perspective lens shifts must be in [-2, 2]")
    if not -0.5 <= shear <= 0.5 or not 1.0 <= focal_length <= 2000.0:
        raise ValueError("perspective shear or focal length is out of range")
    if not 0.5 <= crop_factor <= 10.0 or not 0.0 <= lens_dependence <= 100.0:
        raise ValueError("perspective crop factor or lens dependence is out of range")
    if not 0.5 <= aspect <= 2.0 or mode not in (0, 1) or crop_mode not in (0, 1, 2):
        raise ValueError("perspective aspect, mode, or crop_mode is invalid")
    crop_values = _finite(crop, "perspective crop box")
    if len(crop_values) != 4:
        raise ValueError("perspective crop needs [left, right, top, bottom]")
    cl, cr, ct, cb = crop_values
    if not (0.0 <= cl < cr <= 1.0 and 0.0 <= ct < cb <= 1.0):
        raise ValueError("perspective crop box is invalid")
    if len(drawn_lines) > 50 or any(len(line) != 4 for line in drawn_lines):
        raise ValueError("perspective accepts at most 50 four-coordinate guide lines")
    line_values = _finite(
        [value for line in drawn_lines for value in line], "perspective guide lines"
    )
    line_values += [0.0] * (200 - len(line_values))
    quad_values = _finite(quad_lines, "perspective quadrilateral")
    if len(quad_values) != 8:
        raise ValueError("perspective quad_lines needs eight coordinates")
    return _require_length(
        struct.pack(
            "<8f2i4f200fi8f", *head, int(mode), int(crop_mode),
            *crop_values, *line_values, len(drawn_lines), *quad_values,
        ),
        892, "perspective correction v5",
    )


def unpack_perspective_v5(blob: bytes) -> dict[str, object]:
    values = struct.unpack(
        "<8f2i4f200fi8f",
        _require_length(blob, 892, "perspective correction v5"),
    )
    count = values[214]
    if not 0 <= count <= 50:
        raise ValueError(f"perspective guide-line count is invalid: {count}")
    flattened = values[14:14 + count * 4]
    return {
        "rotation": values[0], "lensshift_v": values[1],
        "lensshift_h": values[2], "shear": values[3],
        "focal_length": values[4], "crop_factor": values[5],
        "lens_dependence": values[6], "aspect": values[7],
        "mode": values[8], "crop_mode": values[9],
        "crop": list(values[10:14]),
        "drawn_lines": [
            list(flattened[index:index + 4])
            for index in range(0, len(flattened), 4)
        ],
        "quad_lines": list(values[215:223]),
    }


def pack_denoise_profile_v12(
    *, strength: float = 1.2, radius: float = 1.0, search_radius: float = 7.0,
    shadows: float = 0.0, bias: float = 0.0, scattering: float = 0.0,
    central_pixel_weight: float = 0.1, overshooting: float = 1.0,
    noise_a: Sequence[float] | None = None, noise_b: Sequence[float] | None = None,
    mode: int = 1, wavelet_color_mode: int = 1,
    curve_x: Sequence[Sequence[float]] | None = None,
    curve_y: Sequence[Sequence[float]] | None = None,
    wb_adaptive_anscombe: bool = True, fix_anscombe_and_nlmeans_norm: bool = True,
    use_new_vst: bool = True, compensate_highlight_preservation: bool = False,
) -> bytes:
    """Pack profiled denoise v12 with camera-profile autodetection by default."""
    head = _finite(
        [radius, search_radius, strength, shadows, bias, scattering,
         central_pixel_weight, overshooting], "profiled denoise",
    )
    if (noise_a is None) != (noise_b is None):
        raise ValueError("provide both noise_a and noise_b, or neither for autodetection")
    if noise_a is None:
        a, b = [-1.0, 0.0, 0.0], [0.0, 0.0, 0.0]
    else:
        if len(noise_a) != 3 or len(noise_b or ()) != 3:
            raise ValueError("noise_a and noise_b each need three camera-profile values")
        a, b = _finite(noise_a, "noise model a"), _finite(noise_b or (), "noise model b")
    default_x = [[band / 6.0 for band in range(7)] for _ in range(6)]
    default_y = [[0.5] * 7 for _ in range(6)]
    default_y[4] = [0.0] * 7
    x_values = default_x if curve_x is None else curve_x
    y_values = default_y if curve_y is None else curve_y
    if len(x_values) != 6 or len(y_values) != 6 or any(
        len(channel) != 7 for channel in [*x_values, *y_values]
    ):
        raise ValueError("profiled denoise curves need six channels of seven points")
    flat_x = _finite([value for channel in x_values for value in channel], "denoise curve x")
    flat_y = _finite([value for channel in y_values for value in channel], "denoise curve y")
    if mode not in (0, 1, 2, 3, 4) or wavelet_color_mode not in (0, 1):
        raise ValueError("profiled denoise mode is invalid")
    return _require_length(
        struct.pack(
            "<14fi84f5i", *head, *a, *b, int(mode), *flat_x, *flat_y,
            int(wb_adaptive_anscombe), int(fix_anscombe_and_nlmeans_norm),
            int(use_new_vst), int(wavelet_color_mode),
            int(compensate_highlight_preservation),
        ),
        416, "profiled denoise v12",
    )


def unpack_denoise_profile_v12(blob: bytes) -> dict[str, object]:
    values = struct.unpack(
        "<14fi84f5i", _require_length(blob, 416, "profiled denoise v12")
    )
    curve_values = values[15:99]
    return {
        "radius": values[0], "search_radius": values[1],
        "strength": values[2], "shadows": values[3], "bias": values[4],
        "scattering": values[5], "central_pixel_weight": values[6],
        "overshooting": values[7], "noise_a": values[8:11],
        "noise_b": values[11:14], "mode": values[14],
        "curve_x": [curve_values[index:index + 7] for index in range(0, 42, 7)],
        "curve_y": [curve_values[index:index + 7] for index in range(42, 84, 7)],
        "wb_adaptive_anscombe": bool(values[99]),
        "fix_anscombe_and_nlmeans_norm": bool(values[100]),
        "use_new_vst": bool(values[101]), "wavelet_color_mode": values[102],
        "compensate_highlight_preservation": bool(values[103]),
    }


def repack_denoise_profile_v12(blob: bytes, **overrides: object) -> bytes:
    """Modify a same-image or v12-preset denoise blob without losing its profile."""
    values = unpack_denoise_profile_v12(blob)
    unknown = set(overrides) - set(values)
    if unknown:
        raise ValueError("unknown profiled denoise fields: " + ", ".join(sorted(unknown)))
    values.update(overrides)
    return pack_denoise_profile_v12(**values)


def pack_color_equalizer_v4(
    *,
    saturation: Sequence[float] = (1.0,) * 8,
    hue: Sequence[float] = (0.0,) * 8,
    brightness: Sequence[float] = (1.0,) * 8,
    threshold: float = 0.10,
    smoothing_hue: float = 1.0,
    contrast: float = 0.0,
    white_level: float = 1.0,
    chroma_size: float = 1.5,
    param_size: float = 2.0,
    use_filter: int = 1,
    hue_shift: float = 0.0,
) -> bytes:
    for label, values in (
        ("saturation", saturation), ("hue", hue), ("brightness", brightness)
    ):
        if len(values) != 8:
            raise ValueError(
                f"{label} needs 8 values in this order: "
                + ", ".join(COLOR_EQUALIZER_HUES)
            )
    header = _finite(
        [threshold, smoothing_hue, contrast, white_level, chroma_size, param_size],
        "color equalizer header",
    )
    saturation = _finite(saturation, "color equalizer saturation")
    hue = _finite(hue, "color equalizer hue")
    brightness = _finite(brightness, "color equalizer brightness")
    hue_shift = _finite([hue_shift], "color equalizer hue shift")[0]
    blob = bytearray(struct.pack("<6fi", *header, int(use_filter)))
    blob.extend(struct.pack("<8f", *saturation))
    blob.extend(struct.pack("<8f", *hue))
    blob.extend(struct.pack("<8f", *brightness))
    blob.extend(struct.pack("<f", hue_shift))
    return _require_length(blob, 128, "color equalizer v4")


def unpack_color_equalizer_v4(blob: bytes) -> dict[str, object]:
    values = struct.unpack(
        "<6fi24ff", _require_length(blob, 128, "color equalizer v4")
    )
    return {
        "threshold": values[0], "smoothing_hue": values[1],
        "contrast": values[2], "white_level": values[3],
        "chroma_size": values[4], "param_size": values[5],
        "use_filter": values[6], "saturation": list(values[7:15]),
        "hue": list(values[15:23]), "brightness": list(values[23:31]),
        "hue_shift": values[31],
    }


def color_equalizer_values(
    neutral: float, overrides: Mapping[str, float] | None = None
) -> list[float]:
    """Build an eight-hue Color Equalizer vector from named overrides."""
    result = [float(neutral)] * len(COLOR_EQUALIZER_HUES)
    indexes = {name: index for index, name in enumerate(COLOR_EQUALIZER_HUES)}
    for name, value in (overrides or {}).items():
        if name not in indexes:
            raise KeyError(
                f"unknown color equalizer hue {name!r}; use one of "
                + ", ".join(COLOR_EQUALIZER_HUES)
            )
        result[indexes[name]] = float(value)
    return _finite(result, "color equalizer values")


def neutral_color_balance_rgb_v5() -> list[float]:
    params = [0.0] * 32
    params[COLOR_BALANCE_RGB_INDEX["shadows_weight"]] = 1.0
    params[COLOR_BALANCE_RGB_INDEX["highlights_weight"]] = 1.0
    params[COLOR_BALANCE_RGB_INDEX["mask_grey_fulcrum"]] = 0.1845
    params[COLOR_BALANCE_RGB_INDEX["grey_fulcrum"]] = 0.1845
    return params


def pack_color_balance_rgb_v5(
    params: Sequence[float] | None = None,
    *,
    saturation_formula: int = 1,
    overrides: Mapping[str, float] | None = None,
) -> bytes:
    """Pack Color Balance RGB using named overrides rather than magic indexes."""
    values = neutral_color_balance_rgb_v5() if params is None else list(params)
    if len(values) != 32:
        raise ValueError("Color Balance RGB v5 needs exactly 32 float parameters")
    for name, value in (overrides or {}).items():
        if name not in COLOR_BALANCE_RGB_INDEX:
            raise KeyError(
                f"unknown Color Balance RGB field {name!r}; use one of "
                + ", ".join(COLOR_BALANCE_RGB_FIELDS)
            )
        values[COLOR_BALANCE_RGB_INDEX[name]] = float(value)
    values = _finite(values, "Color Balance RGB")
    blob = struct.pack("<32fi", *values, int(saturation_formula))
    return _require_length(blob, 132, "Color Balance RGB v5")


def unpack_color_balance_rgb_v5(blob: bytes) -> dict[str, object]:
    values = struct.unpack(
        "<32fi", _require_length(blob, 132, "Color Balance RGB v5")
    )
    return {"params": list(values[:32]), "saturation_formula": values[32]}


def pack_basic_adjustments_v2(
    *,
    black_point: float = 0.0,
    exposure: float = 0.0,
    highlight_compression: float = 35.0,
    highlight_compression_threshold: float = 50.0,
    contrast: float = 0.0,
    preserve_colors: int = 1,
    middle_grey: float = 18.42,
    brightness: float = 0.0,
    saturation: float = 0.0,
    vibrance: float = 0.0,
    clip: float = 0.01,
) -> bytes:
    values = _finite(
        [
            black_point, exposure, highlight_compression,
            highlight_compression_threshold, contrast, middle_grey, brightness,
            saturation, vibrance, clip,
        ],
        "basic adjustments",
    )
    blob = struct.pack(
        "<5fi5f", *values[:5], int(preserve_colors), *values[5:]
    )
    return _require_length(blob, 44, "basic adjustments v2")


def unpack_basic_adjustments_v2(blob: bytes) -> dict[str, object]:
    values = struct.unpack("<5fi5f", _require_length(blob, 44, "basic adjustments v2"))
    return {
        "black_point": values[0], "exposure": values[1],
        "highlight_compression": values[2],
        "highlight_compression_threshold": values[3],
        "contrast": values[4], "preserve_colors": values[5],
        "middle_grey": values[6], "brightness": values[7],
        "saturation": values[8], "vibrance": values[9],
        "clip": values[10],
    }


def pack_flip_v2(raw_enum: int, *, mapping_verified: bool = False) -> bytes:
    """Pack a raw orientation enum only after its visual mapping was verified."""
    if not mapping_verified:
        raise ValueError(
            "flip enum meanings are not asserted by this library; render-verify the value "
            "or inherit it from same-image history, then set mapping_verified=True"
        )
    return _require_length(struct.pack("<i", int(raw_enum)), 4, "flip v2")


def unpack_flip_v2(blob: bytes) -> dict[str, object]:
    return {"raw_enum": struct.unpack("<i", _require_length(blob, 4, "flip v2"))[0]}


def pack_ellipse_mask_v6(
    cx: float, cy: float, rx: float, ry: float,
    *, rotation: float = 0.0, border: float = 0.08, flags: int = 0,
) -> bytes:
    values = _finite([cx, cy, rx, ry, rotation, border], "ellipse mask")
    if any(not 0.0 <= value <= 1.0 for value in values[:4]):
        raise ValueError("ellipse center and radii must use normalized [0, 1] coordinates")
    if rx <= 0.0 or ry <= 0.0 or border < 0.0:
        raise ValueError("ellipse radii must be positive and border must be non-negative")
    return _require_length(struct.pack("<6fi", *values, int(flags)), 28, "ellipse mask v6")


def pack_gradient_mask_v6(
    anchor_x: float, anchor_y: float, *, rotation: float = 0.0,
    compression: float = 0.1, steepness: float = 0.0, curvature: float = 0.0,
    state: int = 2,
) -> bytes:
    values = _finite(
        [anchor_x, anchor_y, rotation, compression, steepness, curvature],
        "gradient mask",
    )
    if not 0.0 <= anchor_x <= 1.0 or not 0.0 <= anchor_y <= 1.0:
        raise ValueError("gradient anchor must use normalized [0, 1] coordinates")
    if not 0.001 <= compression <= 1.0 or not -2.0 <= curvature <= 2.0:
        raise ValueError("gradient compression or curvature is outside darktable's range")
    if state not in (1, 2):
        raise ValueError("gradient state must be 1 (linear) or 2 (sigmoidal)")
    return _require_length(struct.pack("<6fi", *values, int(state)), 28, "gradient mask v6")


def _mask_pair(value: float | Sequence[float], label: str) -> tuple[float, float]:
    values = (float(value), float(value)) if isinstance(value, (int, float)) else tuple(value)
    if len(values) != 2:
        raise ValueError(f"{label} needs one value or a two-value pair")
    return tuple(_finite(values, label))


def pack_path_mask_v6(points: Sequence[Mapping[str, object]]) -> bytes:
    """Pack a closed Bézier path from normalized point dictionaries."""
    if len(points) < 3:
        raise ValueError("a path mask needs at least three points")
    blob = bytearray()
    for index, point in enumerate(points):
        corner = _mask_pair(point["corner"], f"path point {index} corner")
        ctrl1 = _mask_pair(point.get("ctrl1", (-1.0, -1.0)), f"path point {index} ctrl1")
        ctrl2 = _mask_pair(point.get("ctrl2", (-1.0, -1.0)), f"path point {index} ctrl2")
        border = _mask_pair(point.get("border", 0.02), f"path point {index} border")
        if any(not 0.0 <= value <= 1.0 for value in corner):
            raise ValueError("path corners must use normalized [0, 1] coordinates")
        if any(value < 0.0 for value in border):
            raise ValueError("path feather borders must be non-negative")
        state = int(point.get("state", 1))
        if state not in (1, 2):
            raise ValueError("path point state must be 1 (automatic) or 2 (user)")
        blob.extend(struct.pack("<8fi", *corner, *ctrl1, *ctrl2, *border, state))
    return _require_length(blob, 36 * len(points), "path mask v6")


def pack_brush_mask_v6(points: Sequence[Mapping[str, object]]) -> bytes:
    """Pack an open pressure-style brush stroke from normalized points."""
    if len(points) < 2:
        raise ValueError("a brush mask needs at least two points")
    blob = bytearray()
    for index, point in enumerate(points):
        corner = _mask_pair(point["corner"], f"brush point {index} corner")
        ctrl1 = _mask_pair(point.get("ctrl1", (-1.0, -1.0)), f"brush point {index} ctrl1")
        ctrl2 = _mask_pair(point.get("ctrl2", (-1.0, -1.0)), f"brush point {index} ctrl2")
        border = _mask_pair(point.get("border", 0.02), f"brush point {index} border")
        density = float(point.get("density", 1.0))
        hardness = float(point.get("hardness", 0.5))
        _finite([density, hardness], f"brush point {index}")
        if any(not 0.0 <= value <= 1.0 for value in corner):
            raise ValueError("brush corners must use normalized [0, 1] coordinates")
        if any(value < 0.0 for value in border):
            raise ValueError("brush borders must be non-negative")
        if not 0.0 <= density <= 1.0 or not 0.0 <= hardness <= 1.0:
            raise ValueError("brush density and hardness must be in [0, 1]")
        state = int(point.get("state", 1))
        if state not in (1, 2):
            raise ValueError("brush point state must be 1 (automatic) or 2 (user)")
        blob.extend(struct.pack("<10fi", *corner, *ctrl1, *ctrl2, *border, density, hardness, state))
    return _require_length(blob, 44 * len(points), "brush mask v6")


def pack_mask_group_v6(
    child_ids: Sequence[int],
    group_id: int,
    *,
    opacity: float = 1.0,
    opacities: Sequence[float] | None = None,
    states: Sequence[int] | None = None,
) -> bytes:
    """Pack a mask group with optional per-child opacity and state values.

    ``opacity`` preserves the original uniform-opacity API.  Pass
    ``opacities`` when independently weighted regions share one masked module.
    The default states are 3 for the first child and union state 11 thereafter.
    """
    if not child_ids or len(set(child_ids)) != len(child_ids):
        raise ValueError("mask group child IDs must be non-empty and unique")
    if int(group_id) in {int(child) for child in child_ids}:
        raise ValueError("group ID must differ from every child ID")
    if opacities is None:
        child_opacities = [opacity] * len(child_ids)
    else:
        if opacity != 1.0:
            raise ValueError(
                "pass either uniform opacity or per-child opacities, not both"
            )
        if len(opacities) != len(child_ids):
            raise ValueError("each mask child needs exactly one opacity")
        child_opacities = list(opacities)
    child_opacities = _finite(child_opacities, "mask group opacities")
    if any(not 0.0 <= value <= 1.0 for value in child_opacities):
        raise ValueError("mask group opacities must be in [0, 1]")

    if states is None:
        child_states = [3] + [11] * (len(child_ids) - 1)
    else:
        if len(states) != len(child_ids):
            raise ValueError("each mask child needs exactly one state")
        child_states = [int(value) for value in states]
        if any(not -(2**31) <= value < 2**31 for value in child_states):
            raise ValueError("mask group states must fit a signed 32-bit integer")

    blob = bytearray()
    for child_id, state, child_opacity in zip(
        child_ids, child_states, child_opacities
    ):
        blob.extend(
            struct.pack(
                "<iiif", int(child_id), int(group_id), state, child_opacity
            )
        )
    return _require_length(blob, 16 * len(child_ids), "mask group v6")


def _choose_imgid(connection: sqlite3.Connection, imgid: int | None) -> int:
    ids = [row[0] for row in connection.execute("select distinct imgid from history")]
    if imgid is not None:
        if imgid not in ids:
            raise ValueError(f"imgid {imgid} is not present in the history table")
        return imgid
    if len(ids) != 1:
        raise ValueError(
            f"library contains {len(ids)} images with history; pass imgid explicitly"
        )
    return ids[0]


def _is_neutral_blend(version: int, blob: bytes | None) -> bool:
    return bool(
        version == BLENDOP_VERSION
        and blob is not None
        and len(blob) == BLEND_BLOB_LENGTH
        and struct.unpack_from("<I", blob, 0)[0] == 0
    )


def neutral_blend_v14() -> bytes:
    """Return the source-confirmed darktable v14 neutral blend structure."""
    parameters = [value for _ in range(16) for value in (0.0, 0.0, 1.0, 1.0)]
    blob = struct.pack(
        "<IiIffIIIfIffffI2I64f16f20siii",
        0, BLEND_CS_NONE, 0x18, 0.0, 100.0, 0, 0, 0,
        0.0, 0x05, 0.0, 0.0, 0.0, 0.0, 1, 0, 0,
        *parameters, *([0.0] * 16), b"\x00" * 20, 0, -1, 0,
    )
    return _require_length(blob, BLEND_BLOB_LENGTH, "neutral blend v14")


def load_blend_template(
    library_db: str | Path | None = None,
    *,
    imgid: int | None = None,
    data_db: str | Path | None = None,
) -> bytes:
    """Load a neutral blend v14 blob accepted by the active runtime.

    Prefer the current image's ``library.db`` history when available.  A
    ``data.db`` preset from the active runtime is a safe fallback for the neutral
    template and avoids opening the GUI solely to create a history row.
    """
    if library_db is None and data_db is None:
        raise ValueError("provide library_db, data_db, or both")
    if library_db is None and imgid is not None:
        raise ValueError("imgid can only be used with library_db")

    failures: list[str] = []
    if library_db is not None:
        connection = sqlite3.connect(str(library_db))
        try:
            selected_imgid = _choose_imgid(connection, imgid)
            rows = connection.execute(
                "select blendop_version, blendop_params from history "
                "where imgid=? order by num",
                (selected_imgid,),
            ).fetchall()
            for version, blob in rows:
                if _is_neutral_blend(version, blob):
                    return bytes(blob)
            failures.append(f"library imgid {selected_imgid} has no neutral blob")
        except (sqlite3.Error, ValueError) as exc:
            failures.append(f"library lookup failed: {exc}")
        finally:
            connection.close()

    if data_db is not None:
        connection = sqlite3.connect(str(data_db))
        try:
            rows = connection.execute(
                "select blendop_version, blendop_params from presets "
                "where blendop_version=? and length(blendop_params)=? "
                "order by rowid",
                (BLENDOP_VERSION, BLEND_BLOB_LENGTH),
            ).fetchall()
            for version, blob in rows:
                if _is_neutral_blend(version, blob):
                    return bytes(blob)
            failures.append("data presets have no neutral blob")
        except sqlite3.Error as exc:
            failures.append(f"data preset lookup failed: {exc}")
        finally:
            connection.close()

    raise ValueError(
        f"no neutral v{BLENDOP_VERSION} {BLEND_BLOB_LENGTH}-byte blend blob found; "
        + "; ".join(failures)
    )


def make_mask_blend(
    template: bytes, *, blend_colorspace: int, mask_id: int | None = None,
    parametric: Mapping[str, Sequence[float]] | None = None,
    inverted_channels: Iterable[str] = (), boosts: Mapping[str, float] | None = None,
    mask_combine: int = 0, opacity: float = 100.0,
    feathering_radius: float | None = None, blur_radius: float | None = None,
    contrast: float | None = None, brightness: float | None = None,
    details: float | None = None,
) -> bytes:
    """Attach drawn and/or parametric masks to a verified blend v14 template.

    Parametric sliders use darktable's normalized four-stop representation.
    Channel names are colorspace-specific and exposed through ``BLEND_CHANNELS``.
    """
    blob = bytearray(_require_length(template, BLEND_BLOB_LENGTH, "blend v14 template"))
    if blend_colorspace not in {BLEND_CS_RAW, BLEND_CS_LAB, BLEND_CS_RGB_DISPLAY, BLEND_CS_RGB_SCENE}:
        raise ValueError("masked blend color space must not be BLEND_CS_NONE")
    parametric = dict(parametric or {})
    inverted = set(inverted_channels)
    boost_values = dict(boosts or {})
    available = BLEND_CHANNELS[blend_colorspace]
    unknown = (set(parametric) | inverted | set(boost_values)) - set(available)
    if unknown:
        raise ValueError(
            "unknown parametric channels for this colorspace: " + ", ".join(sorted(unknown))
        )
    if mask_id is None and not parametric:
        raise ValueError("a masked blend needs a drawn mask_id, parametric channels, or both")
    mode = MASK_MODE_ENABLED
    if mask_id is not None:
        if int(mask_id) <= 0:
            raise ValueError("drawn mask_id must be positive")
        mode |= MASK_MODE_DRAWN
    if parametric:
        mode |= MASK_MODE_PARAMETRIC
    struct.pack_into("<I", blob, 0, mode)
    struct.pack_into("<i", blob, 4, int(blend_colorspace))
    if not math.isfinite(opacity) or not 0.0 <= opacity <= 100.0:
        raise ValueError("blend opacity must be in [0, 100]")
    struct.pack_into("<f", blob, 16, float(opacity))
    struct.pack_into("<I", blob, 20, int(mask_combine))
    struct.pack_into("<I", blob, 24, 0 if mask_id is None else int(mask_id))
    blendif = 0
    for name, stops in parametric.items():
        if len(stops) != 4:
            raise ValueError(f"parametric channel {name!r} needs four feather stops")
        values = _finite(stops, f"parametric channel {name}")
        if any(not 0.0 <= value <= 1.0 for value in values) or any(
            right < left for left, right in zip(values, values[1:])
        ):
            raise ValueError(
                f"parametric channel {name!r} needs ascending normalized stops in [0, 1]"
            )
        channel = available[name]
        blendif |= 1 << channel
        struct.pack_into("<4f", blob, BLEND_PARAMS_OFFSET + channel * 16, *values)
    for name in inverted:
        channel = available[name]
        if name not in parametric:
            raise ValueError(f"cannot invert inactive parametric channel {name!r}")
        blendif |= 1 << (channel + 16)
    for name, value in boost_values.items():
        value = _finite([value], f"parametric boost {name}")[0]
        struct.pack_into("<f", blob, BLEND_BOOST_OFFSET + available[name] * 4, value)
    if blend_colorspace == BLEND_CS_RGB_SCENE:
        for name in ("jz_in", "cz_in", "jz_out", "cz_out"):
            if name in available and name not in boost_values:
                struct.pack_into(
                    "<f", blob, BLEND_BOOST_OFFSET + available[name] * 4, -6.64385619
                )
    struct.pack_into("<I", blob, 28, blendif)
    optional_fields = (
        (32, feathering_radius), (40, blur_radius), (44, contrast),
        (48, brightness), (52, details),
    )
    for offset, value in optional_fields:
        if value is not None:
            value = _finite([value], "blend refinement")[0]
            struct.pack_into("<f", blob, offset, value)
    return bytes(blob)


def make_drawn_mask_blend(
    template: bytes, mask_id: int, *, blend_colorspace: int
) -> bytes:
    """Compatibility wrapper for the original drawn-mask API."""
    return make_mask_blend(
        template, blend_colorspace=blend_colorspace, mask_id=mask_id
    )


@dataclass(frozen=True)
class HistoryEntry:
    operation: str
    module_version: int
    params: bytes
    enabled: int = 1
    name: str = ""
    priority: int = 0
    blend_params: bytes | None = None
    blendop_version: int = BLENDOP_VERSION


@dataclass(frozen=True)
class MaskEntry:
    history_index: int
    mask_id: int
    mask_type: int
    name: str
    points: bytes
    point_count: int
    mask_version: int = 6


def load_history_entries(
    library_db: str | Path,
    *,
    imgid: int | None = None,
    operations: Iterable[str] | None = None,
) -> list[HistoryEntry]:
    """Load current-image baseline history from a compatible darktable DB.

    The caller must ensure ``imgid`` belongs to the source photograph being
    edited. This helper is not permission to copy camera/input parameters from
    an unrelated image.
    """
    selected_operations = None if operations is None else set(operations)
    connection = sqlite3.connect(str(library_db))
    try:
        selected_imgid = _choose_imgid(connection, imgid)
        rows = connection.execute(
            "select operation,module,op_params,enabled,blendop_params,"
            "blendop_version,multi_priority,multi_name from history "
            "where imgid=? order by num",
            (selected_imgid,),
        ).fetchall()
    finally:
        connection.close()
    result = []
    for operation, module, params, enabled, blend, blend_version, priority, name in rows:
        if selected_operations is not None and operation not in selected_operations:
            continue
        if params is None:
            raise ValueError(f"history operation {operation!r} has null parameters")
        if blend is not None:
            _require_length(
                blend, BLEND_BLOB_LENGTH, f"history operation {operation!r} blend"
            )
        result.append(HistoryEntry(
            operation=operation,
            module_version=int(module),
            params=bytes(params),
            enabled=int(enabled),
            name=name or "",
            priority=int(priority),
            blend_params=None if blend is None else bytes(blend),
            blendop_version=int(blend_version),
        ))
    if selected_operations is not None:
        missing = selected_operations - {entry.operation for entry in result}
        if missing:
            raise ValueError(
                "requested history operations are missing: " + ", ".join(sorted(missing))
            )
    return result


def load_mask_entries(
    library_db: str | Path, *, imgid: int | None = None
) -> list[MaskEntry]:
    """Load the final complete mask snapshot for an exact current image.

    darktable stores a complete forms snapshot at each ``masks_history.num``.
    Selecting the latest row for each form would incorrectly resurrect forms
    deleted from the final snapshot, so only the greatest history number is
    returned.
    """
    connection = sqlite3.connect(str(library_db))
    try:
        selected_imgid = _choose_imgid(connection, imgid)
        rows = connection.execute(
            "select num,formid,form,name,version,points,points_count "
            "from masks_history where imgid=? order by num,rowid",
            (selected_imgid,),
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        return []
    final_history_index = max(int(row[0]) for row in rows)
    result: list[MaskEntry] = []
    for num, formid, form, name, version, points, points_count in rows:
        if int(num) != final_history_index:
            continue
        result.append(MaskEntry(
            history_index=int(num), mask_id=int(formid), mask_type=int(form),
            name=name or "", points=bytes(points or b""),
            point_count=int(points_count), mask_version=int(version),
        ))
    return result


def load_preset_entry(
    data_db: str | Path, *, operation: str, name: str
) -> HistoryEntry:
    """Load one exact named darktable preset from the active runtime DB."""
    connection = sqlite3.connect(str(data_db))
    try:
        rows = connection.execute(
            "select op_version,op_params,enabled,blendop_params,blendop_version,"
            "multi_priority,multi_name from presets where operation=? and name=?",
            (operation, name),
        ).fetchall()
    finally:
        connection.close()
    if len(rows) != 1:
        raise ValueError(
            f"expected one preset {operation!r}/{name!r}, found {len(rows)}"
        )
    version, params, enabled, blend, blend_version, priority, multi_name = rows[0]
    if params is None:
        raise ValueError(f"preset {operation!r}/{name!r} has null parameters")
    if blend is not None:
        _require_length(blend, BLEND_BLOB_LENGTH, f"preset {operation!r}/{name!r} blend")
    return HistoryEntry(
        operation=operation, module_version=int(version), params=bytes(params),
        enabled=int(enabled), name=multi_name or name, priority=int(priority or 0),
        blend_params=None if blend is None else bytes(blend),
        blendop_version=int(blend_version or BLENDOP_VERSION),
    )


def list_presets(
    data_db: str | Path, *, operations: Iterable[str] | None = None
) -> list[dict[str, object]]:
    connection = sqlite3.connect(str(data_db))
    try:
        if operations is None:
            rows = connection.execute(
                "select operation,name,op_version,length(op_params),blendop_version "
                "from presets order by operation,name"
            ).fetchall()
        else:
            selected = sorted(set(operations))
            if not selected:
                return []
            placeholders = ",".join("?" for _ in selected)
            rows = connection.execute(
                "select operation,name,op_version,length(op_params),blendop_version "
                f"from presets where operation in ({placeholders}) order by operation,name",
                selected,
            ).fetchall()
    finally:
        connection.close()
    return [
        {
            "operation": operation, "name": name, "module_version": version,
            "parameter_bytes": size, "blendop_version": blend_version,
        }
        for operation, name, version, size, blend_version in rows
    ]


def read_xmp(
    path: str | Path,
) -> tuple[list[HistoryEntry], list[MaskEntry], dict[str, object]]:
    """Read darktable history and masks emitted by this or darktable itself."""
    root = ET.parse(path).getroot()
    entries: list[HistoryEntry] = []
    masks: list[MaskEntry] = []
    metadata: dict[str, object] = {"path": str(path)}

    def attrs_by_local(element: ET.Element) -> dict[str, str]:
        return {key.rsplit("}", 1)[-1]: value for key, value in element.attrib.items()}

    for element in root.iter():
        attrs = attrs_by_local(element)
        if "DerivedFrom" in attrs:
            metadata["source_name"] = attrs["DerivedFrom"]
        if "xmp_version" in attrs:
            metadata["xmp_version"] = int(attrs["xmp_version"])
        if "operation" in attrs:
            try:
                entries.append(HistoryEntry(
                    operation=attrs["operation"],
                    module_version=int(attrs["modversion"]),
                    params=bytes.fromhex(attrs["params"]),
                    enabled=int(attrs.get("enabled", "1")),
                    name=attrs.get("multi_name", ""),
                    priority=int(attrs.get("multi_priority", "0")),
                    blend_params=bytes.fromhex(attrs["blendop_params"]),
                    blendop_version=int(attrs.get("blendop_version", BLENDOP_VERSION)),
                ))
            except (KeyError, ValueError) as exc:
                raise ValueError(f"invalid history item in {path}: {exc}") from exc
        elif "mask_id" in attrs:
            try:
                masks.append(MaskEntry(
                    history_index=int(attrs["mask_num"]),
                    mask_id=int(attrs["mask_id"]),
                    mask_type=int(attrs["mask_type"]),
                    name=attrs.get("mask_name", ""),
                    points=bytes.fromhex(attrs.get("mask_points", "")),
                    point_count=int(attrs["mask_nb"]),
                    mask_version=int(attrs.get("mask_version", "6")),
                ))
            except (KeyError, ValueError) as exc:
                raise ValueError(f"invalid mask item in {path}: {exc}") from exc
    metadata["history_items"] = len(entries)
    metadata["mask_items"] = len(masks)
    return entries, masks, metadata


def current_mask_snapshot(masks: Sequence[MaskEntry]) -> list[MaskEntry]:
    """Return the complete forms snapshot darktable will use for rendering.

    ``dt_masks_read_masks_history()`` replaces the current forms with the forms
    attached to the greatest mask history number before ``history_end``.  Older
    snapshots are undo history, not additive mask fragments.
    """
    if not masks:
        return []
    final_history_index = max(mask.history_index for mask in masks)
    return [mask for mask in masks if mask.history_index == final_history_index]


def _history_xml(entry: HistoryEntry, index: int, default_blend: bytes) -> str:
    blend = default_blend if entry.blend_params is None else entry.blend_params
    _require_length(blend, BLEND_BLOB_LENGTH, f"history[{index}] blend params")
    return f'''     <rdf:li
      darktable:num="{index}"
      darktable:operation="{html.escape(entry.operation, quote=True)}"
      darktable:enabled="{int(entry.enabled)}"
      darktable:modversion="{int(entry.module_version)}"
      darktable:params="{bytes(entry.params).hex()}"
      darktable:multi_name="{html.escape(entry.name, quote=True)}"
      darktable:multi_name_hand_edited="{1 if entry.name else 0}"
      darktable:multi_priority="{int(entry.priority)}"
      darktable:blendop_version="{int(entry.blendop_version)}"
      darktable:blendop_params="{bytes(blend).hex()}"/>'''


def _mask_xml(mask: MaskEntry) -> str:
    return f'''     <rdf:li
      darktable:mask_num="{int(mask.history_index)}"
      darktable:mask_id="{int(mask.mask_id)}"
      darktable:mask_type="{int(mask.mask_type)}"
      darktable:mask_name="{html.escape(mask.name, quote=True)}"
      darktable:mask_version="{int(mask.mask_version)}"
      darktable:mask_points="{bytes(mask.points).hex()}"
      darktable:mask_nb="{int(mask.point_count)}"
      darktable:mask_src="0000000000000000"/>'''


def write_xmp(
    output: str | Path,
    *,
    source_name: str,
    entries: Sequence[HistoryEntry],
    default_blend: bytes,
    masks: Sequence[MaskEntry] = (),
    darktable_version: str | None = None,
) -> Path:
    """Write a complete XMP using the covered byte-valued entries."""
    if darktable_version is None:
        darktable_version = detect_darktable_version()
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", darktable_version):
        raise ValueError(
            f"darktable_version must be a semantic version for XMP provenance, got {darktable_version!r}"
        )
    _require_length(default_blend, BLEND_BLOB_LENGTH, "default blend params")
    operation_counts: dict[str, int] = {}
    for entry in entries:
        operation_counts[entry.operation] = operation_counts.get(entry.operation, 0) + 1
    duplicate_operations = sorted(
        operation for operation, count in operation_counts.items() if count > 1
    )
    if duplicate_operations:
        raise ValueError(
            "duplicate operations are not supported by this writer because it does "
            "not emit a verified complete multi-instance iop-order; merge the move "
            "into one module instance or add and render-verify that encoding first: "
            + ", ".join(duplicate_operations)
        )
    for mask in masks:
        if not 0 <= mask.history_index < len(entries):
            raise ValueError(
                f"mask {mask.mask_id} points to history index {mask.history_index}, "
                f"but there are {len(entries)} entries"
            )
    ids_by_snapshot: dict[int, list[int]] = {}
    for mask in masks:
        ids_by_snapshot.setdefault(mask.history_index, []).append(mask.mask_id)
    duplicates = {
        history_index: sorted(
            mask_id for mask_id in set(ids) if ids.count(mask_id) > 1
        )
        for history_index, ids in ids_by_snapshot.items()
        if len(ids) != len(set(ids))
    }
    if duplicates:
        raise ValueError(
            "mask IDs must be unique within each forms snapshot: "
            + "; ".join(
                f"history {index}: {', '.join(map(str, ids))}"
                for index, ids in sorted(duplicates.items())
            )
        )

    masks_block = ""
    if masks:
        masks_block = (
            "\n   <darktable:masks_history>\n    <rdf:Seq>\n"
            + "\n".join(_mask_xml(mask) for mask in masks)
            + "\n    </rdf:Seq>\n   </darktable:masks_history>"
        )
    history = "\n".join(
        _history_xml(entry, index, default_blend) for index, entry in enumerate(entries)
    )
    source = html.escape(source_name, quote=True)
    document = f'''<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="darktable {darktable_version}">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/"
    xmlns:darktable="http://darktable.sf.net/"
    xmp:Rating="1"
    xmpMM:DerivedFrom="{source}"
    darktable:xmp_version="{XMP_VERSION}"
    darktable:auto_presets_applied="1"
    darktable:history_end="{len(entries)}"
    darktable:iop_order_version="{IOP_ORDER_VERSION}">{masks_block}
   <darktable:history>
    <rdf:Seq>
{history}
    </rdf:Seq>
   </darktable:history>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
'''
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document, encoding="utf-8")
    return target


def _self_test() -> None:
    white_balance = pack_white_balance_v4(2.0, 1.0, 1.5, 1.0)
    assert len(white_balance) == 20
    decoded_white_balance = unpack_white_balance_v4(white_balance)
    assert decoded_white_balance["preset"] == WHITE_BALANCE_PRESETS["user"]
    assert math.isclose(decoded_white_balance["red"], 2.0)
    relative_white_balance = relative_white_balance_coefficients(
        (2.0, 1.0, 1.5, 1.0), warmth_ev=0.1, tint_ev=-0.05
    )
    assert relative_white_balance[0] > 2.0
    assert relative_white_balance[2] < 1.5
    assert relative_white_balance[1] == 1.0
    nan_white_balance = unpack_white_balance_v4(
        pack_white_balance_v4(2.0, 1.0, 1.5, math.nan)
    )
    assert math.isnan(nan_white_balance["fourth"])
    nan_relative = relative_white_balance_coefficients(
        (2.0, 1.0, 1.5, math.nan), warmth_ev=0.05, tint_ev=0.02
    )
    assert math.isnan(nan_relative[3])

    daylight = daylight_xy(6500.0)
    assert math.isclose(daylight[0], 0.31278, abs_tol=1e-4)
    assert math.isclose(daylight[1], 0.32918, abs_tol=1e-4)
    color_calibration = pack_color_calibration_v3(
        illuminant="daylight", temperature_kelvin=6500.0, adaptation="cat16"
    )
    assert len(color_calibration) == 160
    decoded_color_calibration = unpack_color_calibration_v3(color_calibration)
    assert decoded_color_calibration["illuminant"] == COLOR_CALIBRATION_ILLUMINANTS["daylight"]
    assert decoded_color_calibration["adaptation"] == COLOR_CALIBRATION_ADAPTATIONS["cat16"]
    assert decoded_color_calibration["algorithm_version"] == 2
    assert math.isclose(decoded_color_calibration["x"], daylight[0], abs_tol=1e-6)
    assert math.isclose(decoded_color_calibration["y"], daylight[1], abs_tol=1e-6)
    neutral_calibration = unpack_color_calibration_v3(pack_color_calibration_v3())
    assert neutral_calibration["illuminant"] == COLOR_CALIBRATION_ILLUMINANTS["pipeline"]
    assert neutral_calibration["adaptation"] == COLOR_CALIBRATION_ADAPTATIONS["none"]
    assert neutral_calibration["red"][:3] == (1.0, 0.0, 0.0)
    inherited_calibration = unpack_color_calibration_v3(
        repack_color_calibration_v3(
            color_calibration,
            matrix=(1.05, -0.02, -0.03, 0.0, 1.0, 0.0, 0.0, -0.01, 1.01),
        )
    )
    assert inherited_calibration["illuminant"] == decoded_color_calibration["illuminant"]
    assert inherited_calibration["adaptation"] == decoded_color_calibration["adaptation"]
    assert inherited_calibration["x"] == decoded_color_calibration["x"]
    assert inherited_calibration["y"] == decoded_color_calibration["y"]
    assert math.isclose(inherited_calibration["red"][0], 1.05, abs_tol=1e-6)
    changed_illuminant = unpack_color_calibration_v3(
        repack_color_calibration_v3(color_calibration, illuminant="pipeline")
    )
    assert changed_illuminant["adaptation"] == COLOR_CALIBRATION_ADAPTATIONS["none"]
    changed_illuminant = unpack_color_calibration_v3(
        repack_color_calibration_v3(
            pack_color_calibration_v3(), illuminant="daylight",
            temperature_kelvin=6500.0,
        )
    )
    assert changed_illuminant["adaptation"] == COLOR_CALIBRATION_ADAPTATIONS["cat16"]
    fluorescent_calibration = unpack_color_calibration_v3(
        pack_color_calibration_v3(illuminant="fluorescent", fluorescent="f7")
    )
    assert fluorescent_calibration["fluorescent"] == 6
    assert math.isclose(
        fluorescent_calibration["x"],
        COLOR_CALIBRATION_FLUORESCENT_XY[6][0], abs_tol=1e-6,
    )
    custom_calibration = unpack_color_calibration_v3(
        pack_color_calibration_v3(illuminant="custom", x=0.31, y=0.33)
    )
    assert math.isclose(custom_calibration["x"], 0.31, abs_tol=1e-6)
    try:
        pack_color_calibration_v3(illuminant="camera")
    except ValueError:
        pass
    else:
        raise AssertionError("source-dependent camera illuminant was synthesized")

    exposure = pack_exposure_v6(0.1, black=0.01)
    assert len(exposure) == 24
    assert math.isclose(unpack_exposure_v6(exposure)["exposure"], 0.1, rel_tol=1e-6)
    tone_equalizer = pack_tone_equalizer_v2(tone_equalizer_bands(shadows=0.25))
    assert len(tone_equalizer) == 72
    assert math.isclose(unpack_tone_equalizer_v2(tone_equalizer)["bands"][4], 0.25)
    assert tone_equalizer_bands(shadows=0.25)[4] == 0.25
    assert len(pack_rgb_curve_v1([(0.0, 0.0), (1.0, 1.0)])) == 516
    independent_curve = unpack_rgb_curve_v1(pack_rgb_curve_v1(
        red=[(0.0, 0.0), (0.5, 0.55), (1.0, 1.0)],
        green=[(0.0, 0.0), (1.0, 1.0)],
        blue=[(0.0, 0.0), (0.5, 0.45), (1.0, 1.0)],
    ))
    assert independent_curve["autoscale"] == 1
    assert independent_curve["red"][1][1] > independent_curve["blue"][1][1]
    diffuse = pack_diffuse_v2(
        iterations=4, radius=32, regularization=1.0, speed=(0.1, 0.0, -0.1, 0.0)
    )
    assert len(diffuse) == 60 and unpack_diffuse_v2(diffuse)["iterations"] == 4
    haze = pack_haze_removal_v3(0.1, distance=0.3)
    assert len(haze) == 16
    assert math.isclose(unpack_haze_removal_v3(haze)["distance"], 0.3, rel_tol=1e-6)
    crop = pack_crop_v3(0.05, 0.04, 0.95, 0.96)
    assert len(crop) == 24 and unpack_crop_v3(crop)["ratio_n"] == -1
    perspective = pack_perspective_v5(
        rotation=0.5, lensshift_v=0.01, drawn_lines=[(0.1, 0.2, 0.3, 0.4)]
    )
    assert len(perspective) == 892
    decoded_perspective = unpack_perspective_v5(perspective)
    assert len(decoded_perspective["drawn_lines"]) == 1
    denoise = pack_denoise_profile_v12(strength=1.1)
    assert len(denoise) == 416
    assert unpack_denoise_profile_v12(denoise)["noise_a"][0] == -1.0
    assert math.isclose(
        unpack_denoise_profile_v12(
            repack_denoise_profile_v12(denoise, strength=0.8)
        )["strength"], 0.8, abs_tol=1e-6
    )
    color_equalizer = pack_color_equalizer_v4()
    assert len(color_equalizer) == 128
    assert unpack_color_equalizer_v4(color_equalizer)["saturation"] == [1.0] * 8
    assert color_equalizer_values(1.0, {"blue": 1.2})[5] == 1.2
    color_balance = pack_color_balance_rgb_v5(overrides={"vibrance": 0.1})
    assert len(color_balance) == 132
    assert len(unpack_color_balance_rgb_v5(color_balance)["params"]) == 32
    basic = pack_basic_adjustments_v2(brightness=0.1, contrast=0.2)
    assert len(basic) == 44
    decoded_basic = unpack_basic_adjustments_v2(basic)
    assert math.isclose(decoded_basic["brightness"], 0.1, rel_tol=1e-6)
    assert unpack_flip_v2(pack_flip_v2(5, mapping_verified=True))["raw_enum"] == 5
    assert len(pack_ellipse_mask_v6(0.5, 0.5, 0.1, 0.1)) == 28
    assert len(pack_gradient_mask_v6(0.5, 0.5)) == 28
    path = pack_path_mask_v6([
        {"corner": (0.2, 0.2)}, {"corner": (0.8, 0.2)},
        {"corner": (0.5, 0.8)},
    ])
    brush = pack_brush_mask_v6([
        {"corner": (0.2, 0.5)}, {"corner": (0.8, 0.5)},
    ])
    assert len(path) == 3 * 36 and len(brush) == 2 * 44
    group = pack_mask_group_v6(
        [1001, 1002], 1099, opacities=[0.4, 0.8], states=[3, 11]
    )
    assert len(group) == 32
    first = struct.unpack_from("<iiif", group, 0)
    second = struct.unpack_from("<iiif", group, 16)
    assert first[:3] == (1001, 1099, 3) and math.isclose(
        first[3], 0.4, abs_tol=1e-6
    )
    assert second[:3] == (1002, 1099, 11) and math.isclose(
        second[3], 0.8, abs_tol=1e-6
    )

    neutral_blend = bytes(BLEND_BLOB_LENGTH)
    source_neutral_blend = neutral_blend_v14()
    assert len(source_neutral_blend) == BLEND_BLOB_LENGTH
    assert struct.unpack_from("<I", source_neutral_blend, 0)[0] == 0
    assert struct.unpack_from("<i", source_neutral_blend, 408)[0] == 0
    assert struct.unpack_from("<i", source_neutral_blend, 412)[0] == -1
    masked_blend = make_mask_blend(
        neutral_blend, blend_colorspace=BLEND_CS_RGB_DISPLAY, mask_id=1099,
        parametric={"lightness_in": (0.1, 0.2, 0.8, 0.9)}, opacity=75.0,
    )
    assert struct.unpack_from("<I", masked_blend, 0)[0] == 7
    assert struct.unpack_from("<I", masked_blend, 24)[0] == 1099
    assert struct.unpack_from("<I", masked_blend, 28)[0] == 1 << 10
    assert struct.unpack_from("<4f", masked_blend, BLEND_PARAMS_OFFSET + 10 * 16) == (
        0.10000000149011612, 0.20000000298023224,
        0.800000011920929, 0.8999999761581421,
    )
    with tempfile.TemporaryDirectory() as directory:
        try:
            write_xmp(
                Path(directory) / "duplicate.xmp",
                source_name="photo.jpg",
                entries=[
                    HistoryEntry("basicadj", 2, pack_basic_adjustments_v2()),
                    HistoryEntry("basicadj", 2, pack_basic_adjustments_v2()),
                ],
                default_blend=neutral_blend,
                darktable_version=TEST_PROVENANCE_DARKTABLE,
            )
        except ValueError as exc:
            assert "duplicate operations" in str(exc)
        else:
            raise AssertionError("duplicate operation was not rejected")
        cross_runtime = Path(directory) / "cross-runtime.xmp"
        write_xmp(
            cross_runtime, source_name="photo.jpg", entries=[],
            default_blend=neutral_blend, darktable_version="9.9.9",
        )
        assert 'xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="darktable 9.9.9"' in (
            cross_runtime.read_text(encoding="utf-8")
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--check-version", action="store_true")
    args = parser.parse_args()
    if not args.self_test and not args.check_version:
        parser.error("choose --self-test or --check-version")
    if args.self_test:
        _self_test()
        print("photo_xmp.xmp packer self-test passed")
    if args.check_version:
        print(check_darktable_version())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
