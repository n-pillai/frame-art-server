"""
image_processor.py — Gallery-quality image processing for Samsung Frame TV.

Handles:
  - Intelligent resizing to 3840x2160 (or 1920x1080 for 32")
  - Aspect ratio preservation with museum-quality matte borders
  - Color-matched matte generation (samples dominant colors from artwork)
  - Subtle sharpening for 4K clarity
  - Color temperature adjustment to match the Frame TV's warm display
  - Museum-style metadata overlay (title + artist)
"""

import logging
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageEnhance, ImageStat, ImageFont, ImageCms
import io

logger = logging.getLogger("frame_art.processor")

# ---------------------------------------------------------------------------
# sRGB color profile conversion
# ---------------------------------------------------------------------------
# Samsung Frame TV auto-applies a mat border when it detects non-sRGB pixel
# data.  Museum source images frequently arrive in Adobe RGB, ProPhoto RGB,
# or other wide-gamut spaces.  We must convert the actual pixel values to
# sRGB — simply stripping the ICC tag leaves the pixels in the wrong space
# and colors look shifted on the TV.

_SRGB_PROFILE = ImageCms.createProfile("sRGB")


def _convert_to_srgb(img: Image.Image) -> Image.Image:
    """
    Convert an image to sRGB color space if it has an embedded ICC profile.
    If no profile is embedded, assume pixels are already sRGB (the web default).
    Returns an RGB image with pixel values in sRGB space.
    """
    icc_raw = img.info.get("icc_profile")
    if not icc_raw:
        return img  # No profile → assume sRGB already

    try:
        src_profile = ImageCms.ImageCmsProfile(io.BytesIO(icc_raw))
        # Check if it's already sRGB by comparing the profile description
        try:
            desc = ImageCms.getProfileDescription(src_profile).strip().lower()
            if "srgb" in desc or "sRGB" in desc:
                logger.debug("Image already in sRGB profile, skipping conversion")
                return img
        except Exception:
            pass  # Can't read description — convert to be safe

        # Build a transform from whatever-the-source-is → sRGB
        transform = ImageCms.buildTransformFromOpenProfiles(
            src_profile, _SRGB_PROFILE, img.mode, "RGB",
            renderingIntent=ImageCms.Intent.PERCEPTUAL,
        )
        converted = ImageCms.applyTransform(img, transform)
        logger.info(f"Converted color profile to sRGB (source: {desc if 'desc' in dir() else 'unknown'})")
        return converted
    except Exception as e:
        logger.warning(f"ICC profile conversion failed ({e}), using pixels as-is")
        return img


# ---------------------------------------------------------------------------
# Matte color palettes
# ---------------------------------------------------------------------------
MATTE_PALETTES = {
    "neutral": (45, 45, 45),       # Dark charcoal — classic museum
    "warm": (55, 48, 42),          # Warm dark brown
    "cool": (40, 44, 50),          # Cool dark blue-gray
    "white": (245, 243, 240),      # Off-white / gallery white
    "cream": (235, 225, 210),      # Warm cream
    "black": (15, 15, 15),         # Near-black
}

# Frame TV display aspect ratio
TARGET_ASPECT = 16 / 9


def get_dominant_color(image: Image.Image, sample_size: int = 100) -> tuple:
    """Get the dominant color from the image edges for matte matching."""
    # Sample from the border region of the image
    w, h = image.size
    border = 20  # pixels from edge to sample

    # Collect border pixels
    pixels = []
    small = image.resize((sample_size, sample_size))
    for x in range(sample_size):
        for y in range(sample_size):
            # Only sample border regions
            if (
                x < border * sample_size // w
                or x > sample_size - border * sample_size // w
                or y < border * sample_size // h
                or y > sample_size - border * sample_size // h
            ):
                pixels.append(small.getpixel((x, y))[:3])

    if not pixels:
        return MATTE_PALETTES["neutral"]

    # Average the border colors and darken for matte
    r = sum(p[0] for p in pixels) // len(pixels)
    g = sum(p[1] for p in pixels) // len(pixels)
    b = sum(p[2] for p in pixels) // len(pixels)

    # Darken to ~25% brightness for a tasteful matte
    factor = 0.25
    return (int(r * factor), int(g * factor), int(b * factor))


