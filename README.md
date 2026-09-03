# photo-xmp

`photo-xmp` is a community, third-party command-line interface for building,
inspecting, validating, and rendering editable [darktable](https://www.darktable.org/)
XMP sidecars. It is designed for AI agents and reproducible automation: the
command tree exposes editing capabilities as validated parameters instead of
requiring callers to serialize darktable internals themselves.

The project is not affiliated with or endorsed by the darktable project.

## What it provides

- direct commands for White Balance, Color Calibration, exposure, tone
  equalizer, linked or independent RGB curves, Color Equalizer, Color Balance
  RGB, basic adjustments, profiled denoise, Diffuse or Sharpen, Haze Removal,
  crop, perspective, and orientation;
- editable ellipse, gradient, Bézier path, brush, parametric, and combined masks;
- prompt-driven subject/object segmentation through the installed darktable AI
  runtime, finalized as ordinary editable darktable paths;
- same-image XMP/history inheritance and active-runtime preset loading;
- structural inspection and validation, plus isolated `darktable-cli` rendering;
- a JSON recipe interface for advanced reproducible multi-module builds.

Run `photo-xmp capabilities` for the machine-readable current surface and
`photo-xmp edit <module> --help` for the authoritative parameters.

## Requirements

- Python 3.10 or later
- darktable and `darktable-cli` for rendering
- NumPy and Pillow (installed with the package)

The native subject-mask command additionally requires a darktable build with AI
support enabled, an active installed mask model, `libdarktable`, and a C compiler.
It intentionally follows the user's darktable installation and does not download
or bundle a model. `photo-xmp doctor` reports availability and reasons.

## Install

From a checkout:

```bash
python3 -m pip install .
photo-xmp doctor
photo-xmp capabilities
```

For development:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
```

## Direct editing

Every edit writes a new XMP. Continue from the exact photograph's accepted XMP
with `--input-xmp`; an existing module is patched only in the fields supplied.

```bash
photo-xmp edit exposure \
  --source portrait.jpg --exposure 0.20 --output 01-exposure.xmp

photo-xmp edit rgb-curve \
  --input-xmp 01-exposure.xmp \
  --curve '0:0,0.16:0.13,0.50:0.55,1:1' \
  --output 02-curve.xmp

photo-xmp validate 02-curve.xmp
photo-xmp render --source portrait.jpg --xmp 02-curve.xmp \
  --output preview.jpg --fresh-config
```

Rendering preserves the source dimensions by default. Pass `--width` and/or
`--height` explicitly when a smaller preview is desired.

Module help contains mask flags as well as module parameters:

```bash
photo-xmp edit basic-adjustments --help
photo-xmp edit basic-adjustments \
  --source portrait.jpg --brightness 0.04 \
  --ellipse 0.50 0.52 0.27 0.38 0 0.25 \
  --mask-opacity 75 --output subject-light.xmp
```

## darktable-native prompted masks

This is prompt-driven object segmentation, not unprompted person detection. At
least one normalized foreground point is required. Review the generated overlay
before applying the result to an edit.

```bash
photo-xmp mask subject \
  --source portrait.jpg \
  --foreground 0.51 0.46 --background 0.08 0.08 \
  --passes 3 --output subject.json --preview subject-preview.jpg

photo-xmp edit basic-adjustments \
  --source portrait.jpg --subject-mask subject.json \
  --brightness 0.04 --mask-opacity 75 --output subject-light.xmp
```

The finalized XMP stores editable vector paths. The prompt and probability mask
are runtime inputs, not a proprietary object embedded in the sidecar.

## Advanced recipes and internals

Ordinary callers should use command help. Read [docs/recipes.md](docs/recipes.md)
when one multi-module JSON recipe is clearer than sequential commands. Read
[docs/xmp-internals.md](docs/xmp-internals.md) only when extending an uncovered
module/version or debugging a concrete encoding discrepancy.

## Safety and reproducibility

- Never transplant camera-dependent White Balance, calibration, orientation,
  denoise profiles, or masks from another photograph.
- Treat `validate` as a structural check and an isolated `render` as the
  behavioral check.
- darktable config databases are single-writer resources. Run renders serially
  when they share the default config or one `--config-dir`; parallel rendering
  is safe only when every process uses its own config, such as `--fresh-config`.
- The CLI does not choose an aesthetic result; callers should measure and review
  the rendered photograph.
- `photo-xmp` creates a new destination atomically and rejects destructive
  in-place continuation of an input XMP.

## License

`photo-xmp` is distributed under GPL-3.0-or-later. The native bridge links to the
user's installed darktable library and derives part of its prompt/refinement and
raster-to-vector flow from darktable's GPL-licensed source. See [NOTICE](NOTICE).
darktable, its runtime, and its model files are not bundled.
