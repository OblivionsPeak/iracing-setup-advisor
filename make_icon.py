"""
Generate icon.ico for the iRacing Setup Advisor exe.
Run once: python make_icon.py
Requires Pillow: pip install pillow
"""

from PIL import Image, ImageDraw

SIZES = [16, 32, 48, 64, 128, 256]

BG       = (13,  13,  13,  255)   # near-black
ACCENT   = (33, 150, 243,  255)   # iRacing-blue accent bar
TEXT_COL = (230, 230, 230,  255)  # off-white text


def make_frame(size):
    img  = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Rounded background
    r = max(2, size // 8)
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=BG)

    # Blue accent stripe at the bottom
    bar = max(2, size // 10)
    draw.rectangle([0, size - bar, size - 1, size - 1], fill=ACCENT)

    # "SA" text centred above the stripe
    font_size = int(size * 0.42)
    font = None
    for path in [
        r'C:\Windows\Fonts\arialbd.ttf',
        r'C:\Windows\Fonts\arial.ttf',
    ]:
        try:
            from PIL import ImageFont
            font = ImageFont.truetype(path, font_size)
            break
        except OSError:
            pass
    if font is None:
        from PIL import ImageFont
        font = ImageFont.load_default()

    label = 'SA'
    bbox  = draw.textbbox((0, 0), label, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    x = (size - w) // 2 - bbox[0]
    y = (size - bar - h) // 2 - bbox[1]
    draw.text((x, y), label, fill=TEXT_COL, font=font)

    return img


frames = [make_frame(s) for s in SIZES]
frames[0].save(
    'icon.ico',
    format='ICO',
    sizes=[(s, s) for s in SIZES],
    append_images=frames[1:],
)
print(f'icon.ico written ({len(SIZES)} sizes: {SIZES})')
