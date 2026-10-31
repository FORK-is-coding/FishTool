"""从 LOGO 图标（浅色圆角方块 + 深色图形）提取深色图形，输出白色剪影的 PNG（透明底），用作磁带盒上的纯色 LOGO 底纹。

输出 base64，供 web/frontend/static/intro/logo-mark.js 内嵌为 data URI
（WebGL 贴图用内嵌 data URI，不依赖图片路径与跨域设置）。

用法（在仓库根目录执行）::

    python tools/intro/make_logo_mark.py web/frontend/static/图标.png <输出 png> <输出 b64 txt>

然后把 b64 txt 的内容替换 logo-mark.js 中 ``window.FISH_LOGO = 'data:image/png;base64,…'`` 的 base64 部分。
依赖 Pillow；只读输入图片，只写两个输出文件。
"""
import base64
import io
import sys

from PIL import Image, ImageFilter

src, out_png, out_b64 = sys.argv[1], sys.argv[2], sys.argv[3]
im = Image.open(src).convert("RGBA")
lum = im.convert("L")
alpha = im.getchannel("A")
# 深色图形：亮度 < 120 且不透明；浅色底（>200）与中灰描边（150～200）都排除
mask = Image.eval(lum, lambda v: 255 if v < 120 else 0)
mask = Image.composite(mask, Image.new("L", im.size, 0), Image.eval(alpha, lambda v: 255 if v > 200 else 0))
mask = mask.filter(ImageFilter.MedianFilter(5))
box = mask.getbbox()
mask = mask.crop(box)
w, h = mask.size
side = max(w, h) + 24
canvas = Image.new("L", (side, side), 0)
canvas.paste(mask, ((side - w) // 2, (side - h) // 2))
canvas = canvas.resize((320, 320), Image.LANCZOS)
rgba = Image.new("RGBA", canvas.size, (255, 255, 255, 0))
rgba.putalpha(canvas)
rgba.save(out_png, optimize=True)
buf = io.BytesIO()
rgba.save(buf, format="PNG", optimize=True)
b64 = base64.b64encode(buf.getvalue()).decode()
with open(out_b64, "w", encoding="ascii") as fh:
    fh.write(b64)
print("bbox", box, "png bytes", len(buf.getvalue()), "b64 chars", len(b64))
s = canvas.resize((40, 20))
for y in range(20):
    print("".join("#" if s.getpixel((x, y)) > 128 else ("+" if s.getpixel((x, y)) > 30 else " ") for x in range(40)))
