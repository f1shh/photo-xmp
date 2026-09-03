# Advanced JSON recipes for photo-xmp

Read this reference only when a multi-module batch operation is materially easier
as one JSON recipe than as a sequence of self-describing `photo-xmp edit`
commands. Ordinary Agent work should use `photo-xmp --help` and
`photo-xmp edit <module> --help`; it does not require this document or knowledge
of XMP encoding. Recipes are the CLI's reproducible intermediate representation
and require no third-party parser.

## Command surface

```bash
photo-xmp capabilities
photo-xmp inspect existing.xmp
photo-xmp preset list --data-db config/data.db --operation diffuse
photo-xmp recipe build --recipe xmp/recipe.json
photo-xmp validate xmp/result.xmp
photo-xmp render --source source/photo.raw --xmp xmp/result.xmp \
  --output previews/result.jpg --fresh-config --log analysis/result.log
```

All commands print JSON. A failed build or render exits nonzero. `validate` is a
structural check; `render` is the behavioral check. Use both. Build and render
replace their destination atomically only after success. Use `--fresh-config`
for an isolated acceptance render, or `--config-dir` when active-runtime presets
or an intentional darktable configuration are part of the test.
`capabilities` provides machine-readable discovery. Direct module help is the
authoritative Agent-facing parameter interface.

## Recipe model

Paths in a recipe are resolved relative to that recipe. The minimal shape is:

```json
{
  "recipe_version": 1,
  "source_name": "photo.raw",
  "output": "01-tone.xmp",
  "baseline": {
    "data_db": "../config/data.db"
  },
  "modules": [
    {
      "operation": "rgb_curve",
      "name": "tonal shape",
      "params": {
        "nodes": [[0.0, 0.0], [0.15, 0.12], [0.5, 0.55], [1.0, 1.0]]
      }
    }
  ]
}
```

`recipe_version` makes future interface changes explicit. `source_name` is
provenance written into XMP, not an input file opened during build. `output` can
be overridden with `build --output`. The CLI contains the
source-confirmed neutral blend-v14 structure; `data.db` is required only for
presets, and `library.db`/an existing XMP only for same-image inheritance.

### Continue an existing edit

Use the exact photograph's XMP and retain its masks:

```json
{
  "output": "02-revised.xmp",
  "baseline": {
    "xmp": "01-accepted.xmp",
    "data_db": "../config/data.db",
    "inherit": ["*"],
    "inherit_masks": true
  },
  "modules": [
    {
      "operation": "haze_removal",
      "params": {"strength": 0.05, "distance": 0.2}
    }
  ]
}
```

`inherit: ["*"]` keeps the latest base edit for every operation. Naming an
operation keeps only that operation. A module in `modules` replaces the inherited
operation rather than appending a duplicate. True multi-instances are rejected
because their custom iop-order is not synthesized by this writer.

For a darktable library, use:

```json
{
  "baseline": {
    "library_db": "../config/library.db",
    "data_db": "../config/data.db",
    "imgid": 1,
    "inherit": ["flip", "temperature", "channelmixerrgb"],
    "inherit_masks": true
  }
}
```

Confirm that `imgid` is the source photograph. The CLI cannot infer that an
arbitrary database row is safe to transplant.

## Module source modes

Each module uses one source:

- `"source": "create"` (default): pack the documented parameters.
- `"source": "inherit"`: use the latest base instance for the exact
  photograph and patch only supplied `params`.
- `"source": "preset"`: load the exact named preset from `baseline.data_db`;
  discover names with `preset list`; patch only supplied `params`.

All directly supported modules have a tested decoder/repacker for partial
inherited or preset overrides. Omit `params` to preserve a module byte-for-byte.
The CLI rejects unsupported versions rather than silently resetting fields.

Use `inherit` for RAW White Balance, camera/auto Color Calibration, existing AI
paths, orientation, and an existing profiled-denoise state. For profiled denoise,
a new v12 module deliberately stores darktable's `a[0] = -1` auto-profile marker;
explicit `noise_a` and `noise_b` are accepted only as a complete camera model.
Use a runtime preset for Diffuse or Sharpen when its named behavior matches the
job; raw coefficients remain available for deliberate custom work.

## Covered operation names and parameters

Aliases on the left resolve to darktable's operation on the right. Parameter
names match the public functions in `photo_xmp.xmp`.

| Recipe operation | darktable operation | Important parameters |
|---|---|---|
| `white_balance` | `temperature` v4 | new: `coefficients`, `preset`; inherited: `warmth_ev`, `tint_ev` |
| `color_calibration` | `channelmixerrgb` v3 | `illuminant`, `temperature_kelvin`, `adaptation`, `custom_xy`, `matrix`, `saturation`, `lightness`, `grey`, `gamut`, `clip` |
| `exposure` | `exposure` v6 | `exposure`, `black` and advanced mode fields |
| `tone_equalizer` | `toneequal` v2 | `named_bands` or nine-value `bands`, plus blending/smoothing controls |
| `rgb_curve` | `rgbcurve` v1 | linked `nodes`, or all of `red`, `green`, `blue`; optional `curve_types`, `autoscale`, `preserve_colors` |
| `color_equalizer` | `colorequal` v4 | eight-value arrays or named mappings for `saturation`, `hue`, `brightness`; filter controls |
| `color_balance_rgb` | `colorbalancergb` v5 | named `overrides`, optional complete `params`, `saturation_formula` |
| `basic_adjustments` | `basicadj` v2 | `black_point`, `exposure`, `contrast`, `brightness`, `saturation`, `vibrance`, highlight controls |
| `denoise` | `denoiseprofile` v12 | strength/mode controls, optional complete noise model and 6×7 curves |
| `diffuse_or_sharpen` | `diffuse` v2 | preset, or iterations/radius/regularization/anisotropy/threshold/speed |
| `haze_removal` | `hazeremoval` v3 | `strength`, `distance`, `compatibility_mode`, `adaptive` |
| `crop` | `crop` v3 | normalized `left`, `top`, `right`, `bottom`, optional ratio numerator/denominator |
| `perspective` | `ashift` v5 | rotation, vertical/horizontal lens shift, shear, focal length, crop factor, lens dependence, aspect, mode, crop mode/box, optional guides and quadrilateral |
| `flip` | `flip` v2 | `raw_enum`; only use a visually verified enum or inherit it |

