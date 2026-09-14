import io
import os

from PIL import Image, ImageDraw, ImageFont, ImageOps

FONT_PATH = os.path.join(os.path.dirname(__file__), "fonts", "DejaVuSans-Bold.ttf")
CARD_SIZE = (1000, 320)
AVATAR_SIZE = 240


def _load_font(size: int):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:
        return ImageFont.load_default()


def build_profile_header(avatar_bytes: bytes | None) -> bytes:
    """Собирает картинку: круглая аватарка слева, текст «ПРОФИЛЬ» справа,
    на бело-сером градиентном фоне. Возвращает готовый PNG как bytes."""
    width, height = CARD_SIZE

    bg = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(bg)
    for x in range(width):
        t = x / width
        shade = int(255 - t * 35)  # от белого к светло-серому слева направо
        draw.line([(x, 0), (x, height)], fill=(shade, shade, shade))

    pad = 40
    avatar_top = (height - AVATAR_SIZE) // 2
    avatar_box = (pad, avatar_top, pad + AVATAR_SIZE, avatar_top + AVATAR_SIZE)

    if avatar_bytes:
        try:
            avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGB")
        except Exception:
            avatar = Image.new("RGB", (AVATAR_SIZE, AVATAR_SIZE), "#cccccc")
    else:
        avatar = Image.new("RGB", (AVATAR_SIZE, AVATAR_SIZE), "#cccccc")

    avatar = ImageOps.fit(avatar, (AVATAR_SIZE, AVATAR_SIZE))

    mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.ellipse((0, 0, AVATAR_SIZE, AVATAR_SIZE), fill=255)
    bg.paste(avatar, (avatar_box[0], avatar_box[1]), mask)

    text = "ПРОФИЛЬ"
    font = _load_font(80)
    text_x = avatar_box[2] + 50
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_h = text_bbox[3] - text_bbox[1]
    text_y = (height - text_h) // 2 - text_bbox[1]
    draw.text((text_x, text_y), text, font=font, fill="#2b2b2b")

    buf = io.BytesIO()
    bg.save(buf, format="PNG")
    return buf.getvalue()
