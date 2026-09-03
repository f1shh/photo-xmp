# System dependencies

`photo-xmp` separates its core XMP engine from the wider photo-development
toolchain. This keeps XMP construction usable in minimal environments while
making missing workflow tools visible through `photo-xmp doctor`.

## Capability layers

| Layer | Tools | Behavior when absent |
| --- | --- | --- |
| Core authoring | Python 3.10+, `photo-xmp` | The CLI cannot run. |
| Runtime validation | darktable, `darktable-cli` | XMP can be inspected structurally, but runtime-derived defaults and final render validation are unavailable. |
| Native prompted masks | darktable AI build/model, `libdarktable`, C compiler | Only `mask subject` is unavailable; ordinary drawn and parametric masks still work. |
| Workflow analysis | `uv`, ExifTool, ImageMagick | Metadata capture, isolated helpers, contact sheets, or overlays may be unavailable. |
| Annotation fonts | Fontconfig and a Unicode/CJK font | Prefer an explicit font file; do not assume ImageMagick's default font contains the requested glyphs. |
| Optional effects | G'MIC | Only workflows that explicitly select a G'MIC operation are affected. |

Run this once per environment and again after installing anything:

```bash
photo-xmp doctor
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
