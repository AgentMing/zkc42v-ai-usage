from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).with_name('global-markets-mockup.png')
W, H = 400, 300
BLACK, WHITE, RED = (0, 0, 0), (255, 255, 255), (255, 0, 0)
img = Image.new('RGB', (W, H), WHITE)
d = ImageDraw.Draw(img)
regular = ROOT / 'fonts' / 'HarmonyOS_Sans_SC_Regular.ttf'
bold = ROOT / 'fonts' / 'HarmonyOS_Sans_SC_Bold.ttf'
pixel6 = ROOT / 'fonts' / 'spleen-6x12.bdf'
pixel8 = ROOT / 'fonts' / 'spleen-8x16.bdf'
pixel12 = ROOT / 'fonts' / 'spleen-12x24.bdf'

def f(size, is_bold=False):
    # Use native BDF strikes only: 6x12 UI text, 8x16 market values, 12x24 clock.
    path, strike = (pixel6, 12) if size <= 12 else (pixel8, 16) if size <= 16 else (pixel12, 24)
    return ImageFont.truetype(str(path), strike)

def text(x, y, s, size, color=BLACK, is_bold=False, anchor=None):
    d.text((x, y), s, fill=color, font=f(size, is_bold), anchor=anchor)

def fit_text(s, size, max_width, is_bold=False):
    font = f(size, is_bold)
    if d.textbbox((0, 0), s, font=font)[2] > max_width:
        font = ImageFont.truetype(str(pixel6), 12)
    return font

