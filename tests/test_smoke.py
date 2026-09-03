from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from photo_xmp import xmp
from photo_xmp.cli import capabilities, companion_tool_status, inspect_xmp, validate_xmp


class PhotoXmpSmokeTests(unittest.TestCase):
    def test_companion_tool_status_has_stable_machine_readable_shape(self) -> None:
        result = companion_tool_status()
        self.assertEqual(
            set(result),
            {
                "uv", "exiftool", "imagemagick", "fontconfig",
                "gmic", "cjk_font", "imagemagick_font_registry",
            },
        )
        for name in ("uv", "exiftool", "imagemagick", "fontconfig", "gmic"):
            self.assertIn("available", result[name])
            self.assertFalse(result[name]["required_for_cli"])
        self.assertIn("font_count", result["imagemagick_font_registry"])

    def test_packer_self_test(self) -> None:
        xmp._self_test()

    def test_capabilities_expose_advanced_modules_and_masks(self) -> None:
        result = capabilities()
        modules = result["direct_xmp"]["modules"]
        masks = result["direct_xmp"]["masks"]
        self.assertIn("profiled denoise v12", modules)
        self.assertIn("diffuse or sharpen v2", modules)
        self.assertIn("perspective correction v5", modules)
        self.assertIn("parametric masks", masks)
        self.assertIn(
            "darktable-native prompted subject segmentation finalized as editable paths",
            masks,
        )
        self.assertEqual(
            result["named_fields"]["color_equalizer_units_and_neutral"],
            {
                "saturation": "multiplier; neutral 1.0",
                "brightness": "multiplier; neutral 1.0",
                "hue": "degree offset; neutral 0.0",
            },
        )

    def test_color_equalizer_help_explains_units_and_neutral_values(self) -> None:
        completed = subprocess.run(
            [
                sys.executable, "-m", "photo_xmp", "edit",
                "color-equalizer", "--help",
            ],
            text=True, capture_output=True, check=True,
        )
        self.assertIn("saturation multiplier; neutral is 1.0", completed.stdout)
        self.assertIn("brightness multiplier; neutral is 1.0", completed.stdout)
        self.assertIn("degree offset; neutral is 0.0", completed.stdout)

    def test_render_help_defaults_to_source_dimensions(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "photo_xmp", "render", "--help"],
            text=True, capture_output=True, check=True,
        )
        self.assertIn("maximum output width; 0 preserves the source width", completed.stdout)
        self.assertIn("maximum output height; 0 preserves the source height", completed.stdout)
        self.assertGreaterEqual(completed.stdout.count("(default: 0)"), 2)

    def test_cli_builds_and_validates_masked_xmp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            output = root / "masked.xmp"
            Image.new("RGB", (64, 48), (128, 144, 160)).save(source)
            completed = subprocess.run(
                [
                    sys.executable, "-m", "photo_xmp", "edit",
                    "basic-adjustments", "--source", str(source),
                    "--brightness", "0.04", "--ellipse",
                    "0.5", "0.5", "0.25", "0.35", "0", "0.2",
                    "--parametric", "lightness_in=0.1,0.2,0.8,0.9",
                    "--mask-opacity", "75", "--output", str(output),
                ],
                text=True, capture_output=True, check=True,
            )
            result = json.loads(completed.stdout)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(validate_xmp(output)["status"], "ok")
            inspected = inspect_xmp(output)
            self.assertEqual(inspected["history"][0]["operation"], "basicadj")
            self.assertEqual(len(inspected["masks"]), 2)

    def test_sequential_local_edits_write_one_complete_final_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jpg"
            Image.new("RGB", (96, 72), (96, 128, 160)).save(source)
            stages = [
                ("color-equalizer", ["--brightness", "orange=1.05"]),
                ("basic-adjustments", ["--brightness", "0.04"]),
                ("exposure", ["--exposure", "0.15"]),
            ]
            parent = None
            for index, (command, parameters) in enumerate(stages, 1):
                output = root / f"stage-{index}.xmp"
                invocation = [
                    sys.executable, "-m", "photo_xmp", "edit", command,
                    *(
                        ["--source", str(source)] if parent is None
                        else ["--input-xmp", str(parent)]
                    ),
                    *parameters, "--ellipse", "0.5", "0.5", "0.2",
                    "0.3", "0", "0.15", "--output", str(output),
                ]
                subprocess.run(invocation, text=True, capture_output=True, check=True)
                parent = output

            report = validate_xmp(parent)
            self.assertEqual(report["status"], "ok", report)
            inspected = inspect_xmp(parent)
            self.assertEqual(len(inspected["masks"]), 6)
            self.assertEqual(
                {mask["history_index"] for mask in inspected["masks"]}, {2}
            )
            self.assertEqual(
                {item["blend"]["drawn_mask_id"] for item in inspected["history"]},
                {1001, 1003, 1005},
            )

    def test_validation_rejects_fragmented_final_mask_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "fragmented.xmp"
            blend = xmp.neutral_blend_v14()
            entries = [
                xmp.HistoryEntry(
                    "colorequal", 4, xmp.pack_color_equalizer_v4(),
                    blend_params=xmp.make_drawn_mask_blend(
                        blend, 1001, blend_colorspace=xmp.BLEND_CS_RGB_SCENE
                    ),
                ),
                xmp.HistoryEntry(
                    "basicadj", 2, xmp.pack_basic_adjustments_v2(brightness=0.03),
                    blend_params=xmp.make_drawn_mask_blend(
                        blend, 1003, blend_colorspace=xmp.BLEND_CS_RGB_DISPLAY
                    ),
                ),
            ]
            masks = [
                xmp.MaskEntry(0, 1000, xmp.MASK_ELLIPSE, "first", xmp.pack_ellipse_mask_v6(0.3, 0.5, 0.1, 0.2), 1),
                xmp.MaskEntry(0, 1001, xmp.MASK_GROUP, "first group", xmp.pack_mask_group_v6([1000], 1001), 1),
                xmp.MaskEntry(1, 1002, xmp.MASK_ELLIPSE, "second", xmp.pack_ellipse_mask_v6(0.7, 0.5, 0.1, 0.2), 1),
                xmp.MaskEntry(1, 1003, xmp.MASK_GROUP, "second group", xmp.pack_mask_group_v6([1002], 1003), 1),
            ]
            xmp.write_xmp(
                output, source_name="source.jpg", entries=entries,
                default_blend=blend, masks=masks, darktable_version="5.6.1",
            )
            report = validate_xmp(output)
            self.assertEqual(report["status"], "invalid")
            self.assertTrue(
                any("drawn mask group 1001 is missing" in error for error in report["errors"]),
                report,
            )


if __name__ == "__main__":
    unittest.main()
