# darktable XMP encoding cookbook

This is the low-level implementation reference for the `photo-xmp` package.
Ordinary photo workflows should use the self-describing `photo-xmp` command and
its module-level help. Read [recipes.md](recipes.md) only for advanced JSON
recipes, and read this file only when extending an uncovered module or debugging
a concrete encoding discrepancy. Covered structures were derived from
darktable source/specimens and render-tested on 5.6.1; older core packers were
also exercised on 5.2.1. A new session may inspect source for uncovered modules,
version changes, or actual discrepancies without rediscovering covered layouts.

## Contents

- [Decision gate](#decision-gate)
- [Low-level entry points](#low-level-entry-points)
- [Adjustment intent to module](#adjustment-intent-to-module)
- [History and sidecar contract](#history-and-sidecar-contract)
- [Module encodings](#module-encodings)
- [Masks and blend parameters](#masks-and-blend-parameters)
- [Stage construction pattern](#stage-construction-pattern)
- [Validation](#validation)
- [When to inspect source](#when-to-inspect-source)

## Decision gate

1. Run `darktable-cli --version` and record the runtime.
2. If the needed module, blend, and mask versions appear below, import
   `photo_xmp.xmp` and reuse their recorded structures. Do not reject
   an otherwise usable runtime from its application version alone.
3. Render a minimal stage containing every structure used. Reject conversion
   warnings, missing modules, invalid masks, or a behavioral discrepancy. The
   helper warns when the runtime has not yet been recorded as render-tested.
4. If a required structure is not covered or the smoke test fails, generate a
   specimen with that runtime and inspect the relevant source. Existing covered
   packers remain reusable unless the evidence exposes a specific discrepancy.

The binary structures are versioned even though the application version is not
a hard gate. Newer releases commonly read and convert older module versions; the
reverse direction and newly introduced features are not implied.

## Low-level entry points

Record the runtime and verify the installed encoder before implementation work:

```bash
photo-xmp doctor
python -m photo_xmp.xmp --check-version
python -m photo_xmp.xmp --self-test
```

Use `photo-xmp` for direct editing, same-image inheritance, preset loading,
inspection, validation, and rendering.

From a custom builder in the work directory:

```python
from photo_xmp.xmp import (
    HistoryEntry, load_blend_template, load_history_entries,
    pack_exposure_v6, write_xmp,
)

blend = load_blend_template(
    "config/library.db", imgid=1, data_db="config/data.db"
)
entries = load_history_entries(
    "config/library.db", imgid=1, operations={"flip"}
)
entries += [
    HistoryEntry("exposure", 6, pack_exposure_v6(0.15), name="global exposure"),
]
write_xmp(
    "xmp/01-exposure.xmp",
    source_name="photo.jpg",
    entries=entries,
    default_blend=blend,
)
```

Packers return `bytes`. `HistoryEntry` and `MaskEntry` take `bytes`;
`write_xmp` performs the hexadecimal encoding. Do not call `.hex()` yourself.
Only inherit history when the database `imgid` belongs to the current source
photo. Never use the helper to copy input-profile or orientation parameters from
an unrelated image.

## Adjustment intent to module

| Editing intent | Operation / version | Bundled function | Role and constraint |
|---|---|---|---|
| Small global exposure or black offset | `exposure` v6 | `pack_exposure_v6` | Gross scene-referred placement only; do not use a large move to fix flat contrast or one dark subject; protect P95/P99 |
| Lift shadows/midtones while anchoring whites | `toneequal` v2 | `pack_tone_equalizer_v2` | Nine exposure zones; change a smooth run of neighboring bands |
| Toe, midtone, shoulder, S-curve, or channel curve | `rgbcurve` v1 | `pack_rgb_curve_v1` | Linked or independent R/G/B; keep each channel's x nodes monotonic and endpoints intentional |
| Hue-family H/S/L changes | `colorequal` v4 | `pack_color_equalizer_v4` | Red through magenta; suitable for skin, vegetation, sky, water, accents |
| Restrained skin yellow/red correction | `colorequal` v4 | `color_equalizer_values` + `pack_color_equalizer_v4` | Diagnose first; adjust neighboring red/orange/yellow families smoothly and check warm surroundings |
| Tonal color grading, chroma, vibrance, color contrast | `colorbalancergb` v5 | `pack_color_balance_rgb_v5` | Named overrides avoid fragile numeric indexes |
| Local subject brightness after masking | `basicadj` v2 | `pack_basic_adjustments_v2` | Display-referred; use `BLEND_CS_RGB_DISPLAY`; prefer a protected midtone/brightness move over hard exposure when appropriate |
| Requested skin luminosity after color correction | `basicadj` v2 + drawn skin/subject mask | `pack_basic_adjustments_v2` + mask helpers | Lift skin midtones enough for the selected portrait profile; retain texture, facial shape, warm-red variation, and highlight headroom |
| Verified orientation enum | `flip` v2 | `pack_flip_v2` | Inherit same-image history when possible; raw enum mapping is not guessed |
| Technical white balance | `temperature` v4 | `pack_white_balance_v4`, `unpack_white_balance_v4`, `relative_white_balance_coefficients` | Stores four channel multipliers, not camera-independent Kelvin/tint; inherit the exact image for RAW or supply known coefficients |
| Chromatic adaptation and color calibration | `channelmixerrgb` v3 | `pack_color_calibration_v3`, `unpack_color_calibration_v3`, `repack_color_calibration_v3` | Supports standard/custom illuminants, adaptation, gamut/clip, and a 3x3 mixer; inherit camera/auto-detected baselines from the exact image |
| RGB primaries / channel mixing | `channelmixerrgb` v3 | `pack_color_calibration_v3`, `repack_color_calibration_v3` | Use the row-major 3x3 matrix; bracket and render-check material color and skin rather than treating it as a generic saturation control |
| Camera-aware noise reduction | `denoiseprofile` v12 | `pack_denoise_profile_v12`, `repack_denoise_profile_v12` | Prefer same-image history; a new module uses darktable auto-profile unless a complete noise model is supplied |
| Sharpen, diffuse, deblur, local contrast | `diffuse` v2 | `pack_diffuse_v2` or active-runtime preset | Prefer a named runtime preset for a known photographic job, then tune deliberately |
| Remove or add haze | `hazeremoval` v3 | `pack_haze_removal_v3` | Use restrained strength and verify gradients/color |
| Crop and aspect ratio | `crop` v3 | `pack_crop_v3` | Normalized left/top/right/bottom plus stored ratio |
| Rotation and perspective | `ashift` v5 | `pack_perspective_v5` | Full rotation/lens shift/shear/lens/crop geometry, with optional guide lines and quadrilateral |

Use each module for one clear job. A typical clean workflow uses exposure for
small gross placement, tone equalizer for zones, RGB curve or contrast/fulcrum
for final tonal shape,
Color Equalizer for named hue families, and Color Balance RGB for the coherent
palette. Do not recreate the same move in three modules.

For zone work, named helpers avoid magic positions:

```python
from photo_xmp.xmp import pack_tone_equalizer_v2, tone_equalizer_bands

zones = tone_equalizer_bands(
    deep_blacks=0.15, blacks=0.25, shadows=0.35,
    midtones=0.18, whites=-0.12, speculars=-0.20,
)
tone = pack_tone_equalizer_v2(zones)
```

For creative grading, express the look as tonal relationships. For example, a
cool environment with warm light can use named Color Balance RGB overrides:

```python
from photo_xmp.xmp import pack_color_balance_rgb_v5

grade = pack_color_balance_rgb_v5(overrides={
    "shadows_C": 0.02,
    "shadows_H": 215.0,
    "highlights_C": 0.008,
    "highlights_H": 38.0,
    "chroma_global": 0.035,
    "vibrance": 0.08,
})
```

Those numbers illustrate the API, not a preset. Establish the direction on the
actual photograph, create restrained/expressive/bold candidates by scaling one
coherent relationship, then check skin and neutrals.

For selective color, arrays always follow the fixed hue order. Keep neutral
values for families that should not move:

```python
from photo_xmp.xmp import color_equalizer_values, pack_color_equalizer_v4

hsl = pack_color_equalizer_v4(
    saturation=color_equalizer_values(
        1.0, {"orange": 0.96, "yellow": 0.94, "cyan": 1.08, "blue": 1.12}
    ),
    hue=color_equalizer_values(
        0.0, {"orange": -2.0, "yellow": -1.0, "cyan": -1.0, "blue": -2.0}
    ),
    brightness=color_equalizer_values(
        1.0, {"orange": 1.02, "yellow": 1.02, "cyan": 1.01, "blue": 0.99}
    ),
)
```

## History and sidecar contract

Each `darktable:history` item must contain at least:

```text
darktable:num
darktable:operation
darktable:enabled
darktable:modversion
darktable:params
darktable:multi_name
darktable:multi_name_hand_edited
darktable:multi_priority
darktable:blendop_version
darktable:blendop_params
```

For the covered versions, the writer emits these top-level values:

```text
darktable:xmp_version=5
darktable:auto_presets_applied=1
darktable:history_end=<number of entries>
darktable:iop_order_version=5
```

History append order is not pixel-pipeline order. Inspect the rendered pipeline.
Do not transplant `colorin`, `colorout`, white-balance, orientation, or camera
calibration blobs from an unrelated photograph as if they were universal.
Choose and record one baseline mode: `inherited-current-image` loads required
entries from the exact source photograph; `implicit-defaults` lets darktable
derive its normal defaults and authors only the active adjustments. An empty or
adjustment-only entry list is `implicit-defaults`, not inherited history.

`write_xmp` rejects duplicate operation names. Although history items expose
`multi_priority`, this helper does not yet emit a verified complete multi-instance
iop-order, and darktable can silently omit the later instance. Merge the move
into one instance or investigate and render-verify multi-instance ordering before
extending the helper.

Give every stage a fresh `entries` list. Copy accepted entries, append one
parameter family, write a new sidecar, and render it. This makes rollback and
candidate attribution unambiguous.

## Module encodings

### White Balance (`temperature`) v4 — 20 bytes

```python
struct.pack("<4fi", red, green, blue, fourth, preset)
```

`pack_white_balance_v4` writes the four channel multipliers and preset enum;
`unpack_white_balance_v4` decodes a same-image history blob. darktable does not
store a universal Kelvin/tint pair in this module. Converting a CCT to these
multipliers depends on the camera/input matrix, so the helper deliberately does
not expose a fake `kelvin=` shortcut. The first three RGB coefficients must be
finite and positive; the fourth sensor-channel slot may legitimately be `NaN`
when it does not apply, and same-image relative adjustment preserves that value.

In darktable's modern scene-referred RAW workflow, this module commonly brings
sensor channels to the camera reference while Color Calibration performs the
scene-illuminant chromatic adaptation. In other workflows it may carry more of
the visible white-balance correction itself. Preserve the exact image's paired
baseline unless there is a deliberate reason to replace one part, and reject
darktable's chromatic-adaptation conflict warnings.

For a small correction to a known same-image baseline,
`relative_white_balance_coefficients` uses log2 controls: positive
`warmth_ev` raises red relative to blue, while positive `tint_ev` raises red and
blue relative to green (toward magenta). In a CLI recipe, use
`source="inherit"` with `warmth_ev` and `tint_ev`; zero relative movement
preserves the inherited coefficients. Do not copy this blob between unrelated
photographs or camera profiles. Repeated edit states are normal: the CLI selects
the newest priority-0 base instance rather than assuming every matching history
row is a separate module instance.

### Color Calibration (`channelmixerrgb`) v3 — 160 bytes

```python
struct.pack("<24f10i4f2i",
            *six_four_float_vectors, *ten_enums_or_flags,
            x, y, temperature_kelvin, gamut, clip, algorithm_version)
```

The six four-float vectors are red, green, blue, saturation, lightness, and
grey. The first three form the row-major 3x3 channel mixer in their first three
slots. The integer block stores six normalization flags, illuminant, fluorescent
standard, LED standard, and chromatic-adaptation transform. The final fields
store illuminant xy, CCT, gamut compression, clipping, and algorithm version.

Supported named illuminants are `pipeline`, `incandescent`, `daylight`,
`equal-energy`, `fluorescent`, `led`, `blackbody`, and `custom`; adaptation
choices are `linear-bradford`, `cat16`, `full-bradford`, `xyz`, and `none`.
Daylight/blackbody CCT is converted to the same CIE xy loci used by darktable;
custom requires explicit xy. Camera and surface/edge detection are deliberately
not synthesized because their results depend on the image.

`pack_color_calibration_v3` authors a new, explicit module.
`repack_color_calibration_v3` modifies an exact same-image v3 blob while
preserving its camera/auto illuminant data unless a new standard/custom
illuminant is explicitly selected. Selecting a new illuminant without an
adaptation override chooses CAT16, or bypass for the pipeline illuminant; an
unchanged illuminant preserves the inherited adaptation. The CLI exposes these
paths through a `color_calibration` recipe module, using `source="inherit"` for
the same-image baseline or `source="create"` for an explicit illuminant. Its
`params` cover temperature, fluorescent/LED standard, adaptation, custom xy,
gamut, the row-major matrix, saturation/lightness/grey coefficient triplets, and
negative-RGB clipping. The Python packer additionally exposes every
normalization flag and algorithm version. For inherited history the CLI selects
the latest priority-0 base instance; use a custom builder when a non-base
multi-instance calibration is intentional.

An explicit identity matrix with pipeline illuminant and adaptation disabled is
a no-op module, but it need not reproduce an empty XMP's source-dependent
implicit defaults. For a faithful existing baseline, use `source="inherit"` in
the CLI recipe and provide calibration overrides in that module's `params`; for
a deliberate new calibration, write it explicitly and compare a rendered
technical parent.

### Exposure v6 — 24 bytes

```python
struct.pack("<iffffi", mode, black, exposure,
            deflicker_percentile, deflicker_target_level,
            compensate_exposure_bias)
```

Manual exposure normally uses `mode=0`, percentile `50.0`, target `-4.0`, and
compensation `0`. Call `pack_exposure_v6(exposure_ev, black=...)`.

### Tone Equalizer v2 — 72 bytes

```python
struct.pack("<15f3i", *nine_bands, blending, smoothing, feathering,
            quantization, contrast_boost, exposure_boost,
            details, method, iterations)
```

Nine-band order is:

```text
noise, ultra_deep_blacks, deep_blacks, blacks, shadows,
midtones, highlights, whites, speculars
```

`pack_tone_equalizer_v2` supplies the verified 5.2.1 tail values. Pass exactly
nine zone adjustments. Avoid abrupt sign changes between neighboring bands;
they tend to create unnatural local contrast.

### RGB Curve v1 — 516 bytes

The blob stores 20 `(x, y)` float slots for each of three channels, then node
counts, curve types, autoscale, middle-grey compensation, and color preservation.
The bundled packer accepts one linked 2–20-node curve through `nodes`, or all of
`red`, `green`, and `blue` for independent-channel mode. It fills unused slots
with zero, enforces increasing x coordinates per channel, and asserts 516 bytes.

```python
curve = pack_rgb_curve_v1([
    (0.0, 0.0), (0.12, 0.15), (0.50, 0.56), (0.88, 0.91), (1.0, 1.0),
])
```

### Profiled Denoise (`denoiseprofile`) v12 — 416 bytes

The layout contains eight global floats, `a[3]`/`b[3]` camera-noise terms, the
mode, six channels of seven x/y wavelet points, and five compatibility/mode
integers. `pack_denoise_profile_v12` uses darktable's `a[0] = -1` auto-profile
marker by default. `repack_denoise_profile_v12` is preferred for an existing
same-image or v12 preset blob. Never reuse explicit camera noise terms from a
different image/camera.

### Diffuse or Sharpen (`diffuse`) v2 — 60 bytes

```python
struct.pack("<ifi11fi", iterations, sharpness, radius, regularization,
            variance_threshold, *four_anisotropy_orders, threshold,
            *four_speed_orders, radius_center)
```

The active-runtime preset database contains useful named behaviors for
sharpness, lens deblur, denoise, local contrast, dehaze, bloom, and other
effects. Prefer an exact preset followed by controlled overrides when that
communicates intent better than fourteen raw coefficients.

### Haze Removal (`hazeremoval`) v3 — 16 bytes

The layout is `<2f2i>`: strength, distance, compatibility mode, and adaptive
mode. Strength is `[-1,1]` and distance `[0,1]`.

### Crop (`crop`) v3 — 24 bytes

The layout is `<4f2i>`: normalized left, top, right, bottom, ratio numerator,
and ratio denominator. Each retained dimension must be at least 0.01.

### Perspective Correction (`ashift`) v5 — 892 bytes

The layout contains rotation, vertical/horizontal lens shift, shear, focal
length, crop factor, lens dependence, aspect, mode, auto-crop mode, normalized
crop box, up to 50 four-coordinate saved guide lines, the line count, and an
eight-coordinate quadrilateral. `pack_perspective_v5` initializes unused guides
to zero. This is distinct from the crop module; a complete geometry recipe may
use both.

### Color Equalizer v4 — 128 bytes

Header: six floats plus `use_filter`; then eight saturation floats, eight hue
floats, eight brightness floats, and one global hue-shift float. The hue-family
order is fixed:

```text
red, orange, yellow, green, cyan, blue, lavender, magenta
```

Neutral saturation/brightness is `1.0`; neutral hue is `0.0` degrees. Use it
for colors already present in the image, not to simulate illumination across
otherwise unrelated objects.

### Color Balance RGB v5 — 132 bytes

The binary form is `struct.pack("<32fi", *params, saturation_formula)`. The
bundled packer starts from verified neutral fulcrums and weights and accepts
these named override fields in binary order:

```text
shadows_Y, shadows_C, shadows_H
midtones_Y, midtones_C, midtones_H
highlights_Y, highlights_C, highlights_H
global_Y, global_C, global_H
shadows_weight, white_fulcrum, highlights_weight
chroma_shadows, chroma_highlights, chroma_global, chroma_midtones
saturation_global, saturation_highlights, saturation_midtones, saturation_shadows
hue_angle
brilliance_global, brilliance_highlights, brilliance_midtones, brilliance_shadows
mask_grey_fulcrum, vibrance, grey_fulcrum, contrast
```

Prefer `overrides={...}` over manipulating raw indexes. Hue fields use degrees.
The verified `saturation_formula` is `1`. Keep Y/brilliance neutral when a stage
is intended to change color without moving approved luminance.

### Basic Adjustments v2 — 44 bytes

```python
struct.pack("<5fi5f", black_point, exposure, hlcompr, hlcomprthresh,
            contrast, preserve_colors, middle_grey, brightness, saturation,
            vibrance, clip)
```

The bundled defaults include `preserve_colors=1`, `middle_grey=18.42`, and
`clip=0.01`. This module is display-referred. For portrait local light, make a
small exposure/brightness adjustment and attach a feathered drawn mask rather
than pushing global exposure.

### Flip v2 — 4 bytes

The blob is one signed integer. `pack_flip_v2` intentionally requires
`mapping_verified=True`; it does not claim that a value means a particular
rotation. Prefer inheriting orientation from the same image. If writing a raw
enum, render and visually confirm it first.

## Masks and blend parameters

The covered releases use a 420-byte blend v14 blob. Copy a known-good neutral
blob from current-image history or the active runtime's `data.db` preset, then
use `make_mask_blend` to change the verified mask, colorspace, opacity,
parametric-channel, and refinement fields. `make_drawn_mask_blend` remains a
compatibility wrapper. `load_blend_template(library_db, imgid=...,
data_db=...)` tries history first and falls back to presets; at least one database
must be provided. The verified blend color space enum is:

```text
NONE=0, RAW=1, LAB=2, RGB_DISPLAY=3, RGB_SCENE=4
```

Example for a display-referred Basic Adjustments mask:

```python
from photo_xmp.xmp import (
    BLEND_CS_RGB_DISPLAY, HistoryEntry, MaskEntry,
    load_blend_template, make_mask_blend,
    pack_basic_adjustments_v2, pack_ellipse_mask_v6,
    pack_mask_group_v6, write_xmp,
)

blend = load_blend_template("config/library.db", imgid=1)
history_index = len(entries)
child_id = 4101
group_id = 4199
masks = [
    MaskEntry(history_index, child_id, 32, "broad subject light field",
              pack_ellipse_mask_v6(0.50, 0.52, 0.24, 0.36, border=0.22), 1),
    MaskEntry(history_index, group_id, 4, "subject local light",
              pack_mask_group_v6([child_id], group_id), 1),
]
entries.append(HistoryEntry(
    "basicadj", 2,
    pack_basic_adjustments_v2(exposure=0.08, brightness=0.03),
    name="subject local light",
    blend_params=make_mask_blend(
        blend, mask_id=group_id, blend_colorspace=BLEND_CS_RGB_DISPLAY
    ),
))
write_xmp("xmp/05-subject.xmp", source_name="photo.jpg",
          entries=entries, default_blend=blend, masks=masks)
```

The coordinates above only demonstrate normalized geometry; calculate real
coordinates for the actual photograph. For broad subject lighting, make one
ellipse larger than the person, keep its transition off the face and silhouette,
and use generous feathering. Do not replace a natural light field with separate
head/body/limb patches merely to trace the subject.

Ellipse mask type is `32`, group type is `4`, and mask version is `6`. Group
children use state `3` for the first member and union state `11` for later
members. The bundled packer handles these states. Multiple children and
per-child `opacities` are appropriate when regions genuinely need different
corrections; values must be in `[0, 1]`. Optional `states` likewise requires one
signed integer per child; keep the verified defaults unless another combination
has been tested.

Additional v6 packers cover gradient (`16`, one `<6fi>` point), closed Bézier
path (`2`, repeated `<8fi>` points), and brush (`64`, repeated `<10fi>` points).
Path and brush corners use normalized input-image coordinates; omitted control
points use `-1` so darktable initializes automatic controls. Gradient state is
`1` linear or `2` sigmoidal. Every shape still needs visual geometry review.

Blend v14 uses `mask_mode` bits `1` enabled, `2` drawn, and `4` parametric. The
parametric mask stores sixteen channels of four normalized feather stops plus
per-channel boost factors. Active channels occupy the low sixteen `blendif`
bits; inverted channels occupy the corresponding high sixteen bits. Use
`BLEND_CHANNELS` and `make_mask_blend` instead of numeric offsets. The helper
supports parametric-only and drawn+parametric masks.

darktable 5.6's AI object prompts are transient; the durable XMP result is an
ordinary path/group tree. `photo-xmp mask subject` passes foreground/background
prompts to the active model through the installed darktable library, calls its
segmentation and raster-to-vector APIs, and emits a source-bound JSON path tree.
`--subject-mask` embeds that reviewed result. Recipe documents do not serialize
or replay inference as a static mask type.

`masks_history` is snapshot history, not a set of per-module buckets. darktable
replaces its current forms with the complete set attached to the greatest
`mask_num` below `history_end`. Therefore the final sidecar must place every
form reachable from every active drawn-mask module in one complete current
snapshot, normally attached to the last active drawn-mask history item. An older
module may reference a root stored in that later complete snapshot. Stable,
unique IDs are required within each snapshot. `photo-xmp` assembles this snapshot
for direct edits and recipes; callers should not assign mask history numbers.

Use `RGB_SCENE` only for a scene-referred module whose blend behavior has been
render-verified. Basic Adjustments requires `RGB_DISPLAY`. `BLEND_CS_NONE` is
always a failure for an intended drawn-mask adjustment.

## Stage construction pattern

1. Obtain or derive current-image baseline behavior; do not copy camera-specific
   input parameters from a different photo.
2. Load one 420-byte blend v14 template accepted by the active runtime,
   preferably from current-image library history or that runtime's `data.db`.
3. Create `HistoryEntry` objects with the bundled packers.
4. Write `01-basic.xmp` and render it.
5. Copy the accepted entry list, append only the next parameter family, and
   write the next stage.
6. After all masked entries are known, place every active mask tree in one
   complete final forms snapshot; `photo-xmp` does this automatically.
7. Keep technical baseline, selective color, creative grade, local repair, and
   final polish as separate reviewable sidecars.

The packers encode controls; they do not choose good photographic values. Use
the calling workflow's histogram, semantic-region, reference/style, and visual-review
workflow to choose those values.

## Validation

For every new or changed stage:

```bash
darktable-cli source/photo.jpg xmp/05-subject.xmp previews/05-subject.jpg \
  --width 1600 --height 0 --hq true --core --configdir config -d pipe \
  > analysis/05-subject.log 2>&1
```

Require all of the following:

- no `invalid parameters`, conversion failure, unknown module, or invalid mask;
- no parameter-size assertion failure in the builder;
- expected module versions appear in the pipeline;
- masked stages log `blend with form`;
- Basic Adjustments masks log `BLEND_CS_RGB_DISPLAY`;
- no intended masked stage logs `BLEND_CS_NONE`;
- the exported image changes in the expected region and not globally;
- a fresh final render reproduces the delivered pixels.

Open the final sidecar in darktable when practical. Successful XML parsing alone
does not prove that darktable accepted the binary parameters or applied a mask.

## When to inspect source

Source inspection remains a normal extension mechanism. It is especially useful
when at least one of these is true:

- the required module/version is not covered here;
- a runtime outside the render-tested list fails the module-level smoke test;
- darktable reports parameter conversion or invalid parameters despite correct
  input length;
- a needed blend mode or module-specific feature is outside the bundled helpers;
- a raw orientation enum must be established for a new case.

It is also acceptable to inspect a covered module when debugging a concrete
behavioral discrepancy. The efficiency rule is simply not to repeat the same
struct discovery by default when the installed version and validated behavior
already match this reference.

When extending support, verify the exact module struct and enum from that
version, add a length assertion and a small self-test to `photo_xmp.xmp`, add
the field mapping here, and render a minimal stage. Do not leave the discovery
only in a one-off work-directory script.
