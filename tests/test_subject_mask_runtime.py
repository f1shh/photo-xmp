from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from photo_xmp import cli, subject_mask, xmp


def make_runtime(root: Path, *, library_dirs: tuple[Path, ...]) -> subject_mask.Runtime:
    return subject_mask.Runtime(
        executable=root / "bin/darktable-cli",
        library=root / "lib/darktable/libdarktable.so",
        glib=root / "lib/libglib-2.0.so.0",
        datadir=root / "share/darktable",
        moduledir=root / "lib/darktable",
        localedir=root / "share/locale",
        configdir=root / "config",
        cachedir=root / "cache",
        version="5.6.1",
        ai_build_support=True,
        ai_enabled=True,
        active_model="mask-object-test",
        models_dir=root / "models",
        model_files_installed=True,
        library_dirs=library_dirs,
    )


class RuntimeLayoutTests(unittest.TestCase):
    def test_linux_discovers_debian_multiarch_appdir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            appdir = Path(directory) / "AppDir"
            binary = appdir / "usr/bin/darktable-cli"
            library = appdir / "usr/lib/x86_64-linux-gnu/darktable/libdarktable.so"
            glib = appdir / "usr/lib/libglib-2.0.so.0"
            for path in (binary, library, glib):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            datadir = appdir / "usr/share/darktable"
            datadir.mkdir(parents=True)

            result = subject_mask._runtime_layout(binary, "Linux")
            self.assertEqual(result[0], library.resolve())
            self.assertEqual(result[1], glib.resolve())
            self.assertEqual(result[2], datadir.resolve())
            self.assertIn((appdir / "usr/lib").resolve(), result[5])
            self.assertIn(library.parent.resolve(), result[5])

    def test_windows_discovers_official_installer_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "darktable"
            binary = root / "bin/darktable-cli.exe"
            library = root / "bin/libdarktable.dll"
            glib = root / "bin/libglib-2.0-0.dll"
            for path in (binary, library, glib):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            (root / "lib/darktable").mkdir(parents=True)
            (root / "share/darktable").mkdir(parents=True)

            result = subject_mask._runtime_layout(binary, "Windows")
            self.assertEqual(result[0], library.resolve())
            self.assertEqual(result[1], glib.resolve())
            self.assertEqual(result[2], (root / "share/darktable").resolve())
            self.assertEqual(result[3], (root / "lib/darktable").resolve())
            self.assertEqual(result[5][0], (root / "bin").resolve())

    def test_platform_specific_private_library_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "private"
            private.mkdir()
            runtime = make_runtime(root, library_dirs=(private,))
            for system, variable in (
                ("Linux", "LD_LIBRARY_PATH"),
                ("Darwin", "DYLD_LIBRARY_PATH"),
                ("Windows", "PATH"),
            ):
                with mock.patch.object(subject_mask.platform, "system", return_value=system):
                    environment = subject_mask._native_environment(runtime)
                self.assertEqual(environment[variable].split(os.pathsep)[0], str(private))

    def test_windows_loader_failure_is_reported_as_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "darktable/bin"
            private.mkdir(parents=True)
            runtime = make_runtime(root, library_dirs=(private,))
            helper = root / "helper.exe"
            helper.touch()
            with (
                mock.patch.object(subject_mask.platform, "system", return_value="Windows"),
                mock.patch.object(
                    subject_mask, "_run",
                    side_effect=OSError(126, "The specified module could not be found"),
                ),
            ):
                ok, detail, missing = subject_mask._loader_smoke(helper, runtime)
            self.assertFalse(ok)
            self.assertIn("could not be started", detail)
            self.assertEqual(missing, [])

    def test_windows_native_cache_and_link_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "darktable/bin"
            bin_dir.mkdir(parents=True)
            library = bin_dir / "libdarktable.dll"
            glib = bin_dir / "libglib-2.0-0.dll"
            library.touch()
            glib.touch()
            runtime = make_runtime(root, library_dirs=(bin_dir.resolve(),))
            runtime = subject_mask.Runtime(
                **{
                    **runtime.__dict__,
                    "library": library,
                    "glib": glib,
                    "cachedir": root / "LocalAppData/darktable",
                }
            )
            source = root / "native.c"
            source.write_text("int main(void){return 0;}\n")
            commands: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                output = Path(command[command.index("-o") + 1])
                output.parent.mkdir(parents=True, exist_ok=True)
                output.touch()
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                mock.patch.object(subject_mask, "NATIVE_SOURCE", source),
                mock.patch.object(subject_mask.platform, "system", return_value="Windows"),
                mock.patch.object(subject_mask, "_find_compiler", return_value="C:/msys64/ucrt64/bin/cc.exe"),
                mock.patch.object(subject_mask, "_pkg_config_glib_flags", return_value=[]),
                mock.patch.object(subject_mask, "_run", side_effect=fake_run),
                mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root / "LocalAppData")}),
            ):
                helper = subject_mask._compile_native(runtime)
            self.assertEqual(helper.suffix, ".exe")
            self.assertEqual(helper.parent, root / "LocalAppData/photo-xmp/native")
            self.assertIn(str(library), commands[-1])
            self.assertIn(str(glib), commands[-1])

    def test_windows_finds_standard_msys2_compiler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory) / "ucrt64"
            compiler = prefix / "bin/gcc.exe"
            compiler.parent.mkdir(parents=True)
            compiler.touch()
            with (
                mock.patch.object(subject_mask.platform, "system", return_value="Windows"),
                mock.patch.object(subject_mask.shutil, "which", return_value=None),
                mock.patch.dict(os.environ, {"MSYSTEM_PREFIX": str(prefix)}, clear=False),
            ):
                self.assertEqual(subject_mask._find_compiler(), str(compiler.resolve()))

    def test_windows_resolves_official_install_without_path_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            program_files = Path(directory)
            executable = program_files / "darktable/bin/darktable-cli.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            with (
                mock.patch.object(xmp.platform, "system", return_value="Windows"),
                mock.patch.object(xmp.shutil, "which", return_value=None),
                mock.patch.dict(
                    os.environ, {"ProgramFiles": str(program_files)}, clear=False
                ),
            ):
                resolved = xmp.resolve_darktable_executable("darktable-cli")
            self.assertEqual(resolved, executable.resolve())

    def test_doctor_blocks_when_compiled_helper_is_not_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = make_runtime(root, library_dirs=(root / "lib",))
            runtime.library.parent.mkdir(parents=True)
            runtime.library.touch()
            runtime.glib.parent.mkdir(parents=True, exist_ok=True)
            runtime.glib.touch()
            helper = root / "helper"
            helper.touch()
            args = argparse.Namespace(
                darktable_cli="darktable-cli", config_dir=None, compile_test=True
            )
            with (
                mock.patch.object(subject_mask, "discover_runtime", return_value=runtime),
                mock.patch.object(subject_mask, "_compile_native", return_value=helper),
                mock.patch.object(
                    subject_mask, "_loader_smoke",
                    return_value=(False, "libpotrace.so.0: not found", ["libpotrace.so.0 => not found"]),
                ),
                mock.patch.object(subject_mask.shutil, "which", return_value="/usr/bin/cc"),
            ):
                report = subject_mask.doctor(args)
            self.assertFalse(report["available"])
            self.assertTrue(report["loader_tested"])
            self.assertFalse(report["loader_ok"])
            self.assertIn("libpotrace.so.0", "; ".join(report["reasons"]))

    def test_linux_compile_links_glib_and_private_runtime_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "AppDir/usr/lib"
            darktable_dir = private / "x86_64-linux-gnu/darktable"
            darktable_dir.mkdir(parents=True)
            runtime = make_runtime(
                root, library_dirs=(darktable_dir.resolve(), private.resolve())
            )
            runtime.library.parent.mkdir(parents=True, exist_ok=True)
            runtime.library.touch()
            runtime.glib.parent.mkdir(parents=True, exist_ok=True)
            runtime.glib.touch()
            source = root / "native.c"
            source.write_text("int main(void){return 0;}\n")
            commands: list[list[str]] = []

            def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
                commands.append(command)
                output = Path(command[command.index("-o") + 1])
                output.touch()
                return subprocess.CompletedProcess(command, 0, "", "")

            with (
                mock.patch.object(subject_mask, "NATIVE_SOURCE", source),
                mock.patch.object(subject_mask.platform, "system", return_value="Linux"),
                mock.patch.object(subject_mask.shutil, "which", return_value="/usr/bin/cc"),
                mock.patch.object(subject_mask, "_pkg_config_glib_flags", return_value=[]),
                mock.patch.object(subject_mask, "_run", side_effect=fake_run),
                mock.patch.object(Path, "home", return_value=root / "home"),
            ):
                subject_mask._compile_native(runtime)
            command = commands[-1]
            self.assertIn(str(runtime.glib), command)
            self.assertIn("-Wl,-rpath-link," + str(private.resolve()), command)
            self.assertIn("-Wl,-rpath," + str(darktable_dir.resolve()), command)

    def test_top_level_doctor_strictly_requires_native_ai_when_requested(self) -> None:
        version_output = subprocess.CompletedProcess(
            ["darktable-cli", "--version"], 0, "darktable 5.6.1\n", ""
        )
        native_output = subprocess.CompletedProcess(
            ["python", "-m", "photo_xmp.subject_mask"], 2,
            '{"status":"unavailable","available":false,"reasons":["loader failed"]}',
            "",
        )

        def fake_subprocess_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            if "photo_xmp.subject_mask" in command:
                return native_output
            return version_output

        with (
            mock.patch.object(
                cli, "companion_tool_status",
                return_value={"uv": {"available": True}},
            ),
            mock.patch.object(
                cli, "resolve_darktable_executable",
                return_value=Path("/missing/darktable-cli"),
            ),
            mock.patch.object(cli, "check_darktable_version", return_value="5.6.1"),
            mock.patch.object(cli.subprocess, "run", side_effect=fake_subprocess_run),
        ):
            report = cli.doctor(require_native_ai=True)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["core_status"], "ok")
        self.assertEqual(report["render_status"], "ok")
        self.assertEqual(report["native_ai_status"], "unavailable")
        self.assertFalse(report["requirements"]["met"])

    def test_doctor_help_exposes_strict_capability_flags(self) -> None:
        completed = subprocess.run(
            [os.sys.executable, "-m", "photo_xmp", "doctor", "--help"],
            text=True, capture_output=True, check=True,
        )
        self.assertIn("--require-render", completed.stdout)
        self.assertIn("--require-native-ai", completed.stdout)
        self.assertIn("--strict", completed.stdout)

    @unittest.skipUnless(platform.system() == "Linux", "ELF loader regression test")
    def test_linux_loader_smoke_resolves_private_transitive_dependency(self) -> None:
        compiler = shutil.which("cc") or shutil.which("gcc")
        if compiler is None:
            self.skipTest("C compiler unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "AppDir/usr/lib"
            darktable_dir = private / "x86_64-linux-gnu/darktable"
            darktable_dir.mkdir(parents=True)
            dependency_source = root / "dependency.c"
            darktable_source = root / "darktable.c"
            helper_source = root / "helper.c"
            dependency_source.write_text("int private_dependency(void){return 42;}\n")
            darktable_source.write_text(
                "extern int private_dependency(void); "
                "int darktable_value(void){return private_dependency();}\n"
            )
            helper_source.write_text(
                "#include <stdio.h>\nextern int darktable_value(void);\n"
                "int main(void){printf(\"%d\\n\",darktable_value());return 0;}\n"
            )
            dependency = private / "libphoto_xmp_private.so"
            library = darktable_dir / "libdarktable.so"
            helper = root / "helper"
            subprocess.run(
                [compiler, "-fPIC", "-shared", str(dependency_source),
                 "-Wl,-soname,libphoto_xmp_private.so", "-o", str(dependency)],
                check=True, capture_output=True, text=True,
            )
            subprocess.run(
                [compiler, "-fPIC", "-shared", str(darktable_source),
                 "-L", str(private), "-lphoto_xmp_private",
                 "-Wl,-soname,libdarktable.so", "-o", str(library)],
                check=True, capture_output=True, text=True,
            )
            subprocess.run(
                [compiler, str(helper_source), str(library),
                 "-Wl,-rpath," + str(darktable_dir),
                 "-Wl,--allow-shlib-undefined", "-o", str(helper)],
                check=True, capture_output=True, text=True,
            )
            without_private_env = subprocess.run(
                [str(helper)], text=True, capture_output=True, env={
                    **os.environ, "LD_LIBRARY_PATH": ""
                },
            )
            self.assertNotEqual(without_private_env.returncode, 0)
            runtime = make_runtime(
                root, library_dirs=(darktable_dir.resolve(), private.resolve())
            )
            ok, detail, missing = subject_mask._loader_smoke(helper, runtime)
            self.assertTrue(ok, detail)
            self.assertEqual(missing, [])

    @unittest.skipUnless(platform.system() == "Windows", "PE loader regression test")
    def test_windows_loader_smoke_resolves_private_transitive_dependency(self) -> None:
        compiler = subject_mask._find_compiler()
        if compiler is None or "gcc" not in Path(compiler).name.lower():
            self.skipTest("MinGW GCC unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / "darktable/bin"
            private.mkdir(parents=True)
            dependency_source = root / "dependency.c"
            darktable_source = root / "darktable.c"
            glib_source = root / "glib.c"
            helper_source = root / "helper.c"
            dependency_source.write_text(
                "__declspec(dllexport) int private_dependency(void){return 42;}\n"
            )
            darktable_source.write_text(
                "__declspec(dllimport) int private_dependency(void); "
                "__declspec(dllexport) int darktable_value(void)"
                "{return private_dependency();}\n"
            )
            glib_source.write_text(
                "__declspec(dllexport) int glib_value(void){return 7;}\n"
            )
            helper_source.write_text(
                "#include <stdio.h>\n"
                "extern int darktable_value(void); extern int glib_value(void);\n"
                "int main(void){printf(\"%d\\n\",darktable_value()+glib_value());return 0;}\n"
            )
            dependency = private / "photo_xmp_private.dll"
            dependency_import = root / "libphoto_xmp_private.dll.a"
            library = private / "libdarktable.dll"
            library_import = root / "libdarktable.dll.a"
            glib = private / "libglib-2.0-0.dll"
            subprocess.run(
                [compiler, "-shared", str(dependency_source),
                 "-Wl,--out-implib," + str(dependency_import), "-o", str(dependency)],
                check=True, capture_output=True, text=True,
            )
            subprocess.run(
                [compiler, "-shared", str(darktable_source), str(dependency_import),
                 "-Wl,--out-implib," + str(library_import), "-o", str(library)],
                check=True, capture_output=True, text=True,
            )
            subprocess.run(
                [compiler, "-shared", str(glib_source), "-o", str(glib)],
                check=True, capture_output=True, text=True,
            )
            runtime = make_runtime(root, library_dirs=(private.resolve(),))
            runtime = subject_mask.Runtime(**{
                **runtime.__dict__, "library": library, "glib": glib,
                "cachedir": root / "cache",
            })
            with (
                mock.patch.object(subject_mask, "NATIVE_SOURCE", helper_source),
                mock.patch.dict(os.environ, {"LOCALAPPDATA": str(root / "LocalAppData")}),
            ):
                helper = subject_mask._compile_native(runtime)
            clean_environment = os.environ.copy()
            clean_environment["PATH"] = os.pathsep.join(
                part for part in clean_environment.get("PATH", "").split(os.pathsep)
                if Path(part).resolve() != private.resolve()
            )
            try:
                without_private_env = subprocess.run(
                    [str(helper)], text=True, capture_output=True, env=clean_environment,
                )
            except OSError:
                pass
            else:
                self.assertNotEqual(without_private_env.returncode, 0)
            ok, detail, missing = subject_mask._loader_smoke(helper, runtime)
            self.assertTrue(ok, detail)
            self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
