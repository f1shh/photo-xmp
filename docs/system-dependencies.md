# System dependencies

`photo-xmp` separates its core XMP engine from the wider photo-development
toolchain. This keeps XMP construction usable in minimal environments while
making missing workflow tools visible through `photo-xmp doctor`.

## Capability layers

| Layer | Tools | Behavior when absent |
| --- | --- | --- |
| Core authoring | Python 3.10+, `photo-xmp` | The CLI cannot run. |
| Runtime validation | darktable, `darktable-cli` | XMP can be inspected structurally, but runtime-derived defaults and final render validation are unavailable. |
| Native prompted masks | darktable 5.6+, AI build/model, `libdarktable`, GLib, compatible C toolchain | Only `mask subject` is unavailable; ordinary drawn and parametric masks still work. |
| Workflow analysis | `uv`, ExifTool, ImageMagick | Metadata capture, isolated helpers, contact sheets, or overlays may be unavailable. |
| Annotation fonts | Fontconfig and a Unicode/CJK font | Prefer an explicit font file; do not assume ImageMagick's default font contains the requested glyphs. |
| Optional effects | G'MIC | Only workflows that explicitly select a G'MIC operation are affected. |

Run this once per environment and again after installing anything:

```bash
photo-xmp doctor
photo-xmp doctor --require-native-ai
photo-xmp capabilities
command -v uv exiftool magick fc-match
magick -list font | head
```

## macOS with Homebrew

```bash
brew install exiftool imagemagick fontconfig uv
brew install --cask darktable font-noto-sans-cjk-sc
```

G'MIC is optional:

```bash
brew install gmic
```

Apple Silicon commonly uses `/opt/homebrew/bin`; Intel Homebrew commonly uses
`/usr/local/bin`. Ensure the relevant prefix is on `PATH`.

## Debian or Ubuntu

Package names vary by release, but a typical baseline is:

```bash
sudo apt-get update
sudo apt-get install darktable libimage-exiftool-perl imagemagick fontconfig fonts-noto-cjk
```

Install `uv` from its supported distribution channel, then install `photo-xmp`
with `uv tool install`. Install a C toolchain only if native prompted masks are
needed.

### Linux AppImage and private runtimes

`photo-xmp` recognizes ordinary `lib/darktable`, `lib64/darktable`, and
Debian/Ubuntu multiarch layouts such as
`lib/x86_64-linux-gnu/darktable`. For AppImage-style runtimes, it derives the
private library roots from the discovered `libdarktable.so` and passes them only
to the native helper through `LD_LIBRARY_PATH`. It also links GLib explicitly
because the bridge directly calls GLib functions. Do not copy AppImage libraries
into `/usr/lib` or install a missing transitive library one by one merely because
the loader names the first unresolved dependency.

The native doctor now compiles the helper, executes `helper --help`, and on
Linux records unresolved `ldd` entries. Require this result when AI masking is
part of the workflow:

```bash
photo-xmp doctor --require-native-ai
```

An AppImage still has a minimum host ABI. A failure mentioning unavailable
`GLIBC_*` or `GLIBCXX_*` symbols means the AppImage itself is newer than the host;
it is not fixed by `LD_LIBRARY_PATH`. Do not replace the system glibc in place.
Use a supported newer OS (Debian 12 or an equivalent modern base works for the
reported darktable 5.6.1 artifact), a trusted package built for the host, or a
supported source build. Debian stable repositories may carry a darktable release
older than 5.6 and therefore lack prompted AI masks even when normal rendering
works.

## Windows

The official darktable installer places `darktable-cli.exe`,
`libdarktable.dll`, GLib, and bundled dependency DLLs under the installation
`bin` directory, with modules and data under `lib` and `share`. It registers the
executables through Windows App Paths but does not need to add the directory to
the shell `PATH`. `photo-xmp` checks App Paths and the usual Program Files
location, then prepends the discovered private DLL directory only to the helper
process environment.

Native helper compilation still requires a compatible Windows C toolchain and
linkable darktable/GLib exports. The official runtime-only installer may not
include every development artifact required by a particular compiler. Prefer an
MSYS2 UCRT64/CLANGARM64 or matching darktable source-build environment when
`photo-xmp doctor --require-native-ai` reports a link failure. Never interpret
the mere presence of `libdarktable.dll` as native-helper readiness; doctor must
both compile and start the helper. Windows 5.6 AI acceleration may use DirectML,
while CPU execution remains possible when supported by the installed build.

## Doctor status and exit behavior

The report separates `core_status`, `render_status`, `native_ai_status`, and
`workflow_tools_status`. A default diagnostic run may return `status: degraded`
with exit code zero when core XMP work is usable but an optional capability is
not. Automation should declare what it needs:

```bash
photo-xmp doctor --require-render
photo-xmp doctor --require-native-ai
photo-xmp doctor --strict
```

An unmet requested capability returns `status: failed` and a nonzero exit.

## ImageMagick sees no fonts

This is distinct from having no fonts installed. Diagnose both layers:

```bash
fc-match ':lang=zh-cn'
magick -list font
```

If Fontconfig finds a suitable font but `magick -list font` is empty, use the
font's absolute path with `-font` or create a user-level ImageMagick
`type.xml` under `~/.config/ImageMagick/`. Do not edit Homebrew- or
distribution-managed `type.xml`; upgrades can replace it. After installing a
font, refresh Fontconfig with `fc-cache -f` and verify an actual annotation:

```bash
font_file=$(fc-match -f '%{file}' 'Noto Sans CJK SC')
magick -size 800x160 xc:white -font "$font_file" -pointsize 48 \
  -gravity center -annotate +0+0 '中文 Photo XMP' font-smoke.png
identify font-smoke.png
```

For automation, prefer a stable open font such as Noto Sans CJK and either a
verified ImageMagick alias or the resolved absolute path.