def compute_matte_color(image: Image.Image, matte_color_config: str) -> tuple:
    """Determine the matte color based on config."""
    if matte_color_config.startswith("#"):
        # Hex color
        hex_color = matte_color_config.lstrip("#")
        return tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    elif matte_color_config == "auto":
        return get_dominant_color(image)
    elif matte_color_config in MATTE_PALETTES:
        return MATTE_PALETTES[matte_color_config]
    else:
        return MATTE_PALETTES["neutral"]


def add_gallery_matte(
    image: Image.Image,
    target_w: int,
    target_h: int,
    matte_color: tuple,
) -> Image.Image:
    """
    Place the artwork centered on a matte background, preserving aspect ratio.
    Adds a subtle inner shadow for depth — like a real gallery frame.
    """
    canvas = Image.new("RGB", (target_w, target_h), matte_color)

    # Calculate the maximum size for the art within the matte
    # Leave a minimum border of 4% on each side for the matte to be visible
    min_border_pct = 0.04
    max_art_w = int(target_w * (1 - 2 * min_border_pct))
    max_art_h = int(target_h * (1 - 2 * min_border_pct))

    # Scale art to fit within the available space
    art_w, art_h = image.size
    art_aspect = art_w / art_h

    if art_aspect > max_art_w / max_art_h:
        # Art is wider — fit to width
        new_w = max_art_w
        new_h = int(new_w / art_aspect)
    else:
        # Art is taller — fit to height
        new_h = max_art_h
        new_w = int(new_h * art_aspect)

    # High-quality resize
    resized = image.resize((new_w, new_h), Image.LANCZOS)

    # Center on canvas
    x_offset = (target_w - new_w) // 2
    y_offset = (target_h - new_h) // 2
    canvas.paste(resized, (x_offset, y_offset))

    # Add subtle inner shadow around the art for depth
    draw = ImageDraw.Draw(canvas)
    shadow_color = tuple(max(0, c - 15) for c in matte_color)

    # Top shadow (thin line above art)
    if y_offset > 2:
        draw.rectangle(
            [x_offset, y_offset - 2, x_offset + new_w, y_offset],
            fill=shadow_color,
        )
    # Left shadow
    if x_offset > 2:
        draw.rectangle(
            [x_offset - 2, y_offset, x_offset, y_offset + new_h],
            fill=shadow_color,
        )

    # Bottom and right get lighter highlight for 3D effect
    highlight = tuple(min(255, c + 10) for c in matte_color)
    draw.rectangle(
        [x_offset, y_offset + new_h, x_offset + new_w, y_offset + new_h + 2],
        fill=highlight,
    )
    draw.rectangle(
        [x_offset + new_w, y_offset, x_offset + new_w + 2, y_offset + new_h],
        fill=highlight,
    )

    return canvas


# ---------------------------------------------------------------------------
# Metadata overlay — museum-style label
# ---------------------------------------------------------------------------

# Font search paths (works on Windows, Pi, Ubuntu, macOS)
_FONT_SEARCH = [
    # Windows
    "C:/Windows/Fonts/times.ttf",
    "C:/Windows/Fonts/georgia.ttf",
    "C:/Windows/Fonts/garamond.ttf",
    # Linux
    "/usr/share/fonts/truetype/google-fonts/Lora-Variable.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
]

_FONT_ITALIC_SEARCH = [
    # Windows
    "C:/Windows/Fonts/timesi.ttf",
    "C:/Windows/Fonts/georgiai.ttf",
    # Linux
    "/usr/share/fonts/truetype/google-fonts/Lora-Italic-Variable.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Times New Roman Italic.ttf",
]


