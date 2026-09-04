#!/usr/bin/env python3
"""Agent-facing CLI for building, inspecting, validating, and rendering darktable XMP."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Sequence

from .xmp import (
    BLEND_BLOB_LENGTH, BLEND_CHANNELS, BLENDOP_VERSION, BLEND_CS_LAB, BLEND_CS_RAW,
    BLEND_CS_RGB_DISPLAY, BLEND_CS_RGB_SCENE, HistoryEntry, MASK_BRUSH,
    MASK_ELLIPSE, MASK_GRADIENT, MASK_GROUP, MASK_OBJECT, MASK_PATH, MaskEntry,
    COLOR_BALANCE_RGB_FIELDS, COLOR_CALIBRATION_ADAPTATIONS,
    COLOR_CALIBRATION_FLUORESCENTS, COLOR_CALIBRATION_ILLUMINANTS,
    COLOR_CALIBRATION_LEDS, COLOR_EQUALIZER_HUES, TONE_EQUALIZER_BANDS,
    WHITE_BALANCE_PRESETS, RENDER_TESTED_DARKTABLE,
    check_darktable_version, color_equalizer_values, current_mask_snapshot, list_presets,
    load_blend_template, load_history_entries, load_mask_entries, load_preset_entry,
    make_mask_blend, neutral_blend_v14, pack_basic_adjustments_v2, pack_brush_mask_v6,
    pack_color_balance_rgb_v5, pack_color_calibration_v3, pack_color_equalizer_v4,
    pack_crop_v3, pack_denoise_profile_v12, pack_diffuse_v2, pack_exposure_v6,
    pack_flip_v2, pack_gradient_mask_v6, pack_haze_removal_v3,
    pack_mask_group_v6, pack_path_mask_v6, pack_perspective_v5,
    pack_ellipse_mask_v6, pack_rgb_curve_v1, pack_tone_equalizer_v2,
    pack_white_balance_v4, read_xmp, relative_white_balance_coefficients,
    resolve_darktable_executable,
    repack_color_calibration_v3, repack_denoise_profile_v12,
    tone_equalizer_bands, unpack_basic_adjustments_v2,
    unpack_color_balance_rgb_v5, unpack_color_calibration_v3,
    unpack_color_equalizer_v4, unpack_crop_v3, unpack_denoise_profile_v12,
    unpack_diffuse_v2, unpack_exposure_v6, unpack_flip_v2,
    unpack_haze_removal_v3, unpack_perspective_v5, unpack_rgb_curve_v1,
    unpack_tone_equalizer_v2, unpack_white_balance_v4, write_xmp,
)

RECIPE_VERSION = 1
BASE_OVERRIDE_OPERATIONS = {
    "temperature", "channelmixerrgb", "exposure", "toneequal",
    "rgbcurve", "colorequal", "colorbalancergb", "basicadj",
    "flip", "denoiseprofile", "diffuse", "hazeremoval", "crop",
    "ashift",
}
MASKABLE_OPERATIONS = {
    "exposure", "toneequal", "rgbcurve", "colorequal",
    "colorbalancergb", "basicadj", "denoiseprofile", "diffuse",
    "hazeremoval",
}
DEFAULT_MASK_COLORSPACE = {
    "basicadj": "rgb_display",
    "denoiseprofile": "rgb_scene",
    "exposure": "rgb_scene",
    "toneequal": "rgb_scene",
    "rgbcurve": "rgb_scene",
    "colorequal": "rgb_scene",
    "colorbalancergb": "rgb_scene",
    "diffuse": "rgb_scene",
    "hazeremoval": "rgb_scene",
}
MODULE_SPECS = {
    "temperature": (4, 20), "channelmixerrgb": (3, 160),
    "exposure": (6, 24), "toneequal": (2, 72), "rgbcurve": (1, 516),
    "colorequal": (4, 128), "colorbalancergb": (5, 132),
    "basicadj": (2, 44), "flip": (2, 4), "denoiseprofile": (12, 416),
    "diffuse": (2, 60), "hazeremoval": (3, 16), "crop": (3, 24),
    "ashift": (5, 892),
}
MODULE_ALIASES = {
    "white_balance": "temperature", "color_calibration": "channelmixerrgb",
    "tone_equalizer": "toneequal", "rgb_curve": "rgbcurve",
    "color_equalizer": "colorequal", "color_balance_rgb": "colorbalancergb",
    "basic_adjustments": "basicadj", "denoise": "denoiseprofile",
    "diffuse_or_sharpen": "diffuse", "haze_removal": "hazeremoval",
    "perspective": "ashift",
}
COLORSPACES = {
    "raw": BLEND_CS_RAW, "lab": BLEND_CS_LAB,
    "rgb_display": BLEND_CS_RGB_DISPLAY, "rgb_scene": BLEND_CS_RGB_SCENE,
}
MASK_COMBINE_MODES = {
    "exclusive": 0,
    "exclusive-inverted": 1,
    "inclusive": 2,
    "inclusive-inverted": 3,
}
MASK_TYPES = {
    "path": MASK_PATH, "group": MASK_GROUP, "gradient": MASK_GRADIENT,
    "ellipse": MASK_ELLIPSE, "brush": MASK_BRUSH, "object": MASK_OBJECT,
}
MASK_POINT_BYTES = {
    MASK_PATH: 36, MASK_GROUP: 16, MASK_GRADIENT: 28,
    MASK_ELLIPSE: 28, MASK_BRUSH: 44, MASK_OBJECT: 12,
}
def _json_dump(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


def _command_status(command: str, role: str, *, required: bool = False) -> dict[str, object]:
    path = shutil.which(command)
    return {
        "available": path is not None,
        "command": command,
        "path": path,
        "required_for_cli": required,
        "role": role,
    }


def companion_tool_status() -> dict[str, object]:
    """Report common photo-workflow tools without making them CLI blockers."""
    tools = {
        "uv": _command_status(
            "uv", "isolated execution of skill helpers and their declared dependencies"
        ),
        "exiftool": _command_status(
            "exiftool", "source metadata, orientation, and provenance inspection"
        ),
        "imagemagick": _command_status(
            "magick", "contact sheets, overlays, and auxiliary image inspection"
        ),
        "fontconfig": _command_status(
            "fc-match", "portable font discovery for annotated review artifacts"
        ),
        "gmic": _command_status(
            "gmic", "optional external image operations when a workflow explicitly selects them"
        ),
    }

    fc_list = shutil.which("fc-list")
    cjk_font: dict[str, object] = {
        "available": False,
        "sample": None,
        "role": "CJK-capable text in review boards and annotations",
    }
    if fc_list is not None:
        try:
            completed = subprocess.run(
                [fc_list, ":lang=zh-cn", "family", "file"],
                text=True, capture_output=True, timeout=5, check=False,
            )
            sample = next(
                (line.strip() for line in completed.stdout.splitlines() if line.strip()),
                None,
            )
            cjk_font.update({"available": sample is not None, "sample": sample})
        except (OSError, subprocess.SubprocessError):
            pass
    tools["cjk_font"] = cjk_font

    magick = shutil.which("magick")
    registry: dict[str, object] = {
        "available": False,
        "font_count": 0,
        "cjk_aliases": [],
        "role": "ImageMagick named-font lookup for portable annotation commands",
    }
    if magick is not None:
        try:
            completed = subprocess.run(
                [magick, "-list", "font"], text=True, capture_output=True,
                timeout=10, check=False,
            )
            names = [
                line.split(":", 1)[1].strip()
                for line in completed.stdout.splitlines()
                if line.lstrip().startswith("Font:")
            ]
            cjk_markers = (
                "cjk", "source-han", "sourcehan", "noto-sans-sc",
                "pingfang", "hiragino", "wenquanyi", "arial-unicode",
            )
            aliases = [
                name for name in names
                if any(marker in name.lower() for marker in cjk_markers)
            ]
            registry.update({
                "available": bool(names),
                "font_count": len(names),
                "cjk_aliases": aliases[:12],
            })
        except (OSError, subprocess.SubprocessError):
            pass
    tools["imagemagick_font_registry"] = registry
    return tools


def doctor(
    executable: str = "darktable-cli", config_dir: Path | None = None,
    *, require_render: bool = False, require_native_ai: bool = False,
) -> dict[str, object]:
    workflow_tools = companion_tool_status()
    try:
        resolved_executable = resolve_darktable_executable(executable)
        version = check_darktable_version(str(resolved_executable))
        completed = subprocess.run(
            [str(resolved_executable), "--version"], text=True, capture_output=True, check=True
        )
        output = completed.stdout + completed.stderr
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        unmet = ["darktable runtime is unavailable"]
        return {
            "status": "failed", "darktable_cli": executable,
            "core_status": "unavailable",
            "render_status": "unavailable",
            "native_ai_status": "unavailable",
            "workflow_tools_status": "partial",
            "error": str(exc),
            "workflow_tools": workflow_tools,
            "requirements": {
                "require_render": require_render,
                "require_native_ai": require_native_ai,
                "met": False,
                "unmet": unmet,
            },
        }
    resolved_cli = str(resolved_executable)
    candidates = [Path(resolved_cli)]
    if sys.platform == "darwin":
        candidates.insert(0, Path("/Applications/darktable.app/Contents/MacOS/darktable"))
    elif sys.platform == "win32":
        candidates.insert(0, Path(resolved_cli).with_name("darktable.exe"))
    full_output = output
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            detailed = subprocess.run(
                [str(candidate), "--version"], text=True, capture_output=True
            )
            candidate_output = detailed.stdout + detailed.stderr
            if "Compile options:" in candidate_output:
                full_output = candidate_output
                break
        except (OSError, subprocess.CalledProcessError):
            continue
    ai_support: bool | None = None
    ai_lines = [line.strip() for line in full_output.splitlines() if line.strip().startswith("AI")]
    if any("-> ENABLED" in line for line in ai_lines):
        ai_support = True
    elif any("-> DISABLED" in line for line in ai_lines):
        ai_support = False
    command = [
        sys.executable, "-m", "photo_xmp.subject_mask", "--doctor",
        "--darktable-cli", str(resolved_executable),
    ]
    if config_dir is not None:
        command.extend(["--config-dir", str(config_dir)])
    native = subprocess.run(command, text=True, capture_output=True)
    try:
        native_mask = json.loads(native.stdout)
    except json.JSONDecodeError:
        native_mask = {
            "status": "unavailable", "available": False,
            "reasons": [
                native.stderr.strip() or native.stdout.strip()
                or "native mask doctor failed"
            ],
        }
    render_tested = version in set(RENDER_TESTED_DARKTABLE)
    native_available = bool(native_mask.get("available"))
    workflow_values = [
        value for value in workflow_tools.values() if isinstance(value, dict)
    ]
    workflow_ok = all(bool(value.get("available")) for value in workflow_values)
    unmet: list[str] = []
    if require_render and not render_tested:
        unmet.append("render runtime is not in the tested darktable set")
    if require_native_ai and not native_available:
        unmet.append("native AI subject masking is unavailable")
    status = "ok" if render_tested and native_available else "degraded"
    if unmet:
        status = "failed"
    return {
        "status": status, "darktable_cli": str(resolved_executable),
        "core_status": "ok",
        "render_status": "ok" if render_tested else "untested",
        "native_ai_status": "ok" if native_available else "unavailable",
        "workflow_tools_status": "ok" if workflow_ok else "partial",
        "darktable_version": version,
        "render_tested_runtime": render_tested,
        "ai_support": ai_support,
        "native_subject_mask": native_mask,
        "workflow_tools": workflow_tools,
        "requirements": {
            "require_render": require_render,
            "require_native_ai": require_native_ai,
            "met": not unmet,
            "unmet": unmet,
        },
        "notes": [
            "Run a fresh-config render for every final XMP.",
            "AI object segmentation requires reviewed foreground/background prompts.",
            "Workflow tools are recommendations for analysis and review artifacts; "
            "their absence does not disable core XMP construction.",
        ],
    }


def _resolve(base: Path, value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _require_file(path: Path | None, label: str) -> Path | None:
    if path is not None and not path.is_file():
        raise ValueError(f"{label} does not exist or is not a file: {path}")
    return path


def _operation(value: str) -> str:
    return MODULE_ALIASES.get(value, value)


def _latest(entries: Sequence[HistoryEntry], operation: str) -> HistoryEntry:
    matching = [entry for entry in entries if entry.operation == operation]
    if not matching:
        raise ValueError(f"no inherited {operation!r} module is available")
    base = [entry for entry in matching if entry.priority == 0]
    if base:
        return base[-1]
    raise ValueError(
        f"inherited {operation!r} has no priority-0 base instance; this CLI "
        "does not synthesize custom multi-instance iop order"
    )


def _collapse_history(entries: Sequence[HistoryEntry]) -> list[HistoryEntry]:
    """Keep the latest base edit for each operation; reject real multi-instances."""
    order: list[str] = []
    grouped: dict[str, list[HistoryEntry]] = {}
    for entry in entries:
        if entry.operation not in grouped:
            order.append(entry.operation)
            grouped[entry.operation] = []
        grouped[entry.operation].append(entry)
    result = []
    for operation in order:
        candidates = grouped[operation]
        priorities = {entry.priority for entry in candidates}
        if len(priorities) > 1 or (priorities and priorities != {0}):
            raise ValueError(
                f"operation {operation!r} contains real multi-instances; this CLI "
                "does not synthesize their custom iop order"
            )
        result.append(candidates[-1])
    return result


def _pack_operation(
    operation: str, params: Mapping[str, object], base: HistoryEntry | None
) -> tuple[int, bytes]:
    operation = _operation(operation)
    if base is not None and operation not in BASE_OVERRIDE_OPERATIONS:
        raise ValueError(
            f"{operation!r} cannot apply partial overrides to inherited/preset "
            "parameters; omit params to preserve it exactly, or use source=create "
            "to author the complete supported recipe state"
        )
    values = dict(params)
    if operation == "temperature":
        if base is not None:
            if base.module_version != 4:
                raise ValueError("inherited temperature is not v4")
            decoded = unpack_white_balance_v4(base.params)
            warmth = float(values.pop("warmth_ev", 0.0))
            tint = float(values.pop("tint_ev", 0.0))
            if values:
                raise ValueError("inherited temperature accepts only warmth_ev and tint_ev")
            coeffs = relative_white_balance_coefficients(
                (decoded["red"], decoded["green"], decoded["blue"], decoded["fourth"]),
                warmth_ev=warmth, tint_ev=tint,
            )
            return 4, pack_white_balance_v4(*coeffs, preset=decoded["preset"])
        coeffs = values.pop("coefficients", None)
        if coeffs is None or len(coeffs) != 4:
            raise ValueError("new temperature needs coefficients=[R,G,B,fourth]")
        return 4, pack_white_balance_v4(*coeffs, **values)
    if operation == "channelmixerrgb":
        if base is not None:
            if base.module_version != 3:
                raise ValueError("inherited channelmixerrgb is not v3")
            return 3, repack_color_calibration_v3(base.params, **values)
        matrix = values.pop("matrix", None)
        if matrix is not None:
            if len(matrix) != 9:
                raise ValueError("Color Calibration matrix needs nine values")
            values.update(red=matrix[:3], green=matrix[3:6], blue=matrix[6:])
        custom_xy = values.pop("custom_xy", None)
        if custom_xy is not None:
            values.update(x=custom_xy[0], y=custom_xy[1])
        return 3, pack_color_calibration_v3(**values)
    if operation == "exposure":
        if base is not None:
            if base.module_version != 6:
                raise ValueError("inherited/preset exposure is not v6")
            decoded = unpack_exposure_v6(base.params)
            decoded.update(values)
            values = decoded
        return 6, pack_exposure_v6(**values)
    if operation == "toneequal":
        if base is not None:
            if base.module_version != 2:
                raise ValueError("inherited/preset toneequal is not v2")
            decoded = unpack_tone_equalizer_v2(base.params)
            if "named_bands" in values:
                bands = list(decoded["bands"])
                indexes = {name: index for index, name in enumerate(TONE_EQUALIZER_BANDS)}
                unknown = set(values["named_bands"]) - set(indexes)
                if unknown:
                    raise ValueError(
                        "unknown tone equalizer bands: " + ", ".join(sorted(unknown))
                    )
                for name, value in values.pop("named_bands").items():
                    bands[indexes[name]] = float(value)
                values["bands"] = bands
            decoded.update(values)
            values = decoded
        bands = values.pop("bands", None)
        named = values.pop("named_bands", None)
        if bands is not None and named is not None:
            raise ValueError("toneequal accepts bands or named_bands, not both")
        if named is not None:
            bands = tone_equalizer_bands(**named)
        return 2, pack_tone_equalizer_v2([0.0] * 9 if bands is None else bands, **values)
    if operation == "rgbcurve":
        if base is not None:
            if base.module_version != 1:
                raise ValueError("inherited/preset rgbcurve is not v1")
            decoded = unpack_rgb_curve_v1(base.params)
            if "nodes" in values:
                decoded.pop("red", None)
                decoded.pop("green", None)
                decoded.pop("blue", None)
            elif not any(name in values for name in ("red", "green", "blue")) and (
                decoded["red"] == decoded["green"] == decoded["blue"]
                and decoded["autoscale"] == 0
            ):
                decoded["nodes"] = decoded.pop("red")
                decoded.pop("green")
                decoded.pop("blue")
            decoded.update(values)
            values = decoded
        return 1, pack_rgb_curve_v1(**values)
    if operation == "colorequal":
        decoded = None
        if base is not None:
            if base.module_version != 4:
                raise ValueError("inherited/preset colorequal is not v4")
            decoded = unpack_color_equalizer_v4(base.params)
        for field, neutral in (("saturation", 1.0), ("hue", 0.0), ("brightness", 1.0)):
            value = values.get(field)
            if isinstance(value, dict):
                if decoded is None:
                    values[field] = color_equalizer_values(neutral, value)
                else:
                    channel_values = list(decoded[field])
                    indexes = {name: index for index, name in enumerate(COLOR_EQUALIZER_HUES)}
                    for name, override in value.items():
                        channel_values[indexes[name]] = float(override)
                    values[field] = channel_values
        if decoded is not None:
            decoded.update(values)
            values = decoded
        return 4, pack_color_equalizer_v4(**values)
    if operation == "colorbalancergb":
        if base is not None:
            if base.module_version != 5:
                raise ValueError("inherited/preset colorbalancergb is not v5")
            decoded = unpack_color_balance_rgb_v5(base.params)
            overrides = values.pop("overrides", None)
            decoded.update(values)
            if overrides is not None:
                decoded["overrides"] = overrides
            values = decoded
        return 5, pack_color_balance_rgb_v5(**values)
    if operation == "basicadj":
        if base is not None:
            if base.module_version != 2:
                raise ValueError("inherited/preset basicadj is not v2")
            decoded = unpack_basic_adjustments_v2(base.params)
            decoded.update(values)
            values = decoded
        return 2, pack_basic_adjustments_v2(**values)
    if operation == "flip":
        if base is not None:
            if base.module_version != 2:
                raise ValueError("inherited/preset flip is not v2")
            decoded = unpack_flip_v2(base.params)
            decoded.update(values)
            values = decoded
        return 2, pack_flip_v2(mapping_verified=True, **values)
    if operation == "diffuse":
        if base is not None:
            if base.module_version != 2:
                raise ValueError("inherited diffuse is not v2")
            decoded = unpack_diffuse_v2(base.params)
            decoded.update(values)
            values = decoded
        return 2, pack_diffuse_v2(**values)
    if operation == "hazeremoval":
        if base is not None:
            if base.module_version != 3:
                raise ValueError("inherited/preset hazeremoval is not v3")
            decoded = unpack_haze_removal_v3(base.params)
            decoded.update(values)
            values = decoded
        return 3, pack_haze_removal_v3(**values)
    if operation == "crop":
        if base is not None:
            if base.module_version != 3:
                raise ValueError("inherited/preset crop is not v3")
            decoded = unpack_crop_v3(base.params)
            decoded.update(values)
            values = decoded
        return 3, pack_crop_v3(**values)
    if operation == "ashift":
        if base is not None:
            if base.module_version != 5:
                raise ValueError("inherited/preset ashift is not v5")
            decoded = unpack_perspective_v5(base.params)
            decoded.update(values)
            values = decoded
        return 5, pack_perspective_v5(**values)
    if operation == "denoiseprofile":
        if base is not None:
            if base.module_version != 12:
                raise ValueError("inherited/preset denoiseprofile is not v12")
            return 12, repack_denoise_profile_v12(base.params, **values)
        return 12, pack_denoise_profile_v12(**values)
    raise ValueError(
        f"operation {operation!r} is not directly encoded; use source=preset/inherit "
        "or extend photo_xmp.xmp for its concrete version"
    )


def _pack_mask(definition: Mapping[str, object]) -> tuple[int, bytes, int, list[int]]:
    kind = str(definition["type"])
    params = dict(definition.get("params", {}))
    if kind == "ellipse":
        points = pack_ellipse_mask_v6(**params)
        return MASK_ELLIPSE, points, 1, []
    if kind == "gradient":
        points = pack_gradient_mask_v6(**params)
        return MASK_GRADIENT, points, 1, []
    if kind == "path":
        source = definition.get("points", params.pop("points", None))
        points = pack_path_mask_v6(source)
        return MASK_PATH, points, len(source), []
    if kind == "brush":
        source = definition.get("points", params.pop("points", None))
        points = pack_brush_mask_v6(source)
        return MASK_BRUSH, points, len(source), []
    if kind == "group":
        children = [int(value) for value in definition["children"]]
        points = pack_mask_group_v6(
            children, int(definition["id"]),
            opacities=definition.get("opacities"), states=definition.get("states"),
            opacity=float(definition.get("opacity", 1.0)),
        )
        return MASK_GROUP, points, len(children), children
    if kind in {"ai", "object", "person", "subject"}:
        raise ValueError(
            "AI prompts are runtime inference, not static recipe shapes; use "
            "photo-xmp mask subject and apply its reviewed JSON with --subject-mask"
        )
    raise ValueError(f"unknown mask type {kind!r}")


def _baseline(recipe: Mapping[str, object], base_dir: Path):
    config = dict(recipe.get("baseline", {}))
    xmp_path = _require_file(_resolve(base_dir, config.get("xmp")), "baseline.xmp")
    library_db = _require_file(
        _resolve(base_dir, config.get("library_db")), "baseline.library_db"
    )
    data_db = _require_file(
        _resolve(base_dir, config.get("data_db")), "baseline.data_db"
    )
    imgid = config.get("imgid")
    all_entries: list[HistoryEntry] = []
    all_masks: list[MaskEntry] = []
    metadata: dict[str, object] = {}
    if xmp_path is not None:
        all_entries, all_masks, metadata = read_xmp(xmp_path)
        all_masks = current_mask_snapshot(all_masks)
    elif library_db is not None:
        all_entries = load_history_entries(library_db, imgid=imgid)
        if config.get("inherit_masks"):
            all_masks = load_mask_entries(library_db, imgid=imgid)
    requested_inherit = list(config.get("inherit", []))
    inherit_all = "*" in requested_inherit
    inherit = {_operation(str(value)) for value in requested_inherit if value != "*"}
    entries = list(all_entries) if inherit_all else [
        entry for entry in all_entries if entry.operation in inherit
    ]
    entries = _collapse_history(entries)
    masks = all_masks if config.get("inherit_masks") else []
    blend = None
    if library_db is not None or data_db is not None:
        blend = load_blend_template(library_db, imgid=imgid, data_db=data_db)
    else:
        for entry in all_entries:
            if entry.blend_params is not None and len(entry.blend_params) == BLEND_BLOB_LENGTH:
                if int.from_bytes(entry.blend_params[:4], "little") == 0:
                    blend = entry.blend_params
                    break
    if blend is None:
        blend = neutral_blend_v14()
    return entries, masks, all_entries, blend, data_db, metadata


def build_recipe(recipe_path: Path, output_override: Path | None = None) -> dict[str, object]:
    recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
    if not isinstance(recipe, dict):
        raise ValueError("recipe root must be a JSON object")
    recipe_version = int(recipe.get("recipe_version", RECIPE_VERSION))
    if recipe_version != RECIPE_VERSION:
        raise ValueError(
            f"unsupported recipe_version {recipe_version}; this CLI supports {RECIPE_VERSION}"
        )
    base_dir = recipe_path.resolve().parent
    entries, inherited_masks, available, blend, data_db, metadata = _baseline(recipe, base_dir)
    entry_positions = {entry.operation: index for index, entry in enumerate(entries)}
    mask_requests: dict[str, Mapping[str, object]] = {}

    modules = recipe.get("modules", [])
    requested_operations = [_operation(str(module["operation"])) for module in modules]
    if len(requested_operations) != len(set(requested_operations)):
        duplicates = sorted(
            operation for operation in set(requested_operations)
            if requested_operations.count(operation) > 1
        )
        raise ValueError(
            "recipe repeats operations, but this CLI supports one base instance per "
            "operation: " + ", ".join(duplicates)
        )

    for module in modules:
        operation = _operation(str(module["operation"]))
        existing = entries[entry_positions[operation]] if operation in entry_positions else None
        source = str(module.get("source", "create"))
        params = dict(module.get("params", {}))
        inherited = None
        if source == "inherit":
            inherited = _latest(available, operation)
            version, blob = _pack_operation(operation, params, inherited) if params else (
                inherited.module_version, inherited.params
            )
            entry = replace(
                inherited, module_version=version, params=blob,
                enabled=int(module.get("enabled", inherited.enabled)),
                name=str(module.get("name", inherited.name)),
            )
        elif source == "preset":
            if data_db is None:
                raise ValueError(f"module {operation!r} preset source requires baseline.data_db")
            preset = load_preset_entry(
                data_db, operation=operation, name=str(module["preset"])
            )
            version, blob = _pack_operation(operation, params, preset) if params else (
                preset.module_version, preset.params
            )
            entry = replace(
                preset, module_version=version, params=blob,
                enabled=int(module.get("enabled", preset.enabled)),
                name=str(module.get("name", preset.name)),
            )
        elif source == "create":
            version, blob = _pack_operation(operation, params, None)
            entry = HistoryEntry(
                operation, version, blob, enabled=int(module.get("enabled", 1)),
                name=str(module.get("name", "")),
            )
        else:
            raise ValueError(f"unknown module source {source!r}")
        if (
            existing is not None and "mask" not in module
            and bool(module.get("preserve_existing_blend", True))
        ):
            entry = replace(
                entry, blend_params=existing.blend_params,
                blendop_version=existing.blendop_version, priority=existing.priority,
            )
        elif "mask" not in module and not bool(
            module.get("preserve_existing_blend", True)
        ):
            entry = replace(
                entry, blend_params=blend, blendop_version=BLENDOP_VERSION, priority=0
            )
        if operation in entry_positions:
            entries[entry_positions[operation]] = entry
        else:
            entry_positions[operation] = len(entries)
            entries.append(entry)
        if "mask" in module:
            mask_requests[operation] = dict(module["mask"])

    definitions = {int(item["id"]): item for item in recipe.get("masks", [])}
    if len(definitions) != len(recipe.get("masks", [])):
        raise ValueError("recipe mask IDs must be unique")
    if any(mask_id <= 0 for mask_id in definitions):
        raise ValueError("recipe mask IDs must be positive integers")
    packed: dict[int, tuple[int, bytes, int, list[int], str]] = {}
    for mask_id, definition in definitions.items():
        mask_type, points, count, children = _pack_mask(definition)
        packed[mask_id] = (mask_type, points, count, children, str(definition.get("name", "")))

    masks: list[MaskEntry] = []
    claimed: dict[int, str] = {}
    inherited_by_id = {mask.mask_id: mask for mask in inherited_masks}
    reserved_mask_ids = set(definitions) | set(inherited_by_id)
    next_clone_id = max({999, *reserved_mask_ids}) + 1

    def claim(mask_id: int, operation: str, history_index: int) -> None:
        owner = claimed.get(mask_id)
        if owner is not None and owner != operation:
            raise ValueError(f"mask {mask_id} is shared by {owner!r} and {operation!r}")
        if owner is not None:
            return
        try:
            mask_type, points, count, children, name = packed[mask_id]
        except KeyError as exc:
            raise ValueError(f"module {operation!r} references missing mask {mask_id}") from exc
        claimed[mask_id] = operation
        for child in children:
            claim(child, operation, history_index)
        masks.append(MaskEntry(history_index, mask_id, mask_type, name, points, count))

    def clone_inherited_tree(mask_id: int, history_index: int) -> int:
        nonlocal next_clone_id
        try:
            source = inherited_by_id[mask_id]
        except KeyError as exc:
            raise ValueError(f"missing inherited mask {mask_id}") from exc
        while next_clone_id in reserved_mask_ids:
            next_clone_id += 1
        cloned_id = next_clone_id
        reserved_mask_ids.add(cloned_id)
        next_clone_id += 1
        if source.mask_type == MASK_GROUP:
            child_ids: list[int] = []
            states: list[int] = []
            opacities: list[float] = []
            for offset in range(0, len(source.points), 16):
                child, _parent, state, opacity = struct.unpack_from(
                    "<iiif", source.points, offset
                )
                child_ids.append(clone_inherited_tree(child, history_index))
                states.append(state)
                opacities.append(opacity)
            points = pack_mask_group_v6(
                child_ids, cloned_id, states=states, opacities=opacities
            )
            count = len(child_ids)
        else:
            points = source.points
            count = source.point_count
        masks.append(MaskEntry(
            history_index, cloned_id, source.mask_type,
            f"{source.name} (CLI clone)" if source.name else "CLI cloned mask",
            points, count, source.mask_version,
        ))
        return cloned_id

    for operation, mask_config in mask_requests.items():
        index = entry_positions[operation]
        drawn = mask_config.get("drawn")
        inherited_drawn = mask_config.get("inherited_drawn")
        clone_drawn = mask_config.get("clone_inherited_drawn")
        if sum(value is not None for value in (drawn, inherited_drawn, clone_drawn)) > 1:
            raise ValueError(
                f"module {operation!r} accepts only one drawn-mask source"
            )
        if drawn is not None:
            if int(drawn) not in packed:
                raise ValueError(f"module {operation!r} references missing mask {drawn}")
            if packed[int(drawn)][0] != MASK_GROUP:
                raise ValueError(
                    f"module {operation!r} drawn mask must reference a group mask"
                )
            claim(int(drawn), operation, index)
        if inherited_drawn is not None:
            inherited_root = next(
                (mask for mask in inherited_masks if mask.mask_id == int(inherited_drawn)),
                None,
            )
            if inherited_root is None:
                raise ValueError(
                    f"module {operation!r} references missing inherited mask "
                    f"{inherited_drawn}"
                )
            if inherited_root.mask_type != MASK_GROUP:
                raise ValueError(
                    f"module {operation!r} inherited drawn mask must reference a group"
                )
        cloned_root = None
        if clone_drawn is not None:
            inherited_root = inherited_by_id.get(int(clone_drawn))
            if inherited_root is None:
                raise ValueError(
                    f"module {operation!r} references missing inherited mask {clone_drawn}"
                )
            if inherited_root.mask_type != MASK_GROUP:
                raise ValueError(
                    f"module {operation!r} cloned mask must reference a group"
                )
            cloned_root = clone_inherited_tree(int(clone_drawn), index)
        colorspace_name = str(mask_config.get("colorspace", "rgb_display"))
        if colorspace_name not in COLORSPACES:
            raise ValueError(f"unknown blend colorspace {colorspace_name!r}")
        blend_params = make_mask_blend(
            blend, blend_colorspace=COLORSPACES[colorspace_name],
            mask_id=(
                int(drawn) if drawn is not None
                else int(inherited_drawn) if inherited_drawn is not None
                else cloned_root
            ),
            parametric=mask_config.get("parametric"),
            inverted_channels=mask_config.get("invert", []),
            boosts=mask_config.get("boosts"),
            mask_combine=int(mask_config.get("combine", 0)),
            opacity=float(mask_config.get("opacity", 100.0)),
            feathering_radius=mask_config.get("feathering_radius"),
            blur_radius=mask_config.get("blur_radius"),
            contrast=mask_config.get("contrast"), brightness=mask_config.get("brightness"),
            details=mask_config.get("details"),
        )
        entries[index] = replace(entries[index], blend_params=blend_params)

    if inherited_masks:
        owner_by_id: dict[int, str] = {}

        def assign_owner(mask_id: int, operation: str) -> None:
            previous = owner_by_id.get(mask_id)
            if previous is not None and previous != operation:
                raise ValueError(
                    f"inherited mask {mask_id} is shared by {previous!r} and {operation!r}"
                )
            if previous is not None:
                return
            owner_by_id[mask_id] = operation
            mask = inherited_by_id.get(mask_id)
            if mask is None or mask.mask_type != MASK_GROUP:
                return
            for offset in range(0, len(mask.points), 16):
                child = int.from_bytes(
                    mask.points[offset:offset + 4], "little", signed=True
                )
                assign_owner(child, operation)

        # Resolve ownership from the final history. This avoids carrying a
        # stale inherited mask tree after its operation receives a new mask.
        for entry in entries:
            if entry.operation in mask_requests:
                continue
            if entry.blend_params is None or len(entry.blend_params) < 28:
                continue
            mode = int.from_bytes(entry.blend_params[:4], "little")
            if mode & 2:
                assign_owner(
                    int.from_bytes(entry.blend_params[24:28], "little"), entry.operation
                )
        for operation, mask_config in mask_requests.items():
            inherited_drawn = mask_config.get("inherited_drawn")
            if inherited_drawn is not None:
                assign_owner(int(inherited_drawn), operation)
        for mask in inherited_masks:
            operation = owner_by_id.get(mask.mask_id)
            if operation not in entry_positions:
                continue
            if mask.mask_id in claimed:
                raise ValueError(f"generated mask ID {mask.mask_id} conflicts with inherited XMP mask")
            masks.append(replace(mask, history_index=entry_positions[operation]))

    unused_masks = set(definitions) - set(claimed)
    if unused_masks:
        raise ValueError(
            "recipe defines masks that are not reachable from a module group: "
            + ", ".join(map(str, sorted(unused_masks)))
        )

    # masks_history rows with one ``num`` are one complete forms snapshot, not
    # per-module ownership buckets. darktable renders with the final snapshot
    # only, so every active drawn-mask tree must be present there. Attach the
    # complete current set to the last history item that uses a drawn mask.
    drawn_history_indexes = [
        index for index, entry in enumerate(entries)
        if entry.blend_params is not None
        and len(entry.blend_params) >= 28
        and int.from_bytes(entry.blend_params[:4], "little") & 2
    ]
    if masks and not drawn_history_indexes:
        raise ValueError("mask forms exist but no active module uses a drawn mask")
    if drawn_history_indexes:
        final_mask_history_index = max(drawn_history_indexes)
        masks = [
            replace(mask, history_index=final_mask_history_index) for mask in masks
        ]

    source_name = str(recipe.get("source_name") or metadata.get("source_name") or "photo.raw")
    output = output_override or _resolve(base_dir, recipe.get("output"))
    if output is None:
        raise ValueError("recipe needs output or build needs --output")
    nonbase = [entry.operation for entry in entries if entry.priority != 0]
    if nonbase:
        raise ValueError(
            "non-base module instances require a complete custom iop order, which "
            "this CLI does not emit: " + ", ".join(nonbase)
        )
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    runtime = check_darktable_version()
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{output.stem}.", suffix=".tmp.xmp", dir=output.parent, delete=False
    )
    temporary = Path(handle.name)
    handle.close()
    try:
        write_xmp(
            temporary, source_name=source_name, entries=entries,
            default_blend=blend, masks=masks, darktable_version=runtime,
        )
        report = validate_xmp(temporary)
        if report["errors"]:
            raise ValueError(
                "built XMP failed validation: " + "; ".join(report["errors"])
            )
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "status": "ok", "recipe_version": recipe_version,
        "output": str(output), "darktable_version": runtime,
        "history": [entry.operation for entry in entries],
        "mask_ids": [mask.mask_id for mask in masks], "warnings": report["warnings"],
    }


def _decode_summary(entry: HistoryEntry) -> object:
    try:
        if entry.operation == "temperature" and entry.module_version == 4:
            return unpack_white_balance_v4(entry.params)
        if entry.operation == "channelmixerrgb" and entry.module_version == 3:
            return unpack_color_calibration_v3(entry.params)
        if entry.operation == "denoiseprofile" and entry.module_version == 12:
            result = unpack_denoise_profile_v12(entry.params)
            result["curve_x"] = "6x7 values"
            result["curve_y"] = "6x7 values"
            return result
        if entry.operation == "diffuse" and entry.module_version == 2:
            return unpack_diffuse_v2(entry.params)
        if entry.operation == "rgbcurve" and entry.module_version == 1:
            return unpack_rgb_curve_v1(entry.params)
        if entry.operation == "exposure" and entry.module_version == 6:
            return unpack_exposure_v6(entry.params)
        if entry.operation == "toneequal" and entry.module_version == 2:
            return unpack_tone_equalizer_v2(entry.params)
        if entry.operation == "colorequal" and entry.module_version == 4:
            return unpack_color_equalizer_v4(entry.params)
        if entry.operation == "colorbalancergb" and entry.module_version == 5:
            return unpack_color_balance_rgb_v5(entry.params)
        if entry.operation == "basicadj" and entry.module_version == 2:
            return unpack_basic_adjustments_v2(entry.params)
        if entry.operation == "hazeremoval" and entry.module_version == 3:
            return unpack_haze_removal_v3(entry.params)
        if entry.operation == "crop" and entry.module_version == 3:
            return unpack_crop_v3(entry.params)
        if entry.operation == "ashift" and entry.module_version == 5:
            return unpack_perspective_v5(entry.params)
        if entry.operation == "flip" and entry.module_version == 2:
            return unpack_flip_v2(entry.params)
    except ValueError as exc:
        return {"decode_error": str(exc)}
    return None


def _blend_summary(blob: bytes | None) -> dict[str, object] | None:
    if blob is None or len(blob) != BLEND_BLOB_LENGTH:
        return None
    mode = struct.unpack_from("<I", blob, 0)[0]
    colorspace = struct.unpack_from("<i", blob, 4)[0]
    opacity = struct.unpack_from("<f", blob, 16)[0]
    mask_id = struct.unpack_from("<i", blob, 24)[0]
    blendif = struct.unpack_from("<I", blob, 28)[0]
    channels = BLEND_CHANNELS.get(colorspace, {})
    active = [name for name, index in channels.items() if blendif & (1 << index)]
    inverted = [
        name for name, index in channels.items() if blendif & (1 << (index + 16))
    ]
    return {
        "mask_mode": mode,
        "colorspace": next(
            (name for name, value in COLORSPACES.items() if value == colorspace),
            colorspace,
        ),
        "opacity": opacity,
        "drawn_mask_id": mask_id if mode & 2 else None,
        "parametric_channels": active,
        "inverted_channels": inverted,
    }


def inspect_xmp(path: Path) -> dict[str, object]:
    entries, masks, metadata = read_xmp(path)
    return {
        **metadata,
        "history": [
            {
                "index": index, "operation": entry.operation,
                "module_version": entry.module_version, "enabled": bool(entry.enabled),
                "name": entry.name, "parameter_bytes": len(entry.params),
                "blendop_version": entry.blendop_version,
                "blend": _blend_summary(entry.blend_params),
                "decoded": _decode_summary(entry),
            }
            for index, entry in enumerate(entries)
        ],
        "masks": [
            {
                "history_index": mask.history_index, "id": mask.mask_id,
                "type": next((name for name, value in MASK_TYPES.items() if value == mask.mask_type), mask.mask_type),
                "name": mask.name, "version": mask.mask_version,
                "point_count": mask.point_count, "point_bytes": len(mask.points),
                "children": (
                    [
                        int.from_bytes(mask.points[offset:offset + 4], "little", signed=True)
                        for offset in range(0, len(mask.points), 16)
                    ] if mask.mask_type == MASK_GROUP else None
                ),
                "ai_generated_likely": (
                    mask.mask_type in {MASK_PATH, MASK_GROUP} and mask.name.lower().startswith("ai object")
                ),
            }
            for mask in masks
        ],
    }


def validate_xmp(path: Path) -> dict[str, object]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        entries, masks, _ = read_xmp(path)
    except Exception as exc:
        return {"status": "invalid", "path": str(path), "errors": [str(exc)], "warnings": []}
    operations: set[str] = set()
    for index, entry in enumerate(entries):
        if entry.operation in operations:
            errors.append(f"duplicate operation {entry.operation!r}")
        operations.add(entry.operation)
        if entry.blend_params is None or len(entry.blend_params) != BLEND_BLOB_LENGTH:
            errors.append(f"history[{index}] {entry.operation}: blend blob is not 420 bytes")
        expected = MODULE_SPECS.get(entry.operation)
        if expected is None:
            warnings.append(f"history[{index}] {entry.operation}: no local decoder; preserve/render-test it")
        elif entry.module_version != expected[0]:
            warnings.append(
                f"history[{index}] {entry.operation}: expected covered v{expected[0]}, "
                f"found v{entry.module_version}; preserve/render-test it"
            )
        elif len(entry.params) != expected[1]:
            errors.append(
                f"history[{index}] {entry.operation} v{entry.module_version}: "
                f"expected {expected[1]} parameter bytes, got {len(entry.params)}"
            )
    snapshots: dict[int, list[MaskEntry]] = {}
    for mask in masks:
        if not 0 <= mask.history_index < len(entries):
            errors.append(f"mask {mask.mask_id}: invalid history index {mask.history_index}")
        snapshots.setdefault(mask.history_index, []).append(mask)
    for history_index, snapshot in snapshots.items():
        snapshot_ids = [mask.mask_id for mask in snapshot]
        if len(snapshot_ids) != len(set(snapshot_ids)):
            errors.append(f"mask snapshot {history_index}: duplicate mask IDs")
        ids = set(snapshot_ids)
        for mask in snapshot:
            expected = MASK_POINT_BYTES.get(mask.mask_type)
            if expected is None:
                warnings.append(f"mask {mask.mask_id}: unrecognized type {mask.mask_type}")
            elif len(mask.points) != expected * mask.point_count:
                errors.append(
                    f"mask {mask.mask_id}: expected {expected * mask.point_count} point bytes, "
                    f"got {len(mask.points)}"
                )
            if mask.mask_type == MASK_GROUP:
                for offset in range(0, len(mask.points), 16):
                    child = int.from_bytes(mask.points[offset:offset + 4], "little", signed=True)
                    if child not in ids:
                        errors.append(
                            f"mask snapshot {history_index} group {mask.mask_id}: "
                            f"missing child {child}"
                        )
            if mask.mask_type == MASK_OBJECT:
                warnings.append(
                    f"mask {mask.mask_id}: raw AI prompt object found; 5.6 normally "
                    "finalizes AI masks to path/group"
                )
    referenced = set()
    final_snapshot = current_mask_snapshot(masks)
    final_snapshot_index = (
        max(mask.history_index for mask in masks) if masks else None
    )
    masks_by_id = {mask.mask_id: mask for mask in final_snapshot}
    ids = set(masks_by_id)
    for history_index, entry in enumerate(entries):
        if entry.blend_params and len(entry.blend_params) >= 28:
            mode = int.from_bytes(entry.blend_params[:4], "little")
            if mode & 2:
                mask_id = int.from_bytes(entry.blend_params[24:28], "little")
                referenced.add(mask_id)
                if mask_id not in ids:
                    errors.append(f"operation {entry.operation}: drawn mask group {mask_id} is missing")
                elif masks_by_id[mask_id].mask_type != MASK_GROUP:
                    errors.append(
                        f"operation {entry.operation}: drawn mask root {mask_id} is not a group"
                    )
    if referenced and final_snapshot_index is None:
        errors.append("active drawn masks exist but masks_history is empty")
    if final_snapshot_index is not None and referenced:
        expected_snapshot_index = max(
            index for index, entry in enumerate(entries)
            if entry.blend_params is not None
            and len(entry.blend_params) >= 28
            and int.from_bytes(entry.blend_params[:4], "little") & 2
        )
        if final_snapshot_index != expected_snapshot_index:
            warnings.append(
                f"final mask snapshot is attached to history {final_snapshot_index}; "
                f"the last active drawn-mask module is history {expected_snapshot_index}"
            )
    orphaned = ids - referenced - {
        int.from_bytes(mask.points[offset:offset + 4], "little", signed=True)
        for mask in masks if mask.mask_type == MASK_GROUP
        for offset in range(0, len(mask.points), 16)
    }
    if orphaned:
        warnings.append("unreferenced mask IDs: " + ", ".join(map(str, sorted(orphaned))))
    return {
        "status": "invalid" if errors else "ok", "path": str(path),
        "history_items": len(entries), "mask_items": len(masks),
        "errors": errors, "warnings": warnings,
    }


def render(
    source: Path, xmp: Path, output: Path, *, config_dir: Path | None,
    fresh_config: bool, width: int, height: int, hq: bool,
    log_path: Path | None, executable: str,
) -> dict[str, object]:
    source = _require_file(source, "source")
    xmp = _require_file(xmp, "xmp")
    if output.resolve() == source.resolve():
        raise ValueError("render output must not overwrite the source photograph")
    structural = validate_xmp(xmp)
    if structural["errors"]:
        raise ValueError(
            "XMP failed structural validation: " + "; ".join(structural["errors"])
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.suffix:
        raise ValueError("render output needs a filename extension such as .jpg or .tif")
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{output.stem}.", suffix=output.suffix, dir=output.parent
    )
    os.close(handle)
    temporary = Path(temporary_name)
    temporary.unlink()
    temporary_config = tempfile.TemporaryDirectory(
        prefix="photo-xmp-darktable-"
    ) if fresh_config else None
    active_config = Path(temporary_config.name) if temporary_config else config_dir
    resolved_executable = resolve_darktable_executable(executable)
    command = [
        str(resolved_executable), str(source), str(xmp), str(temporary), "--width", str(width),
        "--height", str(height), "--hq", "true" if hq else "false", "--core",
    ]
    if active_config is not None:
        active_config.mkdir(parents=True, exist_ok=True)
        command += ["--configdir", str(active_config)]
    command += ["-d", "pipe"]
    try:
        completed = subprocess.run(command, text=True, capture_output=True)
        log = completed.stdout + completed.stderr
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(log, encoding="utf-8")
        patterns = (
            "invalid parameters", "unknown module", "cannot convert",
            "failed to convert", "invalid mask", "assertion failed",
            "assertion failure",
        )
        problems = [
            line for line in log.splitlines()
            if any(pattern in line.lower() for pattern in patterns)
        ]
        ok = (
            completed.returncode == 0 and temporary.is_file()
            and temporary.stat().st_size > 0 and not problems
        )
        if ok:
            temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
        if temporary_config is not None:
            temporary_config.cleanup()
    return {
        "status": "ok" if ok else "failed", "returncode": completed.returncode,
        "output": str(output), "log": None if log_path is None else str(log_path),
        "problems": problems[:50], "xmp_warnings": structural["warnings"],
        "command": command,
    }


def capabilities() -> dict[str, object]:
    return {
        "interface": "self-describing direct-parameter CLI with advanced JSON recipes",
        "recipe_version": RECIPE_VERSION,
        "recipe": {
            "module_source_modes": ["create", "inherit", "preset"],
            "operation_aliases": MODULE_ALIASES,
            "one_instance_per_operation": True,
            "paths_relative_to_recipe": True,
            "partial_override_from_inherit_or_preset": sorted(
                BASE_OVERRIDE_OPERATIONS
            ),
        },
        "edit_commands": {
            "white-balance": "technical coefficients or inherited warmth/tint",
            "color-calibration": "illuminant, adaptation, and RGB calibration",
            "exposure": "global exposure and black offset",
            "tone-equalizer": "nine scene-referred tonal zones",
            "rgb-curve": "linked or independent R/G/B curves",
            "color-equalizer": "named hue-family H/S/L controls",
            "color-balance-rgb": "named tonal grading controls",
            "basic-adjustments": "display-referred global or local adjustments",
            "denoise": "profiled denoise v12",
            "diffuse": "Diffuse or Sharpen v2 and runtime presets",
            "haze-removal": "Haze Removal v3",
            "crop": "normalized crop and aspect ratio",
            "perspective": "rotation, shift, shear, guides, and crop",
            "flip": "verified raw orientation enum",
        },
        "direct_xmp": {
            "modules": [
                "white balance v4", "color calibration v3", "exposure v6",
                "tone equalizer v2", "linked/independent RGB curve v1",
                "color equalizer v4", "color balance RGB v5",
                "basic adjustments v2", "profiled denoise v12",
                "diffuse or sharpen v2", "haze removal v3", "crop v3",
                "perspective correction v5",
            ],
            "masks": [
                "ellipse", "gradient", "Bezier path", "brush", "groups",
                "parametric masks", "drawn + parametric masks",
                "darktable-native prompted subject segmentation finalized as editable paths",
            ],
            "blend_colorspaces": {
                name: list(BLEND_CHANNELS[value]) for name, value in COLORSPACES.items()
            },
            "mask_combine": {
                "modes": MASK_COMBINE_MODES,
                "semantics": (
                    "exclusive intersects drawn and parametric masks; inclusive unions "
                    "them; inverted variants invert the combined selection"
                ),
            },
        },
        "named_fields": {
            "color_balance_rgb": list(COLOR_BALANCE_RGB_FIELDS),
            "color_equalizer_hues": list(COLOR_EQUALIZER_HUES),
            "color_equalizer_units_and_neutral": {
                "saturation": "multiplier; neutral 1.0",
                "brightness": "multiplier; neutral 1.0",
                "hue": "degree offset; neutral 0.0",
            },
            "tone_equalizer_bands": list(TONE_EQUALIZER_BANDS),
        },
        "inherit_or_preset": [
            "arbitrary concrete modules from same-image history",
            "active-runtime darktable presets",
            "camera-aware denoise models",
        ],
        "runtime_then_edit": {
            "ai_subject_person_mask": (
                "mask subject calls the active model through the installed darktable runtime; "
                "review its overlay, then apply the vectorized result with --subject-mask"
            )
        },
        "commands": [
            "doctor", "capabilities", "mask subject", "edit <module>", "inspect",
            "validate", "render", "preset list", "recipe build",
        ],
    }


def _curve_arg(value: str) -> list[list[float]]:
    """Parse X:Y,X:Y curve points without exposing XMP encoding."""
    try:
        points = [
            [float(coordinate) for coordinate in pair.split(":", 1)]
            for pair in value.split(",")
        ]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "use X:Y,X:Y,..., for example 0:0,0.5:0.55,1:1"
        ) from exc
    if any(len(point) != 2 for point in points):
        raise argparse.ArgumentTypeError(
            "use X:Y,X:Y,..., for example 0:0,0.5:0.55,1:1"
        )
    return points


def _named_values_arg(value: str) -> dict[str, float]:
    """Parse NAME=VALUE comma lists used by named color controls."""
    try:
        pairs = [item.split("=", 1) for item in value.split(",") if item]
        if not pairs or any(len(pair) != 2 for pair in pairs):
            raise ValueError
        return {name.strip(): float(number) for name, number in pairs}
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "use NAME=VALUE[,NAME=VALUE...], for example orange=0.96,yellow=0.94"
        ) from exc


def _parametric_arg(value: str) -> tuple[str, list[float]]:
    try:
        name, raw = value.split("=", 1)
        stops = [float(item) for item in raw.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "use CHANNEL=L0,L1,H0,H1, for example lightness_in=0.1,0.2,0.8,0.9"
        ) from exc
    if len(stops) != 4:
        raise argparse.ArgumentTypeError(
            "parametric masks need exactly four stops: CHANNEL=L0,L1,H0,H1"
        )
    return name, stops


def _mask_combine_arg(value: str) -> int:
    normalized = value.strip().lower().replace("_", "-")
    if normalized in MASK_COMBINE_MODES:
        return MASK_COMBINE_MODES[normalized]
    try:
        raw = int(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "use exclusive, inclusive, exclusive-inverted, inclusive-inverted, "
            "or a documented raw enum 0..7"
        ) from exc
    if not 0 <= raw <= 7:
        raise argparse.ArgumentTypeError("raw mask-combination enum must be in 0..7")
    return raw


def _boost_arg(value: str) -> tuple[str, float]:
    try:
        name, raw = value.split("=", 1)
        return name, float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "use CHANNEL=VALUE, for example lightness_in=0.0"
        ) from exc


def _json_file(path: Path, label: str) -> object:
    _require_file(path, label)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON: {path}: {exc}") from exc


def _edit_params(args: argparse.Namespace) -> dict[str, object]:
    operation = args.edit_operation
    params = {
        name: getattr(args, name) for name in args.param_fields
        if hasattr(args, name)
    }
    if operation == "exposure" and not args.from_existing and args.preset is None:
        params.setdefault("exposure", 0.0)
    if operation == "temperature":
        if "wb_preset" in params:
            params["preset"] = params.pop("wb_preset")
        if "coefficients" in params and (
            "warmth_ev" in params or "tint_ev" in params
        ):
            raise ValueError(
                "white-balance coefficients cannot be combined with warmth/tint offsets"
            )
        if args.from_existing and "coefficients" in params:
            raise ValueError(
                "--from-existing accepts --warmth-ev/--tint-ev, not --coefficients"
            )
        if args.preset is not None and params:
            allowed = {"warmth_ev", "tint_ev"}
            if set(params) - allowed:
                raise ValueError(
                    "a white-balance --preset accepts only relative --warmth-ev/--tint-ev overrides"
                )
        if not args.from_existing and args.preset is None and "coefficients" not in params:
            raise ValueError(
                "new white balance requires --coefficients; use --from-existing for "
                "relative --warmth-ev/--tint-ev changes"
            )
    elif operation == "toneequal":
        named = {
            name: getattr(args, name) for name in TONE_EQUALIZER_BANDS
            if hasattr(args, name)
        }
        if named and "bands" in params:
            raise ValueError("use --bands or named zone flags, not both")
        if named:
            params["named_bands"] = named
    elif operation == "channelmixerrgb":
        if "matrix" in params:
            params["matrix"] = list(params["matrix"])
        if "custom_xy" in params:
            params["custom_xy"] = list(params["custom_xy"])
    elif operation == "colorequal":
        for name in ("saturation", "hue", "brightness"):
            if name in params and isinstance(params[name], dict):
                unknown = set(params[name]) - set(COLOR_EQUALIZER_HUES)
                if unknown:
                    raise ValueError(
                        f"unknown {name} hue families: {', '.join(sorted(unknown))}"
                    )
    elif operation == "colorbalancergb":
        assignments = getattr(args, "overrides", [])
        overrides: dict[str, float] = {}
        for mapping in assignments:
            overrides.update(mapping)
        unknown = set(overrides) - set(COLOR_BALANCE_RGB_FIELDS)
        if unknown:
            raise ValueError(
                "unknown Color Balance RGB fields: " + ", ".join(sorted(unknown))
            )
        if overrides:
            params["overrides"] = overrides
        if "params_file" in params:
            params["params"] = _json_file(
                Path(params.pop("params_file")), "params-file"
            )
    elif operation == "denoiseprofile":
        for name in ("curve_x_file", "curve_y_file"):
            if name in params:
                params[name.removesuffix("_file")] = _json_file(
                    Path(params.pop(name)), name.replace("_", "-")
                )
    elif operation == "ashift":
        if "drawn_lines_file" in params:
            params["drawn_lines"] = _json_file(
                Path(params.pop("drawn_lines_file")), "drawn-lines-file"
            )
    elif operation == "flip" and not args.from_existing and args.preset is None:
        if "raw_enum" not in params:
            raise ValueError(
                "a new flip module requires --raw-enum; use an existing same-image "
                "orientation when possible"
            )
    return params


def _edit_masks(args: argparse.Namespace) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    shapes: list[dict[str, object]] = []
    inherited_ids: list[int] = []
    if args.input_xmp is not None:
        _, inherited_masks, _ = read_xmp(args.input_xmp)
        inherited_masks = current_mask_snapshot(inherited_masks)
        inherited_ids = [mask.mask_id for mask in inherited_masks]
    next_id = int(args.mask_id_base) if args.mask_id_base is not None else max(
        [999, *inherited_ids]
    ) + 1

    for index, values in enumerate(args.ellipse or [], 1):
        cx, cy, rx, ry, rotation, border = values
        shapes.append({
            "id": next_id, "type": "ellipse",
            "name": f"{args.mask_name or 'CLI mask'} ellipse {index}",
            "params": {
                "cx": cx, "cy": cy, "rx": rx, "ry": ry,
                "rotation": rotation, "border": border,
            },
        })
        next_id += 1
    for index, values in enumerate(args.gradient or [], 1):
        anchor_x, anchor_y, rotation, compression, curvature, steepness, state = values
        shapes.append({
            "id": next_id, "type": "gradient",
            "name": f"{args.mask_name or 'CLI mask'} gradient {index}",
            "params": {
                "anchor_x": anchor_x, "anchor_y": anchor_y,
                "rotation": rotation, "compression": compression,
                "curvature": curvature, "steepness": steepness,
                "state": int(state),
            },
        })
        next_id += 1
    for kind in ("path", "brush"):
        for index, path in enumerate(getattr(args, f"{kind}_file") or [], 1):
            points = _json_file(path, f"{kind}-file")
            if not isinstance(points, list):
                raise ValueError(f"{kind}-file must contain a JSON array of point objects")
            shapes.append({
                "id": next_id, "type": kind,
                "name": f"{args.mask_name or 'CLI mask'} {kind} {index}",
                "points": points,
            })
            next_id += 1
    if args.subject_mask is not None:
        if args.source is None:
            raise ValueError("--subject-mask requires --source so provenance can be verified")
        subject = _json_file(args.subject_mask, "subject-mask")
        if not isinstance(subject, dict) or subject.get("format") != "photo-xmp-subject-mask/v1":
            raise ValueError("--subject-mask is not a photo-xmp-subject-mask/v1 document")
        provenance = subject.get("source")
        if not isinstance(provenance, dict) or not provenance.get("sha256"):
            raise ValueError("--subject-mask has no source provenance")
        digest = hashlib.sha256(args.source.read_bytes()).hexdigest()
        if digest != provenance["sha256"]:
            raise ValueError("--subject-mask belongs to a different source photograph")
        paths = subject.get("paths")
        if not isinstance(paths, list) or not paths:
            raise ValueError("--subject-mask contains no paths")
        for index, path in enumerate(paths, 1):
            if not isinstance(path, dict) or not isinstance(path.get("points"), list):
                raise ValueError(f"--subject-mask path {index} is invalid")
            shapes.append({
                "id": next_id, "type": "path",
                "name": f"{args.mask_name or 'Subject mask'} path {index}",
                "points": path["points"],
                "group_state": 35 if path.get("sign") == "-" else None,
            })
            next_id += 1

    parametric = dict(args.parametric or [])
    boosts = dict(args.parametric_boost or [])
    has_mask = bool(shapes or args.reuse_mask is not None or parametric)
    if args.clear_mask and has_mask:
        raise ValueError("--clear-mask cannot be combined with mask definitions")
    if shapes and args.reuse_mask is not None:
        raise ValueError("new drawn shapes cannot be combined with --reuse-mask")
    if not has_mask:
        if args.parametric_invert or boosts:
            raise ValueError("parametric inversion/boost requires --parametric")
        return None, []

    mask: dict[str, object] = {
        "colorspace": args.mask_colorspace,
        "opacity": args.mask_opacity,
    }
    if shapes:
        group_id = next_id
        group_states = [
            int(shape.get("group_state") or (3 if index == 0 else 11))
            for index, shape in enumerate(shapes)
        ]
        shapes.append({
            "id": group_id, "type": "group",
            "name": args.mask_name or "CLI mask group",
            "children": [shape["id"] for shape in shapes],
            "states": group_states,
        })
        mask["drawn"] = group_id
    elif args.reuse_mask is not None:
        if args.input_xmp is None:
            raise ValueError("--reuse-mask requires --input-xmp")
        mask["clone_inherited_drawn"] = args.reuse_mask
    if parametric:
        mask["parametric"] = parametric
    if args.parametric_invert:
        mask["invert"] = args.parametric_invert
    if boosts:
        mask["boosts"] = boosts
    for cli_name, recipe_name in (
        ("mask_combine", "combine"),
        ("mask_feathering_radius", "feathering_radius"),
        ("mask_blur_radius", "blur_radius"),
        ("mask_contrast", "contrast"),
        ("mask_brightness", "brightness"),
        ("mask_details", "details"),
    ):
        value = getattr(args, cli_name)
        if value is not None:
            mask[recipe_name] = value
    return mask, shapes


def edit_xmp(args: argparse.Namespace) -> dict[str, object]:
    input_xmp = _require_file(args.input_xmp, "input-xmp")
    library_db = _require_file(args.library_db, "library-db")
    data_db = _require_file(args.data_db, "data-db")
    if input_xmp is not None and library_db is not None:
        raise ValueError("use --input-xmp or --library-db, not both")
    if args.imgid is not None and library_db is None:
        raise ValueError("--imgid requires --library-db")
    if args.from_existing and input_xmp is None and library_db is None:
        raise ValueError("--from-existing requires --input-xmp or --library-db")
    if args.preset is not None and data_db is None:
        raise ValueError("--preset requires --data-db from the active darktable config")
    source_image = _require_file(args.source, "source")
    if input_xmp is not None and args.output.resolve() == input_xmp.resolve():
        raise ValueError("--output must differ from --input-xmp; keep stages non-destructive")
    if input_xmp is None and library_db is None and source_image is None:
        raise ValueError(
            "a new XMP needs --source for provenance, or a same-image --input-xmp/--library-db"
        )

    baseline_entries: list[HistoryEntry] = []
    if input_xmp is not None:
        baseline_entries, _, _ = read_xmp(input_xmp)
    elif library_db is not None:
        baseline_entries = load_history_entries(library_db, imgid=args.imgid)
    operation_exists = any(
        entry.operation == args.edit_operation and entry.priority == 0
        for entry in baseline_entries
    )
    effective_inherit = bool(
        args.from_existing
        or (operation_exists and not args.replace and args.preset is None)
    )
    if args.from_existing and not operation_exists:
        raise ValueError(
            f"--from-existing requested, but {args.edit_operation!r} is absent from the baseline"
        )
    args.from_existing = effective_inherit

    baseline: dict[str, object] = {}
    if input_xmp is not None:
        baseline.update(xmp=str(input_xmp.resolve()), inherit=["*"], inherit_masks=True)
    elif library_db is not None:
        baseline.update(
            library_db=str(library_db.resolve()), imgid=args.imgid,
            inherit=["*"], inherit_masks=True,
        )
    if data_db is not None:
        baseline["data_db"] = str(data_db.resolve())

    params = _edit_params(args)
    module: dict[str, object] = {
        "operation": args.edit_operation,
    }
    if args.enabled is not None:
        module["enabled"] = int(args.enabled)
    if args.name:
        module["name"] = args.name
    if effective_inherit:
        module["source"] = "inherit"
    elif args.preset is not None:
        module.update(source="preset", preset=args.preset)
    else:
        module["source"] = "create"
    if params:
        module["params"] = params
    if getattr(args, "clear_mask", False):
        module["preserve_existing_blend"] = False
    mask, masks = _edit_masks(args) if args.edit_operation in MASKABLE_OPERATIONS else (None, [])
    if mask is not None:
        module["mask"] = mask

    recipe: dict[str, object] = {
        "recipe_version": RECIPE_VERSION,
        "output": str(args.output.resolve()),
        "modules": [module],
    }
    if source_image is not None:
        recipe["source_name"] = source_image.name
    if baseline:
        recipe["baseline"] = baseline
    if masks:
        recipe["masks"] = masks

    args.output.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix=".photo-xmp-edit.",
        suffix=".json", dir=args.output.parent, delete=False,
    )
    temporary_recipe = Path(handle.name)
    try:
        json.dump(recipe, handle, ensure_ascii=False, indent=2)
        handle.close()
        result = build_recipe(temporary_recipe, args.output.resolve())
    finally:
        if not handle.closed:
            handle.close()
        temporary_recipe.unlink(missing_ok=True)
    result["command"] = f"edit {args.edit_command}"
    return result


def _option(parser: argparse.ArgumentParser, *flags: str, **kwargs: object) -> None:
    """Add an edit parameter without injecting an unspecified default."""
    parser.add_argument(*flags, default=argparse.SUPPRESS, **kwargs)


def _add_edit_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--source", type=Path,
        help="source photograph; required for a new XMP and used for provenance",
    )
    parser.add_argument(
        "--input-xmp", type=Path,
        help="previous XMP for this exact photograph; all its base edits are retained",
    )
    parser.add_argument(
        "--library-db", type=Path,
        help="darktable library.db containing this exact photograph's history",
    )
    parser.add_argument("--imgid", type=int, help="image ID used with --library-db")
    parser.add_argument(
        "--data-db", type=Path,
        help="active darktable data.db; required by --preset",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--from-existing", action="store_true",
        help="start this module from its same-image state; only documented overrides apply",
    )
    source.add_argument(
        "--preset", help="start this module from an exact active-runtime preset name",
    )
    source.add_argument(
        "--replace", action="store_true",
        help="replace an existing module from verified defaults instead of patching it",
    )
    parser.add_argument("--output", type=Path, required=True, help="new XMP path")
    parser.add_argument("--name", help="human-readable darktable module instance name")
    state = parser.add_mutually_exclusive_group()
    state.add_argument(
        "--enabled", dest="enabled", action="store_true",
        help="enable the module explicitly",
    )
    state.add_argument(
        "--disabled", dest="enabled", action="store_false",
        help="disable the module explicitly",
    )
    parser.set_defaults(enabled=None)


def _add_mask_options(parser: argparse.ArgumentParser, operation: str) -> None:
    group = parser.add_argument_group(
        "local mask",
        "Optional. New shapes are grouped automatically. Coordinates are normalized "
        "to the input image. Existing AI/person masks are reused by group ID.",
    )
    group.add_argument(
        "--ellipse", action="append", nargs=6, type=float,
        metavar=("CX", "CY", "RX", "RY", "ROTATION", "FEATHER"),
        help="add a feathered ellipse; repeat for multiple shapes",
    )
    group.add_argument(
        "--gradient", action="append", nargs=7, type=float,
        metavar=("X", "Y", "ROTATION", "COMPRESSION", "CURVATURE", "STEEPNESS", "STATE"),
        help="add a gradient; STATE is 1 linear or 2 sigmoidal",
    )
    group.add_argument(
        "--path-file", action="append", type=Path,
        help=(
            "JSON array (>=3) of {corner:[x,y], ctrl1?, ctrl2?, border?, state?}; "
            "repeat for multiple paths"
        ),
    )
    group.add_argument(
        "--brush-file", action="append", type=Path,
        help=(
            "JSON array (>=2) of path points plus density/hardness in [0,1]; "
            "repeat for multiple strokes"
        ),
    )
    group.add_argument(
        "--subject-mask", type=Path,
        help=(
            "reviewed JSON from `photo-xmp mask subject`; its source SHA-256 is "
            "verified before the paths are embedded"
        ),
    )
    group.add_argument(
        "--reuse-mask", type=int, metavar="GROUP_ID",
        help="clone an existing group tree from --input-xmp, including AI-derived paths",
    )
    group.add_argument(
        "--parametric", action="append", type=_parametric_arg, metavar="CHANNEL=L0,L1,H0,H1",
        help="add a parametric channel; repeat for multiple channels",
    )
    group.add_argument(
        "--parametric-invert", action="append", default=[], metavar="CHANNEL",
        help="invert an active --parametric channel; repeat as needed",
    )
    group.add_argument(
        "--parametric-boost", action="append", type=_boost_arg, default=[],
        metavar="CHANNEL=VALUE", help="set a parametric-channel boost",
    )
    group.add_argument(
        "--mask-colorspace", choices=sorted(COLORSPACES),
        default=DEFAULT_MASK_COLORSPACE[operation],
        help=f"blend/parametric colorspace (default: {DEFAULT_MASK_COLORSPACE[operation]})",
    )
    group.add_argument(
        "--mask-opacity", type=float, default=100.0, metavar="PERCENT",
        help="mask blend opacity in [0,100] (default: 100)",
    )
    group.add_argument(
        "--mask-combine", type=_mask_combine_arg, metavar="MODE",
        help=(
            "drawn + parametric behavior: exclusive (intersection, default), "
            "inclusive (union), exclusive-inverted, or inclusive-inverted; "
            "raw enums 0..7 remain accepted"
        ),
    )
    group.add_argument("--mask-name", help="name for the generated group and shapes")
    group.add_argument(
        "--mask-id-base", type=int, help="first generated form ID; normally automatic"
    )
    group.add_argument("--mask-feathering-radius", type=float)
    group.add_argument("--mask-blur-radius", type=float)
    group.add_argument("--mask-contrast", type=float)
    group.add_argument("--mask-brightness", type=float)
    group.add_argument("--mask-details", type=float)
    group.add_argument(
        "--clear-mask", action="store_true",
        help="remove the inherited mask/blend; combine with --from-existing to keep module values",
    )


def _add_edit_parser(
    subparsers: argparse._SubParsersAction, command: str, operation: str,
    description: str, *, example: str, maskable: bool = False,
) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        command, help=description,
        description=(
            description + " Omitted flags use the module's verified create "
            "defaults. If the module already exists in --input-xmp/library-db, "
            "it is patched by default and omitted values remain unchanged; use "
            "--replace to reset it. Presets are likewise patched."
        ), epilog=example,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_edit_common(parser)
    if maskable:
        _add_mask_options(parser, operation)
    parser.set_defaults(
        edit_operation=operation, edit_command=command, param_fields=[],
    )
    return parser


def _set_param_fields(parser: argparse.ArgumentParser, *names: str) -> None:
    parser.set_defaults(param_fields=list(names))


def _add_edit_commands(sub: argparse._SubParsersAction) -> None:
    p = _add_edit_parser(
        sub, "white-balance", "temperature",
        "Set explicit channel coefficients, or adjust an inherited same-image white balance.",
        example=(
            "Examples:\n"
            "  photo-xmp edit white-balance --source photo.raw --output wb.xmp --coefficients 2.1 1.0 1.5 1.0\n"
            "  photo-xmp edit white-balance --input-xmp base.xmp --output warm.xmp --from-existing --warmth-ev 0.05"
        ),
    )
    _option(p, "--coefficients", nargs=4, type=float, metavar=("R", "G", "B", "FOURTH"))
    _option(p, "--wb-preset", choices=sorted(WHITE_BALANCE_PRESETS), help="stored white-balance preset enum (default: user)")
    _option(p, "--warmth-ev", type=float, help="relative red-vs-blue shift for --from-existing")
    _option(p, "--tint-ev", type=float, help="relative magenta-vs-green shift for --from-existing")
    _set_param_fields(p, "coefficients", "wb_preset", "warmth_ev", "tint_ev")

    p = _add_edit_parser(
        sub, "color-calibration", "channelmixerrgb",
        "Set chromatic adaptation, illuminant, and the 3x3 RGB calibration matrix.",
        example=(
            "Example:\n  photo-xmp edit color-calibration --input-xmp base.xmp --output calibrated.xmp "
            "--from-existing --adaptation cat16 --matrix 1 0 0 0 1 0 0 0 1"
        ),
    )
    _option(p, "--illuminant", choices=sorted(k for k in COLOR_CALIBRATION_ILLUMINANTS if not k.startswith("detect-") and k != "camera"), help="explicit scene illuminant; camera/auto modes require --from-existing")
    _option(p, "--temperature-kelvin", type=float, help="CCT for daylight/blackbody illuminants")
    _option(p, "--adaptation", choices=sorted(COLOR_CALIBRATION_ADAPTATIONS), help="chromatic adaptation transform")
    _option(p, "--custom-xy", nargs=2, type=float, metavar=("X", "Y"), help="CIE xy coordinates for custom illuminant")
    _option(p, "--matrix", nargs=9, type=float, metavar="VALUE", help="row-major 3x3 RGB matrix")
    _option(p, "--saturation", nargs=3, type=float, metavar=("R", "G", "B"), help="RGB saturation calibration coefficients")
    _option(p, "--lightness", nargs=3, type=float, metavar=("R", "G", "B"), help="RGB lightness calibration coefficients")
    _option(p, "--grey", nargs=3, type=float, metavar=("R", "G", "B"), help="RGB grey calibration coefficients")
    _option(p, "--fluorescent", choices=sorted(COLOR_CALIBRATION_FLUORESCENTS), help="fluorescent standard when illuminant=fluorescent")
    _option(p, "--led", choices=sorted(COLOR_CALIBRATION_LEDS), help="LED standard when illuminant=led")
    _option(p, "--gamut", type=float, help="gamut-compression control")
    _option(p, "--clip", action=argparse.BooleanOptionalAction, help="enable/disable negative-RGB clipping")
    _option(p, "--algorithm-version", type=int, choices=(0, 1, 2), help="stored darktable calibration algorithm revision")
    for flag in ("red", "green", "blue", "saturation", "lightness", "grey"):
        _option(p, f"--normalize-{flag}", action=argparse.BooleanOptionalAction, dest=f"normalize_{flag}")
    _set_param_fields(
        p, "illuminant", "temperature_kelvin", "adaptation", "custom_xy",
        "matrix", "saturation", "lightness", "grey", "fluorescent",
        "led", "gamut", "clip", "algorithm_version", "normalize_red",
        "normalize_green", "normalize_blue", "normalize_saturation",
        "normalize_lightness", "normalize_grey",
    )

    p = _add_edit_parser(
        sub, "exposure", "exposure", "Set global exposure and black offset.",
        example="Example:\n  photo-xmp edit exposure --source photo.jpg --output exposure.xmp --exposure 0.25",
        maskable=True,
    )
    _option(p, "--exposure", type=float, metavar="EV")
    _option(p, "--black", type=float, help="black offset (default: 0)")
    _option(p, "--mode", type=int, help="darktable exposure mode enum (default: 0 manual)")
    _option(p, "--deflicker-percentile", type=float)
    _option(p, "--deflicker-target-level", type=float)
    _option(p, "--compensate-exposure-bias", type=int, choices=(0, 1))
    _set_param_fields(p, "exposure", "black", "mode", "deflicker_percentile", "deflicker_target_level", "compensate_exposure_bias")

    p = _add_edit_parser(
        sub, "tone-equalizer", "toneequal", "Adjust nine scene-referred tonal zones.",
        example=(
            "Example:\n  photo-xmp edit tone-equalizer --input-xmp base.xmp --output tone.xmp "
            "--shadows 0.35 --midtones 0.18 --whites -0.12"
        ), maskable=True,
    )
    _option(p, "--bands", nargs=9, type=float, metavar="EV", help="all nine bands from noise through speculars")
    for zone in TONE_EQUALIZER_BANDS:
        _option(p, f"--{zone.replace('_', '-')}", type=float, dest=zone, metavar="EV")
    _option(p, "--blending", type=float); _option(p, "--smoothing", type=float)
    _option(p, "--feathering", type=float); _option(p, "--quantization", type=float)
    _option(p, "--contrast-boost", type=float); _option(p, "--exposure-boost", type=float)
    _option(p, "--details", type=int); _option(p, "--method", type=int); _option(p, "--iterations", type=int)
    _set_param_fields(p, "bands", "blending", "smoothing", "feathering", "quantization", "contrast_boost", "exposure_boost", "details", "method", "iterations")

    p = _add_edit_parser(
        sub, "rgb-curve", "rgbcurve", "Set a linked RGB curve or independent R/G/B curves.",
        example=(
            "Examples:\n  photo-xmp edit rgb-curve --input-xmp tone.xmp --output curve.xmp "
            "--curve '0:0,0.15:0.12,0.5:0.55,1:1'\n"
            "  photo-xmp edit rgb-curve --source photo.jpg --output channels.xmp "
            "--red '0:0,0.5:0.52,1:1' --green '0:0,0.5:0.5,1:1' "
            "--blue '0:0,0.5:0.48,1:1'"
        ), maskable=True,
    )
    _option(p, "--curve", type=_curve_arg, dest="nodes", help="linked curve as X:Y comma-separated points")
    _option(p, "--red", type=_curve_arg, help="red curve as X:Y comma-separated points"); _option(p, "--green", type=_curve_arg, help="green curve as X:Y comma-separated points"); _option(p, "--blue", type=_curve_arg, help="blue curve as X:Y comma-separated points")
    _option(p, "--curve-types", nargs=3, type=int, metavar=("R", "G", "B"), help="curve interpolation enums for R/G/B")
    _option(p, "--autoscale", type=int, help="stored darktable autoscale enum"); _option(p, "--compensate-middle-grey", type=int, choices=(0, 1)); _option(p, "--preserve-colors", type=int, help="stored preserve-colors enum")
    _set_param_fields(p, "nodes", "red", "green", "blue", "curve_types", "autoscale", "compensate_middle_grey", "preserve_colors")

    p = _add_edit_parser(
        sub, "color-equalizer", "colorequal", "Adjust named hue families in color, saturation, and brightness.",
        example=(
            "Example:\n  photo-xmp edit color-equalizer --input-xmp curve.xmp --output color.xmp "
            "--saturation 'orange=0.96,yellow=0.94,blue=1.1'"
        ), maskable=True,
    )
    _option(
        p, "--saturation", type=_named_values_arg, metavar="HUE=MULTIPLIER,...",
        help="per-hue saturation multiplier; neutral is 1.0",
    )
    _option(
        p, "--hue", type=_named_values_arg, metavar="HUE=DEGREES,...",
        help="per-hue degree offset; neutral is 0.0",
    )
    _option(
        p, "--brightness", type=_named_values_arg, metavar="HUE=MULTIPLIER,...",
        help="per-hue brightness multiplier; neutral is 1.0",
    )
    for name in ("threshold", "smoothing_hue", "contrast", "white_level", "chroma_size", "param_size", "hue_shift"):
        _option(p, f"--{name.replace('_', '-')}", type=float, dest=name)
    _option(p, "--use-filter", type=int, choices=(0, 1))
    _set_param_fields(p, "saturation", "hue", "brightness", "threshold", "smoothing_hue", "contrast", "white_level", "chroma_size", "param_size", "use_filter", "hue_shift")

    p = _add_edit_parser(
        sub, "color-balance-rgb", "colorbalancergb", "Apply tonal color grading through named Color Balance RGB fields.",
        example=(
            "Example:\n  photo-xmp edit color-balance-rgb --input-xmp color.xmp --output grade.xmp "
            "--set shadows_H=215 --set shadows_C=0.02 --set highlights_H=38 --set vibrance=0.08"
        ), maskable=True,
    )
    _option(
        p, "--set", action="append", type=_named_values_arg,
        dest="overrides", metavar="FIELD=VALUE[,FIELD=VALUE...]",
        help=(
            "named field override; repeat as needed. Fields: "
            + ", ".join(COLOR_BALANCE_RGB_FIELDS)
        ),
    )
    _option(p, "--params-file", type=Path, help="advanced JSON array containing all 32 float parameters; --set overrides named fields")
    _option(p, "--saturation-formula", type=int)
    _set_param_fields(p, "params_file", "saturation_formula")

    p = _add_edit_parser(
        sub, "basic-adjustments", "basicadj", "Apply display-referred basic adjustments, often for local subject light.",
        example=(
            "Example:\n  photo-xmp edit basic-adjustments --input-xmp grade.xmp --output subject.xmp "
            "--brightness 0.04 --ellipse 0.5 0.52 0.28 0.38 0 0.22 --mask-opacity 80"
        ), maskable=True,
    )
    for name in ("black_point", "exposure", "highlight_compression", "highlight_compression_threshold", "contrast", "middle_grey", "brightness", "saturation", "vibrance", "clip"):
        _option(p, f"--{name.replace('_', '-')}", type=float, dest=name)
    _option(p, "--preserve-colors", type=int)
    _set_param_fields(p, "black_point", "exposure", "highlight_compression", "highlight_compression_threshold", "contrast", "preserve_colors", "middle_grey", "brightness", "saturation", "vibrance", "clip")

    p = _add_edit_parser(
        sub, "denoise", "denoiseprofile", "Apply camera-aware profiled denoise v12.",
        example=(
            "Example:\n  photo-xmp edit denoise --input-xmp base.xmp --output denoise.xmp "
            "--from-existing --strength 0.8"
        ), maskable=True,
    )
    for name in ("strength", "radius", "search_radius", "shadows", "bias", "scattering", "central_pixel_weight", "overshooting"):
        _option(p, f"--{name.replace('_', '-')}", type=float, dest=name)
    _option(p, "--noise-a", nargs=3, type=float); _option(p, "--noise-b", nargs=3, type=float)
    _option(p, "--mode", type=int); _option(p, "--wavelet-color-mode", type=int)
    _option(p, "--curve-x-file", type=Path, help="JSON array of six arrays, each containing seven denoise X values")
    _option(p, "--curve-y-file", type=Path, help="JSON array of six arrays, each containing seven denoise Y values")
    for name in ("wb_adaptive_anscombe", "fix_anscombe_and_nlmeans_norm", "use_new_vst", "compensate_highlight_preservation"):
        _option(p, f"--{name.replace('_', '-')}", action=argparse.BooleanOptionalAction, dest=name)
    _set_param_fields(p, "strength", "radius", "search_radius", "shadows", "bias", "scattering", "central_pixel_weight", "overshooting", "noise_a", "noise_b", "mode", "wavelet_color_mode", "curve_x_file", "curve_y_file", "wb_adaptive_anscombe", "fix_anscombe_and_nlmeans_norm", "use_new_vst", "compensate_highlight_preservation")

    p = _add_edit_parser(
        sub, "diffuse", "diffuse", "Apply Diffuse or Sharpen v2, preferably from an active-runtime preset.",
        example=(
            "Example:\n  photo-xmp edit diffuse --source photo.jpg --output sharp.xmp "
            "--data-db config/data.db --preset '_builtin_sharpness | fast'"
        ), maskable=True,
    )
    _option(p, "--iterations", type=int); _option(p, "--sharpness", type=float); _option(p, "--radius", type=int)
    _option(p, "--regularization", type=float); _option(p, "--variance-threshold", type=float)
    _option(p, "--anisotropy", nargs=4, type=float, metavar="VALUE")
    _option(p, "--threshold", type=float); _option(p, "--speed", nargs=4, type=float, metavar="VALUE"); _option(p, "--radius-center", type=int)
    _set_param_fields(p, "iterations", "sharpness", "radius", "regularization", "variance_threshold", "anisotropy", "threshold", "speed", "radius_center")

    p = _add_edit_parser(
        sub, "haze-removal", "hazeremoval", "Remove or add haze with darktable Haze Removal v3.",
        example="Example:\n  photo-xmp edit haze-removal --source photo.jpg --output haze.xmp --strength 0.05 --distance 0.2", maskable=True,
    )
    _option(p, "--strength", type=float); _option(p, "--distance", type=float)
    _option(p, "--compatibility-mode", action=argparse.BooleanOptionalAction); _option(p, "--adaptive", action=argparse.BooleanOptionalAction)
    _set_param_fields(p, "strength", "distance", "compatibility_mode", "adaptive")

    p = _add_edit_parser(
        sub, "crop", "crop", "Crop using normalized input-image bounds.",
        example="Example:\n  photo-xmp edit crop --source photo.jpg --output crop.xmp --left 0.02 --top 0.02 --right 0.98 --bottom 0.98",
    )
    for name in ("left", "top", "right", "bottom"):
        _option(p, f"--{name}", type=float)
    _option(p, "--ratio-n", type=int); _option(p, "--ratio-d", type=int)
    _set_param_fields(p, "left", "top", "right", "bottom", "ratio_n", "ratio_d")

    p = _add_edit_parser(
        sub, "perspective", "ashift", "Apply rotation, lens shift, shear, aspect, guides, and perspective crop.",
        example="Example:\n  photo-xmp edit perspective --source photo.jpg --output perspective.xmp --rotation 0.4 --crop-mode 1",
    )
    for name in ("rotation", "lensshift_v", "lensshift_h", "shear", "focal_length", "crop_factor", "lens_dependence", "aspect"):
        _option(p, f"--{name.replace('_', '-')}", type=float, dest=name)
    _option(p, "--mode", type=int); _option(p, "--crop-mode", type=int)
    _option(p, "--crop", nargs=4, type=float, metavar=("LEFT", "RIGHT", "TOP", "BOTTOM"))
    _option(p, "--drawn-lines-file", type=Path, help="JSON array of at most 50 [x1,y1,x2,y2] guide lines")
    _option(p, "--quad-lines", nargs=8, type=float, metavar="VALUE", help="four normalized quadrilateral x/y coordinate pairs")
    _set_param_fields(p, "rotation", "lensshift_v", "lensshift_h", "shear", "focal_length", "crop_factor", "lens_dependence", "aspect", "mode", "crop_mode", "crop", "drawn_lines_file", "quad_lines")

    p = _add_edit_parser(
        sub, "flip", "flip", "Write a previously verified raw darktable orientation enum.",
        example="Example:\n  photo-xmp edit flip --source photo.jpg --output flip.xmp --raw-enum 5",
    )
    _option(p, "--raw-enum", type=int, help="darktable orientation enum already verified by a visual render")
    _set_param_fields(p, "raw_enum")


def main() -> int:
    # Keep the first recipe-oriented interface callable without advertising it
    # as the default Agent path.
    if len(sys.argv) > 1 and sys.argv[1] == "build":
        sys.argv[1:2] = ["recipe", "build"]
    elif len(sys.argv) > 1 and sys.argv[1] == "list-presets":
        sys.argv[1:2] = ["preset", "list"]
    parser = argparse.ArgumentParser(
        prog="photo-xmp", description=(
            "Agent-facing darktable XMP editing. Use `edit <module> --help` to "
            "discover direct parameters; no XMP binary knowledge is required."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    doctor_parser = sub.add_parser("doctor", help="check darktable runtime, AI model, and native mask support")
    doctor_parser.add_argument("--darktable-cli", default="darktable-cli")
    doctor_parser.add_argument("--config-dir", type=Path)
    doctor_parser.add_argument(
        "--require-render", action="store_true",
        help="exit nonzero unless the installed darktable is in the render-tested set",
    )
    doctor_parser.add_argument(
        "--require-native-ai", action="store_true",
        help="compile and load-test the native helper; exit nonzero unless AI masking is ready",
    )
    doctor_parser.add_argument(
        "--strict", action="store_true",
        help="equivalent to --require-render --require-native-ai",
    )
    sub.add_parser("capabilities", help="print the complete machine-readable capability map")
    edit_parser = sub.add_parser("edit", help="add or replace one module using direct CLI parameters")
    edit_sub = edit_parser.add_subparsers(dest="edit_command", required=True)
    _add_edit_commands(edit_sub)
    mask_parser = sub.add_parser(
        "mask", help="generate reviewable semantic masks for later edit commands"
    )
    mask_sub = mask_parser.add_subparsers(dest="mask_command", required=True)
    subject_parser = mask_sub.add_parser(
        "subject", help="prompt darktable's active mask model and generate editable paths",
        description=(
            "Use the installed darktable AI runtime and active mask model, with normalized "
            "foreground/background points and an optional box. The result is alpha/overlay "
            "previews plus ordinary editable darktable path/group mask JSON."
        ),
    )
    subject_parser.add_argument("--source", type=Path, required=True)
    subject_parser.add_argument("--output", type=Path, required=True)
    subject_parser.add_argument("--alpha", type=Path)
    subject_parser.add_argument("--preview", type=Path)
    subject_parser.add_argument(
        "--foreground", nargs=2, type=float, action="append", required=True,
        metavar=("X", "Y"), help="normalized include point; repeat for difficult subjects",
    )
    subject_parser.add_argument(
        "--background", nargs=2, type=float, action="append", default=[],
        metavar=("X", "Y"), help="normalized exclude point; repeat as needed",
    )
    subject_parser.add_argument(
        "--box", nargs=4, type=float, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"),
        help="optional normalized SAM box; rejected by models without box support",
    )
    subject_parser.add_argument("--passes", type=int, choices=(1, 2, 3), default=3)
    subject_parser.add_argument("--threshold", type=float, default=0.5)
    subject_parser.add_argument("--cleanup", type=int, default=50, help="darktable/potrace speck cleanup 0..100")
    subject_parser.add_argument("--smoothing", type=float, default=1.0, help="darktable/potrace path smoothing 0..1.3")
    subject_parser.add_argument("--feather-px", type=float, default=18.0)
    subject_parser.add_argument("--model", help="installed darktable mask model ID; defaults to darktable's active model")
    subject_parser.add_argument("--config-dir", type=Path, help="darktable config containing AI enable/model state")
    subject_parser.add_argument("--darktable-cli", default="darktable-cli")
    inspect_parser = sub.add_parser("inspect", help="inspect XMP modules and masks as JSON")
    inspect_parser.add_argument("xmp", type=Path)
    validate_parser = sub.add_parser("validate", help="validate XMP structure and known parameter sizes")
    validate_parser.add_argument("xmp", type=Path)
    recipe_parser = sub.add_parser("recipe", help="advanced batch recipe interface")
    recipe_sub = recipe_parser.add_subparsers(dest="recipe_command", required=True)
    build_parser = recipe_sub.add_parser("build", help="compile a JSON recipe into XMP")
    build_parser.add_argument("--recipe", type=Path, required=True)
    build_parser.add_argument("--output", type=Path)
    preset_group = sub.add_parser("preset", help="inspect active-runtime darktable presets")
    preset_sub = preset_group.add_subparsers(dest="preset_command", required=True)
    preset_parser = preset_sub.add_parser("list", help="list exact preset names as JSON")
    preset_parser.add_argument("--data-db", type=Path, required=True)
    preset_parser.add_argument("--operation", action="append")
    render_parser = sub.add_parser("render", help="validate and render an XMP through darktable-cli")
    render_parser.add_argument("--source", type=Path, required=True)
    render_parser.add_argument("--xmp", type=Path, required=True)
    render_parser.add_argument("--output", type=Path, required=True)
    config_group = render_parser.add_mutually_exclusive_group()
    config_group.add_argument("--config-dir", type=Path)
    config_group.add_argument("--fresh-config", action="store_true")
    render_parser.add_argument(
        "--width", type=int, default=0,
        help="maximum output width; 0 preserves the source width (default: 0)",
    )
    render_parser.add_argument(
        "--height", type=int, default=0,
        help="maximum output height; 0 preserves the source height (default: 0)",
    )
    render_parser.add_argument("--hq", action=argparse.BooleanOptionalAction, default=True)
    render_parser.add_argument("--log", type=Path)
    render_parser.add_argument("--darktable-cli", default="darktable-cli")
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            result = doctor(
                args.darktable_cli, args.config_dir,
                require_render=args.require_render or args.strict,
                require_native_ai=args.require_native_ai or args.strict,
            )
        elif args.command == "capabilities":
            result = capabilities()
        elif args.command == "edit":
            result = edit_xmp(args)
        elif args.command == "mask" and args.mask_command == "subject":
            command = [
                sys.executable, "-m", "photo_xmp.subject_mask",
                "--source", str(args.source), "--output", str(args.output),
                "--threshold", str(args.threshold), "--passes", str(args.passes),
                "--cleanup", str(args.cleanup), "--smoothing", str(args.smoothing),
                "--feather-px", str(args.feather_px),
                "--darktable-cli", args.darktable_cli,
            ]
            for x, y in args.foreground:
                command.extend(["--foreground", str(x), str(y)])
            for x, y in args.background:
                command.extend(["--background", str(x), str(y)])
            if args.box is not None:
                command.extend(["--box", *(str(value) for value in args.box)])
            for flag, value in (
                ("--alpha", args.alpha), ("--preview", args.preview),
                ("--model", args.model), ("--config-dir", args.config_dir),
            ):
                if value is not None:
                    command.extend([flag, str(value)])
            completed = subprocess.run(command, text=True, capture_output=True)
            if completed.returncode:
                raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
            result = json.loads(completed.stdout)
        elif args.command == "inspect":
            result = inspect_xmp(args.xmp)
        elif args.command == "validate":
            result = validate_xmp(args.xmp)
        elif args.command == "recipe" and args.recipe_command == "build":
            result = build_recipe(args.recipe, args.output)
        elif args.command == "preset" and args.preset_command == "list":
            data_db = _require_file(args.data_db, "data_db")
            result = {
                "status": "ok",
                "presets": list_presets(
                    data_db,
                    operations=None if not args.operation else [_operation(value) for value in args.operation],
                ),
            }
        elif args.command == "render":
            result = render(
                args.source, args.xmp, args.output, config_dir=args.config_dir,
                fresh_config=args.fresh_config, width=args.width, height=args.height,
                hq=args.hq, log_path=args.log,
                executable=args.darktable_cli,
            )
        else:
            raise AssertionError(args.command)
    except Exception as exc:
        _json_dump({"status": "error", "error": str(exc)})
        return 2
    _json_dump(result)
    return 0 if result.get("status") != "invalid" and result.get("status") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
