"""Generate all site icons from the KaisTrade logo's circular brand mark."""
from pathlib import Path

from PIL import Image

PUBLIC = Path(__file__).resolve().parents[1] / "frontend" / "public"
BACKGROUND = (10, 17, 24, 255)


def main():
    with Image.open(PUBLIC / "kaistrade-logo.png") as original:
        source = original.convert("RGBA")
        alpha = source.getchannel("A")
        bounds = alpha.getbbox()
        if bounds is None:
            raise ValueError("KaisTrade logo has no visible artwork")
        left, top, right, bottom = bounds
        mark_right = next(
            (x for x in range(left, right) if alpha.crop((x, top, x + 1, bottom)).getbbox() is None),
            None,
        )
        if mark_right is None or not 0.9 <= (mark_right - left) / (bottom - top) <= 1.1:
            raise ValueError("KaisTrade logo must start with a separate circular brand mark")
        artwork = source.crop((left, top, mark_right, bottom))
    canvas = Image.new("RGBA", (512, 512), BACKGROUND)
    scale = 410 / max(artwork.size)
    artwork = artwork.resize(
        (round(artwork.width * scale), round(artwork.height * scale)),
        Image.Resampling.LANCZOS,
    )
    canvas.alpha_composite(artwork, ((512 - artwork.width) // 2, (512 - artwork.height) // 2))
    for name, size in (
        ("favicon-48x48.png", 48),
        ("favicon-96x96.png", 96),
        ("favicon-192x192.png", 192),
        ("favicon-512x512.png", 512),
        ("icon-192.png", 192),
        ("icon-512.png", 512),
        ("apple-touch-icon.png", 180),
    ):
        canvas.resize((size, size), Image.Resampling.LANCZOS).save(PUBLIC / name)
    canvas.save(PUBLIC / "favicon.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])


if __name__ == "__main__":
    main()
