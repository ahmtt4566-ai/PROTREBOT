"""Generate site icons from the existing KaisTrade pulse artwork."""
from pathlib import Path
from io import BytesIO
import struct

from PIL import Image

PUBLIC = Path(__file__).resolve().parents[1] / "frontend" / "public"
BACKGROUND = (255, 255, 255, 255)


def main():
    with Image.open(PUBLIC / "favicon-512x512.png") as original:
        source = original.convert("RGBA")
        bounds = source.getchannel("A").getbbox()
        if bounds is None:
            raise ValueError("Existing KaisTrade icon has no visible artwork")
        artwork = source.crop(bounds)
    canvas = Image.new("RGBA", (512, 512), BACKGROUND)
    artwork.thumbnail((410, 410), Image.Resampling.LANCZOS)
    canvas.alpha_composite(artwork, ((512 - artwork.width) // 2, (512 - artwork.height) // 2))
    for name, size in (("icon-192.png", 192), ("icon-512.png", 512), ("apple-touch-icon.png", 180)):
        canvas.resize((size, size), Image.Resampling.LANCZOS).save(PUBLIC / name)
    png = BytesIO()
    canvas.resize((48, 48), Image.Resampling.LANCZOS).save(png, format="PNG")
    payload = png.getvalue()
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 48, 48, 0, 0, 1, 32, len(payload), 22)
    (PUBLIC / "favicon.ico").write_bytes(header + entry + payload)


if __name__ == "__main__":
    main()
