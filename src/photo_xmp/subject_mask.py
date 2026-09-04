#!/usr/bin/env python3
"""Use darktable's active prompt-segmentation model and emit XMP path JSON.

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sysconfig
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from .xmp import resolve_darktable_executable

FORMAT = "photo-xmp-subject-mask/v1"
SCRIPT_DIR = Path(__file__).resolve().parent
NATIVE_SOURCE = SCRIPT_DIR / "darktable_subject_mask.c"
NATIVE_AI_MIN_VERSION = (5, 6, 0)


@dataclass(frozen=True)
class Runtime:
    executable: Path
    library: Path
    glib: Path | None
    datadir: Path
    moduledir: Path
    localedir: Path
    configdir: Path
    cachedir: Path
    version: str
    ai_build_support: bool
    ai_enabled: bool
    active_model: str | None
    models_dir: Path
    model_files_installed: bool
    library_dirs: tuple[Path, ...]


def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, **kwargs)


def _resolve_executable(value: str) -> Path:
    try:
        path = resolve_darktable_executable(value)
    except FileNotFoundError as exc:
        raise RuntimeError(str(exc)) from exc
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            wrapper = handle.read(512)
    except OSError:
        wrapper = ""
    if wrapper.startswith("#!"):
        for line in wrapper.splitlines():
            if line.startswith("exec "):
                command = shlex.split(line[5:].replace('"$@"', "").strip())
                if command and Path(command[0]).is_file():
                    return Path(command[0]).resolve()
    return path


def _dedupe_paths(paths: list[Path], *, existing_only: bool = True) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        candidate = path.expanduser().resolve()
        key = os.path.normcase(str(candidate))
        if key in seen or (existing_only and not candidate.is_dir()):
            continue
        seen.add(key)
        result.append(candidate)
    return tuple(result)


def _multiarch_names() -> tuple[str, ...]:
    names: list[str] = []
    configured = sysconfig.get_config_var("MULTIARCH")
    if isinstance(configured, str) and configured:
        names.append(configured)
    dpkg = shutil.which("dpkg-architecture")
    if dpkg is not None:
        completed = _run([dpkg, "-qDEB_HOST_MULTIARCH"])
        if completed.returncode == 0 and completed.stdout.strip():
            names.append(completed.stdout.strip())
    machine = platform.machine().lower()
    common = {
        "x86_64": "x86_64-linux-gnu",
        "amd64": "x86_64-linux-gnu",
        "aarch64": "aarch64-linux-gnu",
        "arm64": "aarch64-linux-gnu",
    }.get(machine)
    if common:
        names.append(common)
    return tuple(dict.fromkeys(name for name in names if name))


def _first_file(paths: list[Path]) -> Path:
    return next((path.resolve() for path in paths if path.is_file()), paths[0].resolve())


def _runtime_layout(
    binary: Path, system: str | None = None
) -> tuple[Path, Path | None, Path, Path, Path, tuple[Path, ...]]:
    system = system or platform.system()
    if system == "Darwin" and ".app/Contents/MacOS" in str(binary):
        contents = Path(str(binary).split(".app/Contents/MacOS", 1)[0] + ".app/Contents")
        resources = contents / "Resources"
        library = resources / "lib/darktable/libdarktable.dylib"
        glib = resources / "lib/libglib-2.0.0.dylib"
        datadir = resources / "share/darktable"
        moduledir = resources / "lib/darktable"
        localedir = resources / "share/locale"
        library_dirs = _dedupe_paths([library.parent, library.parent.parent])
        return library, glib if glib.is_file() else None, datadir, moduledir, localedir, library_dirs

    root = binary.parent.parent
    if system == "Windows":
        bin_dir = binary.parent
        library = _first_file([
            bin_dir / "libdarktable.dll",
            root / "bin/libdarktable.dll",
            root / "lib/libdarktable.dll",
        ])
        glib = _first_file([
            bin_dir / "libglib-2.0-0.dll",
            bin_dir / "glib-2.0-0.dll",
            root / "lib/libglib-2.0-0.dll",
        ])
        datadir = (root / "share/darktable").resolve()
        moduledir = (root / "lib/darktable").resolve()
        localedir = (root / "share/locale").resolve()
        library_dirs = _dedupe_paths([bin_dir, library.parent, root / "lib"])
        return library, glib if glib.is_file() else None, datadir, moduledir, localedir, library_dirs

    lib_bases = [root / "lib", root / "lib64", Path("/usr/lib"),
                 Path("/usr/lib64"), Path("/usr/local/lib")]
    multiarch = _multiarch_names()
    libraries: list[Path] = []
    for base in lib_bases:
        libraries.append(base / "darktable/libdarktable.so")
        libraries.extend(base / name / "darktable/libdarktable.so" for name in multiarch)
    # Some AppImages use a triplet that differs from the host Python's. Keep
    # the search bounded to one directory level below known lib roots.
    for base in lib_bases:
        if base.is_dir():
            libraries.extend(sorted(base.glob("*/darktable/libdarktable.so")))
    library = _first_file(libraries)
    runtime_root = root
    for ancestor in library.parents:
        if ancestor.name in {"lib", "lib64"} and ancestor.parent.name == "usr":
            runtime_root = ancestor.parent
            break
        if ancestor.name in {"lib", "lib64"} and ancestor.parent == root:
            runtime_root = root
            break
    library_dirs = _dedupe_paths([
        library.parent, library.parent.parent,
        runtime_root / "lib", runtime_root / "lib64",
        *(runtime_root / "lib" / name for name in multiarch),
        *(runtime_root / "lib64" / name for name in multiarch),
    ])
    glib_candidates = [
        directory / filename
        for directory in library_dirs
        for filename in ("libglib-2.0.so.0", "libglib-2.0.so")
    ]
    glib = next((path.resolve() for path in glib_candidates if path.is_file()), None)
    data_paths = [runtime_root / "share/darktable", root / "share/darktable",
                  Path("/usr/share/darktable"), Path("/usr/local/share/darktable")]
    datadir = next((path.resolve() for path in data_paths if path.is_dir()), data_paths[0].resolve())
    moduledir = library.parent
    localedir = runtime_root / "share/locale"
    return library, glib, datadir, moduledir, localedir, library_dirs


def _read_conf(configdir: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in ("darktablerc-common", "darktablerc"):
        path = configdir / name
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if raw and not raw.lstrip().startswith("#") and "=" in raw:
                key, value = raw.split("=", 1)
                result[key.strip()] = value.strip()
    return result


def _version(
    executable: Path, detailed_executable: Path | None = None
) -> tuple[str, bool]:
    completed = _run([str(executable), "--version"])
    output = completed.stdout + completed.stderr
    if completed.returncode:
        raise RuntimeError(output.strip() or "darktable --version failed")
    first = next((line.strip() for line in output.splitlines() if line.strip()), "")
    version = first.split()[1] if first.startswith("darktable ") else first
    detailed_output = output
    detail_binary = detailed_executable or executable
    candidates = [detail_binary, detail_binary.with_name(
        "darktable.exe" if detail_binary.suffix.lower() == ".exe" else "darktable"
    )]
    for candidate in candidates:
        if "Compile options:" in detailed_output or not candidate.is_file():
            continue
        detailed = _run([str(candidate), "--version"])
        if "Compile options:" in detailed.stdout + detailed.stderr:
            detailed_output = detailed.stdout + detailed.stderr
    ai_support = any(
        line.strip().startswith("AI") and "-> ENABLED" in line
        for line in detailed_output.splitlines()
    )
    return version, ai_support


def _semantic_version(value: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?<![\d.])(\d+)\.(\d+)\.(\d+)(?![\d.])", value)
    return tuple(int(group) for group in match.groups()) if match else None


def discover_runtime(
    executable: str = "darktable-cli", config_dir: Path | None = None
) -> Runtime:
    launcher = resolve_darktable_executable(executable)
    binary = _resolve_executable(str(launcher))
    version, ai_support = _version(launcher, binary)
    library, glib, datadir, moduledir, localedir, library_dirs = _runtime_layout(binary)
    system = platform.system()
    if system == "Windows":
        local_appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        config_root = Path(os.environ.get("XDG_CONFIG_HOME", local_appdata))
        data_root = Path(os.environ.get("XDG_DATA_HOME", local_appdata))
        default_config = config_root / "darktable"
        # This is a stable private cache passed explicitly to dt_init. GLib's
        # implicit Windows cache root can be InternetCache and is not exposed
        # through a portable environment variable on every supported release.
        default_cache = Path(
            os.environ.get("XDG_CACHE_HOME", local_appdata / "darktable/cache")
        )
        default_models = data_root / "darktable/models"
    else:
        default_config = Path.home() / ".config/darktable"
        default_cache = Path.home() / ".cache/darktable"
        default_models = Path.home() / ".local/share/darktable/models"
    configdir = (config_dir or default_config).expanduser().resolve()
    cachedir = default_cache.expanduser().resolve()
    conf = _read_conf(configdir)
    ai_enabled = conf.get("plugins/ai/enabled", "false").lower() == "true"
    active_model = conf.get("plugins/ai/models/active/mask") or None
    configured_models = conf.get("plugins/ai/models_path", "").strip()
    models_dir = (
        Path(os.path.expanduser(configured_models)).resolve()
        if configured_models else default_models.resolve()
    )
    model_dir = models_dir / active_model if active_model else None
    installed = bool(
        model_dir
        and all((model_dir / name).is_file() for name in ("config.json", "encoder.onnx", "decoder.onnx"))
    )
    return Runtime(
        executable=binary, library=library,
        glib=glib if glib and glib.is_file() else None,
        datadir=datadir, moduledir=moduledir, localedir=localedir,
        configdir=configdir, cachedir=cachedir, version=version,
        ai_build_support=ai_support, ai_enabled=ai_enabled,
        active_model=active_model, models_dir=models_dir,
        model_files_installed=installed,
        library_dirs=library_dirs,
    )


def _pkg_config_glib_flags() -> list[str]:
    pkg_config = shutil.which("pkg-config")
    if pkg_config is None:
        return []
    completed = _run([pkg_config, "--cflags", "--libs", "glib-2.0"])
    return shlex.split(completed.stdout) if completed.returncode == 0 else []


def _find_compiler() -> str | None:
    preferred = ("gcc", "cc", "clang") if platform.system() == "Windows" else ("cc", "clang", "gcc")
    for name in preferred:
        compiler = shutil.which(name)
        if compiler is not None:
            return compiler
    if platform.system() != "Windows":
        return None
    roots: list[Path] = []
    msystem_prefix = os.environ.get("MSYSTEM_PREFIX")
    if msystem_prefix:
        roots.append(Path(msystem_prefix))
    system_drive = Path(os.environ.get("SystemDrive", "C:\\"))
    roots.extend(
        system_drive / "msys64" / name
        for name in ("ucrt64", "clang64", "clangarm64", "mingw64")
    )
    for root in _dedupe_paths(roots):
        for name in ("gcc.exe", "clang.exe", "cc.exe"):
            candidate = root / "bin" / name
            if candidate.is_file():
                return str(candidate.resolve())
    return None


def _native_environment(runtime: Runtime) -> dict[str, str]:
    environment = os.environ.copy()
    paths = [str(path) for path in runtime.library_dirs]
    if platform.system() == "Darwin":
        variable = "DYLD_LIBRARY_PATH"
    elif platform.system() == "Windows":
        variable = "PATH"
    else:
        variable = "LD_LIBRARY_PATH"
    inherited = environment.get(variable)
    if inherited:
        paths.append(inherited)
    environment[variable] = os.pathsep.join(paths)
    return environment


def _loader_smoke(helper: Path, runtime: Runtime) -> tuple[bool, str, list[str]]:
    diagnostics: list[str] = []
    if platform.system() == "Linux":
        ldd = shutil.which("ldd")
        if ldd is not None:
            linked = _run([ldd, str(helper)], env=_native_environment(runtime))
            missing = [
                line.strip() for line in (linked.stdout + linked.stderr).splitlines()
                if "not found" in line.lower()
            ]
            diagnostics.extend(missing)
    try:
        completed = _run([str(helper), "--help"], env=_native_environment(runtime))
    except OSError as exc:
        # Windows reports a missing transitive DLL as a CreateProcess error
        # instead of starting the program and returning a normal exit code.
        return False, f"native helper could not be started: {exc}", diagnostics
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        if diagnostics:
            detail = (detail + "; unresolved dependencies: " + " | ".join(diagnostics)).strip("; ")
        return False, detail or "native helper failed its loader smoke test", diagnostics
    return True, completed.stdout.strip(), diagnostics


def _compile_native(runtime: Runtime) -> Path:
    if not NATIVE_SOURCE.is_file():
        raise RuntimeError(f"native bridge source is missing: {NATIVE_SOURCE}")
    if not runtime.library.is_file():
        raise RuntimeError(f"libdarktable is not discoverable: {runtime.library}")
    compiler = _find_compiler()
    if compiler is None:
        raise RuntimeError("a C compiler is required for darktable-native masking")
    digest = hashlib.sha256()
    digest.update(NATIVE_SOURCE.read_bytes())
    digest.update(platform.system().encode())
    digest.update(str(Path(compiler).resolve()).encode())
    digest.update(str(runtime.library).encode())
    digest.update(str(runtime.library.stat().st_mtime_ns).encode())
    for directory in runtime.library_dirs:
        digest.update(str(directory).encode())
    if runtime.glib is not None:
        digest.update(str(runtime.glib).encode())
        digest.update(str(runtime.glib.stat().st_mtime_ns).encode())
    if platform.system() == "Windows":
        local_appdata = Path(os.environ.get("LOCALAPPDATA", runtime.cachedir.parent))
        cache = local_appdata / "photo-xmp/native"
    else:
        cache = Path.home() / ".cache/photo-xmp/native"
    cache.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if platform.system() == "Windows" else ""
    helper = cache / f"darktable-subject-mask-{digest.hexdigest()[:16]}{suffix}"
    if helper.is_file():
        return helper
    temporary = helper.with_name(helper.stem + ".tmp" + helper.suffix)
    command = [compiler, "-O2"]
    if platform.system() == "Darwin":
        lipo = shutil.which("lipo")
        if lipo:
            architectures = _run([lipo, "-archs", str(runtime.library)])
            available = architectures.stdout.strip().split()
            if available:
                command.extend(["-arch", available[0]])
    command.extend([str(NATIVE_SOURCE), "-o", str(temporary), str(runtime.library)])
    glib_flags = _pkg_config_glib_flags() if platform.system() != "Darwin" else []
    if runtime.glib is not None:
        command.append(str(runtime.glib))
    elif glib_flags:
        command.extend(glib_flags)
    if platform.system() != "Windows":
        command.extend("-Wl,-rpath," + str(path) for path in runtime.library_dirs)
    if platform.system() == "Linux":
        command.extend("-Wl,-rpath-link," + str(path) for path in runtime.library_dirs)
    if platform.system() not in {"Darwin", "Windows"}:
        command.append("-lm")
    completed = _run(command)
    if completed.returncode:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(
            "failed to compile the darktable-native bridge: "
            + (completed.stderr.strip() or completed.stdout.strip())
        )
    temporary.chmod(0o755)
    os.replace(temporary, helper)
    return helper


def doctor(args: argparse.Namespace) -> dict[str, object]:
    runtime = discover_runtime(args.darktable_cli, args.config_dir)
    reasons: list[str] = []
    parsed_version = _semantic_version(runtime.version)
    if parsed_version is None or parsed_version < NATIVE_AI_MIN_VERSION:
        reasons.append(
            "darktable 5.6.0 or later is required for native prompted AI masks"
        )
    if not runtime.ai_build_support:
        reasons.append("darktable was built without AI support")
    if not runtime.ai_enabled:
        reasons.append("plugins/ai/enabled is false")
    if not runtime.active_model:
        reasons.append("no active darktable mask model is configured")
    if runtime.active_model and not runtime.model_files_installed:
        reasons.append("the active mask model files are incomplete")
    if not runtime.library.is_file():
        reasons.append("libdarktable is not discoverable")
    if not _find_compiler():
        reasons.append("no C compiler is available for the native bridge")
    native_helper: str | None = None
    loader_tested = False
    loader_ok = False
    loader_detail: str | None = None
    unresolved_dependencies: list[str] = []
    if not reasons and args.compile_test:
        try:
            helper = _compile_native(runtime)
            native_helper = str(helper)
            loader_tested = True
            loader_ok, loader_detail, unresolved_dependencies = _loader_smoke(helper, runtime)
            if not loader_ok:
                reasons.append("native helper is not loadable: " + loader_detail)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            reasons.append(str(exc))
    return {
        "status": "ok" if not reasons else "unavailable",
        "darktable_version": runtime.version,
        "ai_build_support": runtime.ai_build_support,
        "ai_enabled": runtime.ai_enabled,
        "active_mask_model": runtime.active_model,
        "models_dir": str(runtime.models_dir),
        "model_files_installed": runtime.model_files_installed,
        "libdarktable": str(runtime.library),
        "glib": str(runtime.glib) if runtime.glib else None,
        "library_dirs": [str(path) for path in runtime.library_dirs],
        "native_helper": native_helper,
        "loader_tested": loader_tested,
        "loader_ok": loader_ok,
        "loader_detail": loader_detail,
        "unresolved_dependencies": unresolved_dependencies,
        "available": not reasons,
        "reasons": reasons,
    }


def _atomic_image(image: Image.Image, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        suffix=output.suffix or ".png", dir=output.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        image.save(temporary)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def build(args: argparse.Namespace) -> dict[str, object]:
    source = args.source.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"source does not exist: {source}")
    runtime = discover_runtime(args.darktable_cli, args.config_dir)
    availability = doctor(argparse.Namespace(
        darktable_cli=args.darktable_cli, config_dir=args.config_dir, compile_test=False
    ))
    if not availability["available"]:
        raise RuntimeError(
            "darktable-native masking is unavailable: "
            + "; ".join(availability["reasons"])
        )
    model = args.model or runtime.active_model
    if not model:
        raise RuntimeError("no active darktable mask model is configured")
    model_dir = runtime.models_dir / model
    if not all((model_dir / name).is_file() for name in ("config.json", "encoder.onnx", "decoder.onnx")):
        raise RuntimeError(f"darktable model is not installed completely: {model}")
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    width, height = image.size
    helper = _compile_native(runtime)
    loadable, load_detail, _ = _loader_smoke(helper, runtime)
    if not loadable:
        raise RuntimeError("darktable-native helper is not loadable: " + load_detail)
    output = args.output.expanduser().resolve()
    alpha_path = (args.alpha or output.with_name(output.stem + "-alpha.png")).expanduser().resolve()
    preview_path = (args.preview or output.with_name(output.stem + "-preview.jpg")).expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="photo-xmp-darktable-mask-") as directory:
        temporary = Path(directory)
        rgb_path = temporary / "source.rgb"
        mask_path = temporary / "mask.f32"
        paths_path = temporary / "paths.json"
        rgb_path.write_bytes(image.tobytes())
        command = [
            str(helper), "--configdir", str(runtime.configdir),
            "--cachedir", str(runtime.cachedir), "--datadir", str(runtime.datadir),
            "--moduledir", str(runtime.moduledir), "--localedir", str(runtime.localedir),
            "--model", model, "--rgb", str(rgb_path), "--width", str(width),
            "--height", str(height), "--passes", str(args.passes),
            "--threshold", str(args.threshold), "--cleanup", str(args.cleanup),
            "--smoothing", str(args.smoothing), "--feather", str(args.feather_px),
            "--mask", str(mask_path), "--paths", str(paths_path),
        ]
        for x, y in args.foreground:
            command.extend(["--foreground", str(x), str(y)])
        for x, y in args.background:
            command.extend(["--background", str(x), str(y)])
        if args.box is not None:
            command.extend(["--box", *(str(value) for value in args.box)])
        completed = _run(command, env=_native_environment(runtime))
        if completed.returncode:
            raise RuntimeError(
                "darktable-native segmentation failed: "
                + (completed.stderr.strip() or completed.stdout.strip())
            )
        native_paths = json.loads(paths_path.read_text(encoding="utf-8"))["paths"]
        probability = np.fromfile(mask_path, dtype=np.float32).reshape(height, width)
    alpha = np.clip(probability * 255.0, 0, 255).astype(np.uint8)
    _atomic_image(Image.fromarray(alpha, mode="L"), alpha_path)
    rgb = np.asarray(image, dtype=np.float32)
    weight = probability.clip(0.0, 1.0)[..., None]
    overlay = rgb * (1.0 - weight * 0.34) + np.array([40.0, 225.0, 190.0]) * (weight * 0.34)
    preview_image = Image.fromarray(np.clip(overlay, 0, 255).astype(np.uint8))
    draw = ImageDraw.Draw(preview_image)
    radius = max(4, round(max(width, height) * 0.003))
    for prompts, color in ((args.foreground, (50, 255, 80)), (args.background, (255, 60, 60))):
        for x, y in prompts:
            px, py = round(x * width), round(y * height)
            draw.ellipse(
                (px - radius, py - radius, px + radius, py + radius),
                fill=color, outline=(255, 255, 255), width=2,
            )
    if args.box is not None:
        x0, y0, x1, y1 = args.box
        draw.rectangle(
            (round(x0 * width), round(y0 * height), round(x1 * width), round(y1 * height)),
            outline=(255, 210, 40), width=max(2, radius // 2),
        )
    _atomic_image(preview_image, preview_path)
    paths: list[dict[str, object]] = []
    for item in native_paths:
        points = item.get("points")
        if isinstance(points, list) and len(points) >= 3:
            paths.append({
                "sign": "-" if item.get("sign") == "-" else "+",
                "points": points, "point_count": len(points),
            })
    if not paths:
        raise RuntimeError("darktable produced no editable subject path")
    coverage = float(np.count_nonzero(probability >= args.threshold) / probability.size)
    payload: dict[str, object] = {
        "format": FORMAT,
        "source": {
            "path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "width": width, "height": height,
        },
        "backend": "darktable-native",
        "darktable": {"version": runtime.version, "model": model},
        "prompts": {"foreground": args.foreground, "background": args.background, "box": args.box},
        "settings": {
            "passes": args.passes, "threshold": args.threshold,
            "cleanup": args.cleanup, "smoothing": args.smoothing,
            "feather_px": args.feather_px,
        },
        "coverage_fraction": round(coverage, 6), "paths": paths,
        "alpha": str(alpha_path), "preview": str(preview_path),
        "limitations": [
            "This is darktable prompt-driven object segmentation, not automatic person detection.",
            "The finalized XMP uses editable vector paths, not a soft hair matte.",
            "Inspect the preview before applying the mask to an edit.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", dir=output.parent, delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        temporary_json = Path(handle.name)
    os.replace(temporary_json, output)
    return {
        "status": "ok", "backend": "darktable-native",
        "darktable_version": runtime.version, "model": model,
        "output": str(output), "alpha": str(alpha_path), "preview": str(preview_path),
        "coverage_fraction": round(coverage, 6), "path_count": len(paths),
        "point_count": sum(int(item["point_count"]) for item in paths),
    }


def make_parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--doctor", action="store_true")
    result.add_argument("--compile-test", action=argparse.BooleanOptionalAction, default=True)
    result.add_argument("--darktable-cli", default="darktable-cli")
    result.add_argument("--config-dir", type=Path)
    result.add_argument("--source", type=Path)
    result.add_argument("--output", type=Path)
    result.add_argument("--alpha", type=Path)
    result.add_argument("--preview", type=Path)
    result.add_argument("--model")
    result.add_argument("--foreground", nargs=2, type=float, action="append", default=[])
    result.add_argument("--background", nargs=2, type=float, action="append", default=[])
    result.add_argument("--box", nargs=4, type=float)
    result.add_argument("--passes", type=int, choices=(1, 2, 3), default=3)
    result.add_argument("--threshold", type=float, default=0.5)
    result.add_argument("--cleanup", type=int, default=50)
    result.add_argument("--smoothing", type=float, default=1.0)
    result.add_argument("--feather-px", type=float, default=18.0)
    return result


def main() -> int:
    args = make_parser().parse_args()
    try:
        if args.doctor:
            result = doctor(args)
        else:
            if args.source is None or args.output is None:
                raise ValueError("--source and --output are required")
            if not args.foreground:
                raise ValueError("at least one --foreground X Y prompt is required")
            coordinates = [value for pair in args.foreground + args.background for value in pair]
            if args.box is not None:
                coordinates.extend(args.box)
                if args.box[0] >= args.box[2] or args.box[1] >= args.box[3]:
                    raise ValueError("--box requires LEFT TOP RIGHT BOTTOM")
            if any(value < 0.0 or value > 1.0 for value in coordinates):
                raise ValueError("prompt and box coordinates must be normalized to [0,1]")
            if not 0.3 <= args.threshold <= 0.9:
                raise ValueError("--threshold must be in [0.3,0.9]")
            if not 0 <= args.cleanup <= 100 or not 0.0 <= args.smoothing <= 1.3:
                raise ValueError("cleanup or smoothing is outside darktable's supported range")
            if args.feather_px < 0:
                raise ValueError("--feather-px must be non-negative")
            result = build(args)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") != "unavailable" else 2


if __name__ == "__main__":
    raise SystemExit(main())
