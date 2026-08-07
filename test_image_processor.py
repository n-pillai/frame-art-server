#!/usr/bin/env python3
"""Tests for the image_processor.py pipeline.

Run with: python test_image_processor.py

Covers pure pixel-level logic that can be exercised with small, synthetic,
in-memory PIL images:
  - center_crop() cropping math (which side gets trimmed, exact dimensions)
  - process_image()'s aspect_mode handling, including the fallback for an
    unknown mode (crop + a logged warning)
  - sRGB conversion behaviour for images with no ICC profile, an
    already-sRGB profile, a malformed profile, and a non-RGB (RGBA) mode
  - a few other pure-logic pieces of the pipeline (matte color resolution,
    warmth adjustment, minimum-size/aspect rejection)

Deliberately out of scope: no network calls, no TV/samsungtvws interaction,
no real museum API fixtures, no multi-megabyte source images. Every image
used here is generated in memory and is at most a few hundred pixels per
side.
"""

import logging
import os
import sys
import tempfile

from PIL import Image, ImageCms

from image_processor import (
    center_crop,
    compute_matte_color,
    adjust_warmth,
    process_image,
    _convert_to_srgb,
)

PASSED = 0
FAILED = []


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL {name}{(' — ' + detail) if detail else ''}")


class _ListHandler(logging.Handler):
    """Collects log messages instead of printing them, for assertions."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


def _capture(logger_name, level, fn, *args, **kwargs):
    """Run fn and return (result, [captured log messages]) for logger_name."""
    target_logger = logging.getLogger(logger_name)
    handler = _ListHandler()
    target_logger.addHandler(handler)
    old_level = target_logger.level
    target_logger.setLevel(level)
    try:
        result = fn(*args, **kwargs)
    finally:
        target_logger.removeHandler(handler)
        target_logger.setLevel(old_level)
    return result, handler.records


def _two_tone(size, top_left_color, bottom_right_color, split):
    """Build an RGB image split into two solid blocks along one axis.

    split=(axis, cut) where axis is 'x' or 'y' and cut is the pixel offset
    of the boundary between the two blocks.
    """
    w, h = size
    img = Image.new("RGB", size)
    axis, cut = split
    if axis == "x":
        img.paste(Image.new("RGB", (cut, h), top_left_color), (0, 0))
        img.paste(Image.new("RGB", (w - cut, h), bottom_right_color), (cut, 0))
    else:
        img.paste(Image.new("RGB", (w, cut), top_left_color), (0, 0))
        img.paste(Image.new("RGB", (w, h - cut), bottom_right_color), (0, cut))
    return img


def _save_temp(img, tmpdir, name):
    path = os.path.join(tmpdir, name)
    img.save(path)
    return path


# ---------------------------------------------------------------------------
# center_crop — pure cropping/resizing math
# ---------------------------------------------------------------------------

def test_center_crop_wide_image_crops_sides():
    # 400x200 (aspect 2.0), left half red / right half blue.
    img = _two_tone((400, 200), (255, 0, 0), (0, 0, 255), split=("x", 200))
    # Target aspect 1.0 -> art is wider, so sides get cropped.
    # new_w = art_h * target_aspect = 200, left offset = (400-200)//2 = 100.
    # Crop region is x in [100, 300): half red (100-200), half blue (200-300).
    out = center_crop(img, 200, 200)
    check("wide crop produces exact target size", out.size == (200, 200), str(out.size))
    check("wide crop left region is red", out.getpixel((20, 100)) == (255, 0, 0))
    check("wide crop right region is blue", out.getpixel((180, 100)) == (0, 0, 255))
    check("wide crop boundary sits at the true center",
          out.getpixel((95, 100)) == (255, 0, 0) and out.getpixel((105, 100)) == (0, 0, 255))


def test_center_crop_tall_image_crops_top_bottom():
    # 200x400 (aspect 0.5), top half green / bottom half yellow.
    img = _two_tone((200, 400), (0, 255, 0), (255, 255, 0), split=("y", 200))
    out = center_crop(img, 200, 200)
    check("tall crop produces exact target size", out.size == (200, 200), str(out.size))
    check("tall crop top region is green", out.getpixel((100, 20)) == (0, 255, 0))
    check("tall crop bottom region is yellow", out.getpixel((100, 180)) == (255, 255, 0))


def test_center_crop_matching_aspect_only_resizes():
    # Source already has the target aspect ratio (2:1) -> no cropping, just resize.
    img = _two_tone((400, 200), (10, 20, 30), (10, 20, 30), split=("x", 200))
    out = center_crop(img, 100, 50)
    check("matching-aspect crop resizes to target", out.size == (100, 50), str(out.size))
    check("matching-aspect crop preserves solid color", out.getpixel((50, 25)) == (10, 20, 30))


# ---------------------------------------------------------------------------
# process_image — aspect_mode handling, including the unknown-mode fallback
# ---------------------------------------------------------------------------

def test_unknown_aspect_mode_falls_back_to_crop_with_warning():
    with tempfile.TemporaryDirectory() as tmpdir:
        src = _save_temp(Image.new("RGB", (300, 150), (100, 120, 140)), tmpdir, "src.png")
        out_bogus = os.path.join(tmpdir, "out_bogus.jpg")
        out_crop = os.path.join(tmpdir, "out_crop.jpg")

        common_kwargs = dict(
            target_resolution=(160, 100), min_width=10, min_height=10,
            sharpen=False, warmth_adjust=0,
        )

        result_bogus, warnings = _capture(
            "frame_art.processor", logging.WARNING,
            process_image, src, out_bogus, aspect_mode="totally-bogus", **common_kwargs,
        )
        result_crop = process_image(src, out_crop, aspect_mode="crop", **common_kwargs)

        check("unknown aspect_mode still produces output", result_bogus is not None)
        check("a warning is logged for the unknown mode",
              any("Unknown aspect_mode" in w and "totally-bogus" in w for w in warnings),
              str(warnings))
        check("unknown aspect_mode output is byte-identical to explicit crop",
              result_bogus is not None and result_crop is not None
              and open(out_bogus, "rb").read() == open(out_crop, "rb").read())


def test_matte_mode_is_not_silently_treated_as_crop():
    # Sanity check that "matte" is a real, distinct mode (not swallowed by
    # the unknown-mode fallback) -- a non-16:9 source gets a visible matte
    # border, so it should NOT be byte-identical to the crop output.
    with tempfile.TemporaryDirectory() as tmpdir:
        src = _save_temp(Image.new("RGB", (300, 150), (100, 120, 140)), tmpdir, "src.png")
        out_matte = os.path.join(tmpdir, "out_matte.jpg")
        out_crop = os.path.join(tmpdir, "out_crop.jpg")
        common_kwargs = dict(
            target_resolution=(160, 100), min_width=10, min_height=10,
            sharpen=False, warmth_adjust=0,
        )
        process_image(src, out_matte, aspect_mode="matte", matte_color_config="black", **common_kwargs)
        process_image(src, out_crop, aspect_mode="crop", **common_kwargs)
        check("matte output differs from crop output",
              open(out_matte, "rb").read() != open(out_crop, "rb").read())


def test_rejects_image_below_minimum_dimensions():
    with tempfile.TemporaryDirectory() as tmpdir:
        src = _save_temp(Image.new("RGB", (50, 50), (1, 2, 3)), tmpdir, "small.png")
        out = os.path.join(tmpdir, "out.jpg")
        result = process_image(src, out, min_width=100, min_height=100)
        check("image below minimum dimensions is rejected", result is None)
        check("no output file written on rejection", not os.path.exists(out))


def test_rejects_non_landscape_image_in_crop_mode():
    with tempfile.TemporaryDirectory() as tmpdir:
        src = _save_temp(Image.new("RGB", (100, 200), (1, 2, 3)), tmpdir, "portrait.png")
        out = os.path.join(tmpdir, "out.jpg")
        result = process_image(src, out, aspect_mode="crop", min_width=10, min_height=10)
        check("portrait image in crop mode is rejected", result is None)


# ---------------------------------------------------------------------------
# sRGB conversion — non-RGB / non-sRGB inputs
# ---------------------------------------------------------------------------

def test_convert_to_srgb_no_icc_profile_is_a_noop():
    img = Image.new("RGB", (10, 10), (100, 150, 200))
    out = _convert_to_srgb(img)
    check("no icc_profile returns the same object unchanged", out is img)


def test_convert_to_srgb_already_srgb_profile_is_skipped():
    profile = ImageCms.createProfile("sRGB")
    icc_bytes = ImageCms.ImageCmsProfile(profile).tobytes()
    img = Image.new("RGB", (10, 10), (5, 6, 7))
    img.info["icc_profile"] = icc_bytes
    out = _convert_to_srgb(img)
    check("a profile whose description already says sRGB is not transformed",
          out is img)


def test_convert_to_srgb_malformed_icc_bytes_does_not_raise():
    img = Image.new("RGB", (10, 10), (100, 150, 200))
    img.info["icc_profile"] = b"not a real icc profile"
    result, warnings = _capture(
        "frame_art.processor", logging.WARNING, _convert_to_srgb, img,
    )
    check("malformed icc bytes do not raise", result is not None)
    check("pixels fall back unchanged on malformed profile",
          result.getpixel((0, 0)) == (100, 150, 200))
    check("a warning is logged for the malformed profile",
          any("ICC profile conversion failed" in w for w in warnings), str(warnings))


def test_convert_to_srgb_non_rgb_mode_input_does_not_crash():
    # RGBA input with an embedded (non-sRGB) profile that can't be
    # transformed against an RGBA image -- must degrade gracefully rather
    # than raising, since process_image() calls this before mode conversion.
    profile = ImageCms.createProfile("LAB")
    icc_bytes = ImageCms.ImageCmsProfile(profile).tobytes()
    img = Image.new("RGBA", (10, 10), (10, 20, 30, 255))
    img.info["icc_profile"] = icc_bytes
    result, warnings = _capture(
        "frame_art.processor", logging.WARNING, _convert_to_srgb, img,
    )
    check("RGBA input with an incompatible profile still returns an image",
          result is not None)
    check("RGBA mode is preserved for the later mode-conversion step",
          result.mode == "RGBA")


def test_process_image_converts_palette_mode_to_rgb():
    with tempfile.TemporaryDirectory() as tmpdir:
        src = _save_temp(
            Image.new("RGB", (300, 150), (80, 160, 200)).convert("P"), tmpdir, "p.png",
        )
        out = os.path.join(tmpdir, "out.jpg")
        result = process_image(src, out, aspect_mode="crop", min_width=10, min_height=10,
                                target_resolution=(160, 100))
        check("palette-mode source is processed successfully", result is not None)
        check("palette-mode source is converted to RGB in the output",
              result is not None and Image.open(out).mode == "RGB")


def test_process_image_converts_grayscale_to_rgb():
    with tempfile.TemporaryDirectory() as tmpdir:
        src = _save_temp(Image.new("L", (300, 150), 128), tmpdir, "l.png")
        out = os.path.join(tmpdir, "out.jpg")
        result = process_image(src, out, aspect_mode="crop", min_width=10, min_height=10,
                                target_resolution=(160, 100))
        check("grayscale source is processed successfully", result is not None)
        check("grayscale source is converted to RGB in the output",
              result is not None and Image.open(out).mode == "RGB")


def test_process_image_converts_rgba_to_rgb_dropping_alpha():
    with tempfile.TemporaryDirectory() as tmpdir:
        src = _save_temp(Image.new("RGBA", (300, 150), (10, 20, 30, 128)), tmpdir, "rgba.png")
        out = os.path.join(tmpdir, "out.jpg")
        result = process_image(src, out, aspect_mode="crop", min_width=10, min_height=10,
                                target_resolution=(160, 100), sharpen=False)
        check("RGBA source is processed successfully", result is not None)
        if result is not None:
            saved = Image.open(out)
            check("RGBA source is converted to RGB in the output", saved.mode == "RGB")
            check("RGB channel values survive the alpha drop",
                  saved.getpixel((5, 5)) == (10, 20, 30), str(saved.getpixel((5, 5))))


# ---------------------------------------------------------------------------
# Other pure-logic pieces of the pipeline
# ---------------------------------------------------------------------------

def test_compute_matte_color_hex():
    color = compute_matte_color(Image.new("RGB", (4, 4)), "#112233")
    check("hex matte color parses to the right RGB tuple", color == (17, 34, 51), str(color))


def test_compute_matte_color_named_palette():
    color = compute_matte_color(Image.new("RGB", (4, 4)), "warm")
    check("named palette entry resolves correctly", color == (55, 48, 42), str(color))


def test_compute_matte_color_unknown_value_falls_back_to_neutral():
    color = compute_matte_color(Image.new("RGB", (4, 4)), "not-a-real-option")
    check("unknown matte_color config falls back to neutral", color == (45, 45, 45), str(color))


def test_adjust_warmth_zero_is_identity():
    img = Image.new("RGB", (10, 10), (100, 100, 100))
    out = adjust_warmth(img, 0)
    check("warmth=0 returns the same object", out is img)


def test_adjust_warmth_positive_boosts_red_reduces_blue():
    img = Image.new("RGB", (10, 10), (100, 100, 100))
    out = adjust_warmth(img, 20)
    check("positive warmth boosts red and reduces blue",
          out.getpixel((0, 0)) == (120, 100, 90), str(out.getpixel((0, 0))))


def test_adjust_warmth_negative_boosts_blue_reduces_red():
    img = Image.new("RGB", (10, 10), (100, 100, 100))
    out = adjust_warmth(img, -20)
    check("negative warmth boosts blue and reduces red",
          out.getpixel((0, 0)) == (90, 100, 120), str(out.getpixel((0, 0))))


def main():
    tests = [
        test_center_crop_wide_image_crops_sides,
        test_center_crop_tall_image_crops_top_bottom,
        test_center_crop_matching_aspect_only_resizes,
        test_unknown_aspect_mode_falls_back_to_crop_with_warning,
        test_matte_mode_is_not_silently_treated_as_crop,
        test_rejects_image_below_minimum_dimensions,
        test_rejects_non_landscape_image_in_crop_mode,
        test_convert_to_srgb_no_icc_profile_is_a_noop,
        test_convert_to_srgb_already_srgb_profile_is_skipped,
        test_convert_to_srgb_malformed_icc_bytes_does_not_raise,
        test_convert_to_srgb_non_rgb_mode_input_does_not_crash,
        test_process_image_converts_palette_mode_to_rgb,
        test_process_image_converts_grayscale_to_rgb,
        test_process_image_converts_rgba_to_rgb_dropping_alpha,
        test_compute_matte_color_hex,
        test_compute_matte_color_named_palette,
        test_compute_matte_color_unknown_value_falls_back_to_neutral,
        test_adjust_warmth_zero_is_identity,
        test_adjust_warmth_positive_boosts_red_reduces_blue,
        test_adjust_warmth_negative_boosts_blue_reduces_red,
    ]
    for test in tests:
        print(f"\n{test.__name__}:")
        test()

    print(f"\n{PASSED} passed, {len(FAILED)} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