def globe(cx, cy, r):
    d.ellipse((cx-r, cy-r, cx+r, cy+r), outline=BLACK, width=2)
    d.arc((cx-r//2, cy-r, cx+r//2, cy+r), 75, 285, fill=RED, width=2)
    d.arc((cx-r//2, cy-r, cx+r//2, cy+r), 255, 105, fill=BLACK, width=1)
    d.line((cx-r+2, cy, cx+r-2, cy), fill=BLACK, width=1)
    d.arc((cx-r+1, cy-r//2, cx+r-1, cy+r//2), 185, 355, fill=BLACK, width=1)
    d.arc((cx-r+1, cy-r//2, cx+r-1, cy+r//2), 5, 175, fill=BLACK, width=1)

def category_icon(kind, x, y):
    if kind == 'index':
        d.line((x, y+11, x+3, y+7, x+7, y+9, x+12, y+2), fill=RED, width=2)
        d.line((x, y+13, x+13, y+13), fill=BLACK, width=1)
    elif kind == 'fx':
        d.line((x, y+4, x+12, y+4), fill=BLACK, width=1)
        d.polygon([(x+12,y+4),(x+8,y+1),(x+8,y+7)], fill=RED)
        d.line((x+12, y+10, x, y+10), fill=BLACK, width=1)
        d.polygon([(x,y+10),(x+4,y+7),(x+4,y+13)], fill=RED)
    else:
        d.polygon([(x+1,y+5),(x+11,y+5),(x+14,y+12),(x-2,y+12)], outline=BLACK, fill=WHITE)
        d.line((x+1,y+5,x+5,y+1,x+13,y+1,x+11,y+5), fill=RED, width=2)
        d.line((x-1,y+13,x+14,y+13), fill=BLACK, width=1)

def section_title(kind, y, label, english):
    category_icon(kind, 9, y+1)
    text(28, y, label, 10, BLACK, True)
    text(78, y+1, english, 9, RED, True)
    d.line((130, y+8, 390, y+8), fill=BLACK, width=1)

def card(x, y, w, label, value, change, down=False):
    text(x+3, y+1, label, 10, BLACK, True)
    valfont = fit_text(value, 13, w-62, True)
    d.text((x+3, y+11), value, fill=BLACK, font=valfont)
    color = BLACK if down else RED
    change_font = fit_text(change, 16, 50, True)
    ax = x + w - 58
    if down:
        d.polygon([(ax, y+15), (ax+7, y+15), (ax+3, y+20)], fill=color)
    else:
        d.polygon([(ax, y+19), (ax+7, y+19), (ax+3, y+14)], fill=color)
    d.text((x+w-4, y+10), change, fill=color, font=change_font, anchor='ra')
    d.line((x, y+27, x+w-3, y+27), fill=(185,185,185), width=1)

# Compact masthead: reclaimed header whitespace moves all live/status metadata to footer.
globe(19, 16, 10)
text(35, 4, 'MARKET PULSE', 12, BLACK, True)
text(35, 18, 'GLOBAL MARKETS', 9, RED, True)
text(390, 4, '13:35', 18, BLACK, True, anchor='ra')
text(390, 21, 'THU  10/29', 9, BLACK, True, anchor='ra')
d.line((8, 32, 392, 32), fill=BLACK, width=1)

# Global equity indices: six entries in a 2-column grid.
section_title('index', 35, 'EQ', 'INDICES')
indices = [
    ('SSEC', '3,426.18', '+0.62%'), ('CSI300', '3,965.20', '+0.54%'),
    ('HSI', '20,744.30', '+0.18%'), ('SPX', '6,890.21', '+0.35%'),
    ('NDX', '23,120.44', '+0.81%'), ('N225', '48,301.52', '-0.22%'),
    ('SX5E', '5,600.32', '+0.11%'), ('UKX', '9,640.25', '+0.08%'),
]
for i, (label, val, chg) in enumerate(indices):
    row, col = divmod(i, 2)
    card(8 + col*194, 48 + row*28, 188, label, val, chg.lstrip('+-'), down=chg.startswith('-'))

# FX rates.
section_title('fx', 162, 'FX', 'SPOT')
rates = [
    ('USD / CNY', '7.1234', '-0.08%'), ('EUR / USD', '1.0872', '+0.12%'),
]
for i, (label, val, chg) in enumerate(rates):
    row, col = divmod(i, 2)
    card(8 + col*194, 175, 188, label, val, chg.lstrip('+-'), down=chg.startswith('-'))

# Commodities.
section_title('commodity', 205, 'CMDTY', 'FUTURES')
commodities = [
    ('XAU / USD', '3,982.60', '+0.44%'), ('BRENT / USD', '73.42', '-0.31%'),
]
for i, (label, val, chg) in enumerate(commodities):
    row, col = divmod(i, 2)
    card(8 + col*194, 218, 188, label, val, chg.lstrip('+-'), down=chg.startswith('-'))

# Unified footer status strip.
d.line((8, 249, 392, 249), fill=BLACK, width=1)
d.rectangle((8, 258, 15, 265), fill=RED)
text(20, 255, 'MOCKUP', 9, BLACK, True)
text(92, 256, 'DATA 13:35 CST', 9, BLACK, True)
text(390, 255, '96%', 9, BLACK, True, anchor='ra')
# battery mark
bx, by = 371, 256
d.rectangle((bx,by,bx+14,by+8), outline=BLACK, width=1)
d.rectangle((bx+14,by+2,bx+16,by+6), fill=BLACK)
d.rectangle((bx+2,by+2,bx+10,by+6), fill=RED)
d.line((8, 275, 392, 275), fill=(185,185,185), width=1)
d.polygon([(9,289),(15,283),(21,289)], fill=RED)
text(25, 281, 'UP', 10, RED, True)
d.polygon([(72,283),(78,289),(84,283)], fill=BLACK)
text(88, 281, 'DOWN', 10, BLACK, True)
text(390, 281, 'SAMPLE QUOTES', 9, BLACK, True, anchor='ra')

# Pixel-clean BWR only, as the 400x300 panel expects.
px = img.load()
for yy in range(H):
    for xx in range(W):
        rgb = px[xx, yy]
        if rgb[0] > 170 and rgb[1] < 90 and rgb[2] < 90:
            px[xx, yy] = RED
        elif max(rgb)-min(rgb) < 24 and sum(rgb) > 120:
            px[xx, yy] = WHITE
        else:
            px[xx, yy] = BLACK
img.save(OUT)
print(OUT)
print(img.size)
