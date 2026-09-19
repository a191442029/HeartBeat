# -*- coding: utf-8 -*-
# 用 PIL 直接渲染安卓前景矢量 -> 鸿蒙分层图标 PNG(1024x1024)
# 安卓前景 108dp 视口: 圆环(中心54,54 r26 w2.6 #F8BBD0) + 点缀(76,28 r4) + 红心(24视口 x2.2 + 27.6)
from PIL import Image, ImageDraw
import os

SIZE = 1024
S = SIZE / 108.0  # 安卓视口 -> 画布 缩放

PINK = (248, 187, 208, 255)   # #F8BBD0
RED = (229, 57, 53, 255)      # #E53935

def V(x, y):
    """安卓 108 视口坐标 -> 画布像素"""
    return (x * S, y * S)

def cubic(p0, p1, p2, p3, steps=48):
    pts = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1 - t
        x = mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
        y = mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
        pts.append((x, y))
    return pts

# ---- 心形 path (24 视口, 标准 Material heart) ----
# M12,21.35 l-1.45,-1.32 C5.4,15.36 2,12.28 2,8.5 2,5.42 4.42,3 7.5,3
# c1.74,0 3.41,0.81 4.5,2.09 C13.09,3.81 14.76,3 16.5,3 19.58,3 22,5.42 22,8.5
# c0,3.78 -3.4,6.86 -8.55,11.54 L12,21.35z
heart = []  # 24 视口坐标点列
seg = cubic((12, 21.35), (10.55, 20.03), (10.55, 20.03), (10.55, 20.03), 1)
heart += cubic((10.55, 20.03), (5.4, 15.36), (2, 12.28), (2, 8.5))
heart += cubic((2, 8.5), (2, 5.42), (4.42, 3), (7.5, 3))
heart += cubic((7.5, 3), (9.24, 3), (10.91, 3.81), (12, 5.09))
heart += cubic((12, 5.09), (13.09, 3.81), (14.76, 3), (16.5, 3))
heart += cubic((16.5, 3), (19.58, 3), (22, 5.42), (22, 8.5))
heart += cubic((22, 8.5), (22, 12.28), (18.6, 15.36), (13.45, 20.04))
heart.append((12, 21.35))

# 24 视口 -> 108 视口 (scale 2.2 + translate 27.6)
heart108 = [(x * 2.2 + 27.6, y * 2.2 + 27.6) for x, y in heart]
heart_px = [V(x, y) for x, y in heart108]

# ---- 渲染 ----
# 前景: 透明底
fg = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(fg)
# 圆环: 中心(54,54) r26 线宽2.6 (外径 27.3)
cx, cy = V(54, 54)
r_out = 27.3 * S
r_in = 24.7 * S
d.ellipse([cx - r_out, cy - r_out, cx + r_out, cy + r_out], fill=PINK)
d.ellipse([cx - r_in, cy - r_in, cx + r_in, cy + r_in], fill=(0, 0, 0, 0))
# 重新用多边形方式画环(上面的内圆透明填充无效, 改用 mask)
fg2 = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
d2 = ImageDraw.Draw(fg2)
ring = Image.new('L', (SIZE, SIZE), 0)
dr = ImageDraw.Draw(ring)
dr.ellipse([cx - r_out, cy - r_out, cx + r_out, cy + r_out], fill=255)
dr.ellipse([cx - r_in, cy - r_in, cx + r_in, cy + r_in], fill=0)
solid = Image.new('RGBA', (SIZE, SIZE), PINK)
fg2.paste(solid, (0, 0), ring)
# 点缀: 圆心(76,28) r4
d2.ellipse([V(76, 28)[0] - 4 * S, V(76, 28)[1] - 4 * S, V(76, 28)[0] + 4 * S, V(76, 28)[1] + 4 * S], fill=PINK)
# 心形
d2.polygon(heart_px, fill=RED)
fg2.save(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'foreground.png'))

# 背景: #FAFAFA 纯色
Image.new('RGB', (SIZE, SIZE), (0xFA, 0xFA, 0xFA)).save(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'background.png'))

print('OK')
