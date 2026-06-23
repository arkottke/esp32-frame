"""
Convert a PIL image to the 960,000-byte binary format expected by the
GDEP133C02 EPD driver's pic_display_test() function.

Display: 1200x1600 pixels, 6-color ACeP, two driver ICs (CS0 + CS1).

Buffer layout (from draw_checkerboard in the reference firmware):
  - 1600 rows × 600 bytes/row = 960,000 bytes
  - Each row: 300 bytes for CS0 half + 300 bytes for CS1 half
  - Horizontal scan is left-to-right:
      display pixel (x, y) → byte index = y*600 + x//2
  - Two pixels per byte: high nibble = pixel at even x, low nibble = pixel at odd x

Color nibbles:
  BLACK=0x0  WHITE=0x1  YELLOW=0x2  RED=0x3  BLUE=0x5  GREEN=0x6
"""

from __future__ import annotations

import numpy as np
from PIL import Image

EPD_WIDTH = 1200
EPD_HEIGHT = 1600
EPD_BYTES = EPD_WIDTH * EPD_HEIGHT // 2  # 960_000

# ACeP 6-color palette — real-world measured colors emitted by the Spectra6 panel
# (from epdoptimize: https://github.com/Utzel-Butzel/epdoptimize)
# Using measured values instead of ideal primaries improves Floyd-Steinberg error
# diffusion because the dithering math operates in the display's actual color space.
PALETTE_RGB = [
    (25,  30,  33),   # 0 BLACK
    (232, 232, 232),  # 1 WHITE
    (239, 222,  68),  # 2 YELLOW
    (178,  19,  24),  # 3 RED
    (33,   87, 186),  # 5 BLUE  (nibble 0x5, index 4 in this list)
    (18,   95,  32),  # 6 GREEN (nibble 0x6, index 5 in this list)
]

# Map list index → EPD nibble value
_INDEX_TO_NIBBLE = [0x0, 0x1, 0x2, 0x3, 0x5, 0x6]


def _build_palette_image() -> Image.Image:
    """Build a 256-entry palette PIL image for quantize()."""
    palette_img = Image.new("P", (1, 1))
    flat = []
    for r, g, b in PALETTE_RGB:
        flat += [r, g, b]
    # Pad remaining 250 entries with black
    flat += [0, 0, 0] * (256 - len(PALETTE_RGB))
    palette_img.putpalette(flat)
    return palette_img


_PALETTE_IMG = _build_palette_image()


def image_to_epd(img: Image.Image) -> bytes:
    """
    Convert a PIL Image to the 960,000-byte EPD buffer.

    The image is resized to 1200x1600 (portrait), Floyd-Steinberg dithered
    to the 6-color ACeP palette, then packed into the nibble/scan-direction
    format expected by pic_display_test().
    """
    # Resize to exact display dimensions
    img = img.convert("RGB").resize((EPD_WIDTH, EPD_HEIGHT), Image.LANCZOS)

    # Quantize with Floyd-Steinberg dithering to our 6-color palette
    quantized = img.quantize(palette=_PALETTE_IMG, dither=Image.Dither.FLOYDSTEINBERG)

    # Extract pixel indices as a numpy array for fast packing
    indices = np.array(quantized, dtype=np.uint8)  # shape (1600, 1200)

    # Map palette indices → EPD nibble values.
    # Pillow may assign indices beyond 5 to the black-padded palette slots,
    # so build a full 256-entry LUT (unknown indices default to BLACK = 0x0).
    nibble_lut = np.zeros(256, dtype=np.uint8)
    for i, nibble in enumerate(_INDEX_TO_NIBBLE):
        nibble_lut[i] = nibble
    nibbles = nibble_lut[indices]  # shape (1600, 1200)

    # Pack pairs of nibbles: even column → high nibble, odd column → low nibble
    high = nibbles[:, 0::2]  # shape (1600, 600)
    low  = nibbles[:, 1::2]  # shape (1600, 600)
    packed = ((high << 4) | low).astype(np.uint8)  # shape (1600, 600)

    return packed.tobytes()


def epd_to_image(data: bytes) -> Image.Image:
    """
    Reverse of image_to_epd — reconstruct a viewable RGB image from
    a 960,000-byte EPD buffer. Useful for verifying server output.
    """
    if len(data) != EPD_BYTES:
        raise ValueError(f"Expected {EPD_BYTES} bytes, got {len(data)}")

    packed = np.frombuffer(data, dtype=np.uint8).reshape((EPD_HEIGHT, EPD_WIDTH // 2))
    high = (packed >> 4) & 0x0F
    low  =  packed        & 0x0F

    # Interleave high/low back into (1600, 1200)
    nibbles = np.zeros((EPD_HEIGHT, EPD_WIDTH), dtype=np.uint8)
    nibbles[:, 0::2] = high
    nibbles[:, 1::2] = low

    # Map nibble values → palette RGB
    # Build a lookup table indexed by nibble value (0..15)
    lut = np.zeros((16, 3), dtype=np.uint8)
    for idx, nibble in enumerate(_INDEX_TO_NIBBLE):
        lut[nibble] = PALETTE_RGB[idx]

    rgb = lut[nibbles]  # shape (1600, 1200, 3)
    return Image.fromarray(rgb, "RGB")


if __name__ == "__main__":
    import sys

    if len(sys.argv) == 3 and sys.argv[1] == "encode":
        src = sys.argv[2]
        dst = src.rsplit(".", 1)[0] + ".epd"
        with open(src, "rb") as f:
            img = Image.open(f)
            data = image_to_epd(img)
        with open(dst, "wb") as f:
            f.write(data)
        print(f"Encoded {src} → {dst} ({len(data)} bytes)")

    elif len(sys.argv) == 3 and sys.argv[1] == "decode":
        src = sys.argv[2]
        dst = src.rsplit(".", 1)[0] + "_preview.png"
        with open(src, "rb") as f:
            data = f.read()
        img = epd_to_image(data)
        img.save(dst)
        print(f"Decoded {src} → {dst}")

    else:
        print("Usage:")
        print("  python dither.py encode <image.png>")
        print("  python dither.py decode <image.epd>")