def _load_font(paths: list, size: int) -> ImageFont.FreeTypeFont:
    """Try loading a font from a list of paths, fall back to default."""
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _sample_matte_region(image: Image.Image, region: str = "bottom_right") -> tuple:
    """Sample the average color from a region of the matte to match label tint."""
    w, h = image.size
    if region == "bottom_right":
        box = (w * 3 // 4, h - h // 8, w - 20, h - 20)
    elif region == "bottom_left":
        box = (20, h - h // 8, w // 4, h - 20)
    else:
        box = (w * 3 // 4, h - h // 8, w - 20, h - 20)

    crop = image.crop(box)
    stat = ImageStat.Stat(crop)
    return tuple(int(c) for c in stat.mean[:3])


def add_metadata_overlay(
    image: Image.Image,
    title: str = "",
    artist: str = "",
    date: str = "",
    museum: str = "",
    position: str = "top_left",
    opacity: float = 0.90,
) -> Image.Image:
    """
    Add a museum-style metadata label directly on the painting.

    Uses a subtle dark gradient scrim behind the text so it's readable
    over any painting content. Designed for crop-to-fill mode where
    there's no matte — the label sits in a corner of the artwork itself.

    Font sizes are tuned for a 65" TV viewed from ~8 feet.

    Args:
        image:    The processed image (cropped to fill the screen)
        title:    Painting title (displayed in italic)
        artist:   Artist name
        date:     Date string (e.g., "ca. 1665–67")
        museum:   Museum or institution name
        position: "top_left" or "top_right"
        opacity:  Label text opacity (0.0–1.0)
    """
    if not title and not artist:
        return image

    img = image.copy()
    w, h = img.size

    # Font sizes tuned for 65" TV at 4K — readable from 8 feet
    # At 4K (2160px): title ~40px, artist ~34px, museum ~28px
    title_size = max(28, int(h * 0.019))
    artist_size = max(24, int(h * 0.016))
    museum_size = max(20, int(h * 0.013))

    title_font = _load_font(_FONT_ITALIC_SEARCH, title_size)
    artist_font = _load_font(_FONT_SEARCH, artist_size)
    museum_font = _load_font(_FONT_SEARCH, museum_size)

    # Always use white text on the dark scrim
    text_rgba = (255, 255, 255, int(255 * opacity))
    museum_rgba = (255, 255, 255, int(255 * opacity * 0.75))

    # Build the label text lines
    lines = []
    if title:
        lines.append(("title", title))
    if artist:
        artist_line = artist
        if date:
            artist_line += f",  {date}"
        lines.append(("artist", artist_line))
    if museum:
        lines.append(("museum", museum))

    # Create an overlay with alpha channel
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Measure text to position it
    line_spacing = int(title_size * 0.45)
    total_height = 0
    max_line_width = 0
    line_metrics = []
    for kind, text in lines:
        if kind == "title":
            font = title_font
        elif kind == "museum":
            font = museum_font
        else:
            font = artist_font
        bbox = draw.textbbox((0, 0), text, font=font)
        lw = bbox[2] - bbox[0]
        lh = bbox[3] - bbox[1]
        line_metrics.append((lw, lh))
        max_line_width = max(max_line_width, lw)
        total_height += lh + line_spacing
    total_height -= line_spacing  # no trailing space

    # Margin from edges — 3% of width
    margin = int(w * 0.03)
    pad_x = int(w * 0.015)   # padding inside scrim
    pad_y = int(h * 0.012)

    # Position: top corners
    if position == "top_right":
        # Text right-aligned in top-right corner
        scrim_right = w - margin
        scrim_left = scrim_right - max_line_width - 2 * pad_x
        scrim_top = margin
        scrim_bottom = scrim_top + total_height + 2 * pad_y
        x_anchor = scrim_right - pad_x
        y_start = scrim_top + pad_y
        align = "right"
    else:
        # Text left-aligned in top-left corner
        scrim_left = margin
        scrim_right = scrim_left + max_line_width + 2 * pad_x
        scrim_top = margin
        scrim_bottom = scrim_top + total_height + 2 * pad_y
        x_anchor = scrim_left + pad_x
        y_start = scrim_top + pad_y
        align = "left"

    # Draw a soft dark scrim (rounded rectangle feel via feathered edges)
    # Base scrim at ~35% opacity — enough to read but doesn't dominate
    scrim_opacity = int(255 * 0.35)
    # Draw with extra feather margin for soft edges
    feather = int(h * 0.008)
    for i in range(feather, 0, -1):
        alpha = int(scrim_opacity * (1 - i / feather) ** 1.5)
        draw.rounded_rectangle(
            [scrim_left - i, scrim_top - i, scrim_right + i, scrim_bottom + i],
            radius=int(h * 0.008),
            fill=(0, 0, 0, alpha),
        )
    # Core scrim
    draw.rounded_rectangle(
        [scrim_left, scrim_top, scrim_right, scrim_bottom],
        radius=int(h * 0.008),
        fill=(0, 0, 0, scrim_opacity),
    )

    # Draw each line of text
    y_cursor = y_start
    for i, (kind, text) in enumerate(lines):
        if kind == "title":
            font = title_font
        elif kind == "museum":
            font = museum_font
        else:
            font = artist_font
        lw, lh = line_metrics[i]

        if align == "right":
            x = x_anchor - lw
        else:
            x = x_anchor

        # Subtle text shadow for extra crispness
        shadow_color = (0, 0, 0, int(180 * opacity))
        draw.text((x + 2, y_cursor + 2), text, fill=shadow_color, font=font)

        # Museum line gets the more subdued color
        fill = museum_rgba if kind == "museum" else text_rgba
        draw.text((x, y_cursor), text, fill=fill, font=font)
        y_cursor += lh + line_spacing

    # Composite the overlay
    img = img.convert("RGBA")
    img = Image.alpha_composite(img, overlay)
    return img.convert("RGB")


def center_crop(image: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Center-crop to fill the target dimensions exactly."""
    art_w, art_h = image.size
    target_aspect = target_w / target_h
    art_aspect = art_w / art_h

    if art_aspect > target_aspect:
        # Art is wider — crop sides
        new_h = art_h
        new_w = int(art_h * target_aspect)
        left = (art_w - new_w) // 2
        image = image.crop((left, 0, left + new_w, new_h))
    else:
        # Art is taller — crop top/bottom
        new_w = art_w
        new_h = int(art_w / target_aspect)
        top = (art_h - new_h) // 2
        image = image.crop((0, top, new_w, top + new_h))

    return image.resize((target_w, target_h), Image.LANCZOS)


def adjust_warmth(image: Image.Image, warmth: int) -> Image.Image:
    """
    Adjust color temperature.
    warmth > 0: warmer (adds subtle red/yellow)
    warmth < 0: cooler (adds subtle blue)
    """
    if warmth == 0:
        return image

    r, g, b = image.split()

    if warmth > 0:
        # Warm: boost reds slightly, reduce blues slightly
        r = r.point(lambda x: min(255, x + warmth))
        b = b.point(lambda x: max(0, x - warmth // 2))
    else:
        # Cool: boost blues, reduce reds
        b = b.point(lambda x: min(255, x + abs(warmth)))
        r = r.point(lambda x: max(0, x - abs(warmth) // 2))

    return Image.merge("RGB", (r, g, b))


def process_image(
    input_path: str,
    output_path: str,
    target_resolution: tuple = (3840, 2160),
    aspect_mode: str = "crop",
    matte_color_config: str = "neutral",
    sharpen: bool = True,
    warmth_adjust: int = 0,
    jpeg_quality: int = 95,
    min_width: int = 1500,
    min_height: int = 1000,
    title: str = "",
    artist: str = "",
    date: str = "",
    museum: str = "",
    overlay_position: str = "bottom_right",
    overlay_opacity: float = 0.85,
) -> Optional[str]:
    """
    Full processing pipeline:
      load -> validate -> resize -> matte/crop -> sharpen -> overlay -> save.

    Returns the output path on success, None on failure.
    """
    try:
        img = Image.open(input_path)

        # Convert ICC color profile to sRGB BEFORE any mode conversion,
        # so the pixel values are correct for the Frame TV's sRGB display.
        img = _convert_to_srgb(img)

        # Convert to RGB if necessary (handles RGBA, palette, etc.)
        if img.mode != "RGB":
            img = img.convert("RGB")

        w, h = img.size
        logger.info(f"Processing: {input_path} ({w}x{h})")

        # Validate minimum dimensions
        if w < min_width or h < min_height:
            logger.warning(
                f"Image too small ({w}x{h}), minimum {min_width}x{min_height}"
            )
            return None

        # Unknown modes fall back to crop (no mat) rather than silently matting
        if aspect_mode not in ("crop", "matte", "stretch"):
            logger.warning(f"Unknown aspect_mode '{aspect_mode}' — defaulting to 'crop' (no mat)")
            aspect_mode = "crop"

        # Reject non-landscape images — they can't fill a 16:9 TV
        # Minimum aspect ratio of 1.3 (roughly 4:3) so the crop is gentle
        MIN_LANDSCAPE_ASPECT = 1.3
        if aspect_mode == "crop" and (w / h) < MIN_LANDSCAPE_ASPECT:
            logger.warning(
                f"Skipping non-landscape image ({w}x{h}, aspect {w/h:.2f}) — "
                f"needs >= {MIN_LANDSCAPE_ASPECT} for crop-to-fill"
            )
            return None

        target_w, target_h = target_resolution

        if aspect_mode == "crop":
            # Center-crop to fill the screen (no mat)
            processed = center_crop(img, target_w, target_h)
        elif aspect_mode == "stretch":
            # Simple stretch (not recommended)
            processed = img.resize((target_w, target_h), Image.LANCZOS)
        else:
            # Matte mode — preserve aspect ratio, add border
            art_aspect = w / h
            if abs(art_aspect - TARGET_ASPECT) < 0.05:
                # Close enough to 16:9 — just resize to fill
                processed = img.resize((target_w, target_h), Image.LANCZOS)
            else:
                matte_color = compute_matte_color(img, matte_color_config)
                processed = add_gallery_matte(img, target_w, target_h, matte_color)

        # Apply sharpening
        if sharpen:
            processed = processed.filter(ImageFilter.UnsharpMask(radius=1, percent=50, threshold=3))

        # Adjust warmth
        if warmth_adjust != 0:
            processed = adjust_warmth(processed, warmth_adjust)

        # Add metadata overlay (title + artist + museum)
        # With crop-to-fill, the label goes directly on the painting with a scrim
        if title or artist:
            processed = add_metadata_overlay(
                processed,
                title=title,
                artist=artist,
                date=date,
                museum=museum,
                position=overlay_position,
                opacity=overlay_opacity,
            )

        # Verify exact target dimensions — even 1 pixel off triggers the
        # Samsung Frame TV's auto-mat. Force-resize if rounding caused drift.
        if processed.size != (target_w, target_h):
            logger.warning(f"Dimension drift: {processed.size} -> forcing {target_w}x{target_h}")
            processed = processed.resize((target_w, target_h), Image.LANCZOS)

        # Strip ALL metadata — Samsung Frame TV adds a mat border when it
        # detects orientation flags, non-sRGB color profiles, or other EXIF
        # data.  We create a fresh image with only (now-sRGB) pixel data.
        clean = Image.new("RGB", processed.size)
        clean.putdata(list(processed.getdata()))

        # Save — explicitly exclude icc_profile and exif to guarantee
        # the file contains nothing but sRGB pixel data.
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        if output.suffix.lower() in (".jpg", ".jpeg"):
            clean.save(
                str(output), "JPEG",
                quality=jpeg_quality,
                optimize=True,
                icc_profile=None,   # no ICC tag
                exif=b"",           # no EXIF
            )
        else:
            clean.save(str(output), "PNG", optimize=True)

        logger.info(f"Processed -> {output} ({target_w}x{target_h})")
        return str(output)

    except Exception as e:
        logger.error(f"Image processing failed for {input_path}: {e}")
        return None
