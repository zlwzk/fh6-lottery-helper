"""生成程序图标 assets/app.ico（没有外部资源依赖，纯脚本画）。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "app.ico"

BG = (26, 26, 46, 255)
ACCENT = (255, 108, 52, 255)
WHITE = (255, 255, 255, 255)


def render(size: int = 512) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = int(size * 0.05)
    radius = int(size * 0.22)
    draw.rounded_rectangle((pad, pad, size - pad, size - pad), radius=radius, fill=BG)

    ring = int(size * 0.30)
    cx, cy = size // 2, size // 2
    draw.ellipse((cx - ring, cy - ring, cx + ring, cy + ring), fill=ACCENT)
    # 转盘上的扇形分割
    draw.pieslice((cx - ring * 0.72, cy - ring * 0.72, cx + ring * 0.72, cy + ring * 0.72),
                  start=90, end=180, fill=WHITE)
    hub = int(size * 0.075)
    draw.ellipse((cx - hub, cy - hub, cx + hub, cy + hub), fill=BG)
    # 指针
    pointer = [(cx, cy - ring * 1.02), (cx - int(size * 0.045), cy - ring * 0.72),
               (cx + int(size * 0.045), cy - ring * 0.72)]
    draw.polygon(pointer, fill=WHITE)
    return img


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    base = render(512)
    base.save(OUT, format="ICO",
              sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    preview = ROOT / "assets" / "app.png"
    base.resize((256, 256), Image.LANCZOS).save(preview)
    print(f"已生成图标：{OUT}")


if __name__ == "__main__":
    main()