Run `capabilities` for the authoritative current list. The recipe exposes the
same argument names as the packers; use command help first and inspect
`photo_xmp.xmp` only while extending or debugging the implementation.

### Independent channel curves

Provide all three curves to enable manual independent-channel mode:

```json
{
  "operation": "rgb_curve",
  "params": {
    "red": [[0, 0], [0.5, 0.52], [1, 1]],
    "green": [[0, 0], [0.5, 0.50], [1, 1]],
    "blue": [[0, 0], [0.5, 0.48], [1, 1]]
  }
}
```

Two to twenty nodes per channel are supported. X coordinates must increase and
all coordinates are normalized. Use a linked `nodes` curve for luminance shape
when channel separation is not the intended job.

## Drawn masks

Define shapes once at recipe root and attach a group to a module. IDs must be
positive and unique. Coordinates are normalized to the input image.

```json
{
  "modules": [
    {
      "operation": "basic_adjustments",
      "name": "soft subject light",
      "params": {"brightness": 0.04},
      "mask": {
        "drawn": 1099,
        "colorspace": "rgb_display",
        "opacity": 80
      }
    }
  ],
  "masks": [
    {
      "id": 1001,
      "type": "ellipse",
      "name": "broad subject field",
      "params": {"cx": 0.5, "cy": 0.52, "rx": 0.25, "ry": 0.36, "border": 0.22}
    },
    {"id": 1099, "type": "group", "children": [1001]}
  ]
}
```

Supported shapes:

- `ellipse`: `cx`, `cy`, `rx`, `ry`, optional `rotation`, `border`, `flags`;
- `gradient`: `anchor_x`, `anchor_y`, optional `rotation`, `compression`,
  `curvature`, `steepness`, `state` (`1` linear, `2` sigmoidal);
- `path`: at least three point objects with `corner`, optional `ctrl1`, `ctrl2`,
  one/two-value `border`, and `state`; omitted controls use darktable automatic
  control points;
- `brush`: at least two point objects with the path fields plus `density` and
  `hardness`;
- `group`: `children`, optional per-child `opacities` and `states`.

Path and brush encoding is exact, but coordinates chosen without visual review
can still be aesthetically wrong. Render and inspect at 100%.

## Parametric and combined masks

Attach four normalized feather stops `[low0, low1, high0, high1]`. Available
channel names depend on `colorspace`; inspect `capabilities` for the maintained
map. Example:

```json
{
  "drawn": 1099,
  "colorspace": "rgb_display",
  "parametric": {
    "lightness_in": [0.10, 0.20, 0.78, 0.90]
  },
  "invert": [],
  "opacity": 75,
  "blur_radius": 1.0
}
```

Omit `drawn` for parametric-only masking. Add a channel name to `invert` only if
it is active in `parametric`. Optional refinement fields are `feathering_radius`,
`blur_radius`, `contrast`, `brightness`, and `details`. Use a module-appropriate
blend space: `rgb_display`, `rgb_scene`, `lab`, or `raw`.

## AI subject/person masks

darktable 5.6's object prompt is transient; its durable XMP result is a set of
ordinary Bézier paths and a group. The CLI can invoke the active mask model
through the installed darktable runtime and create that finalized form:

    photo-xmp doctor
    photo-xmp mask subject --source photo.jpg --foreground 0.50 0.35 \
      --background 0.08 0.08 --passes 3 --output subject.json \
      --preview subject-preview.jpg
    photo-xmp edit basic-adjustments --source photo.jpg \
      --subject-mask subject.json --brightness 0.04 --output subject-light.xmp

Coordinates are normalized. At least one foreground point is required. Add
prompts based on the preview rather than assuming the frame center identifies a
person. The CLI intentionally rejects recipe masks named `ai`, `object`,
`person`, or `subject`: inference is a runtime command, while the reviewed JSON
is the deterministic path/group interchange. Existing finalized groups can
still be cloned with `--input-xmp existing.xmp --reuse-mask GROUP_ID`.

## Required validation

After every build:

1. run `validate`;
2. run `render` on the actual source, preferably in a fresh config;
3. inspect the JSON `problems` list and saved log;
4. confirm intended modules appear in the pipe, a local mask logs
   `blend with form`, and its blend colorspace is not `NONE`;
5. visually inspect the full frame, local transitions, texture, and the
   intended direction of change.

Successful XML parsing or a zero-length check is insufficient. Structural and
behavioral success still do not prove aesthetic completion.
