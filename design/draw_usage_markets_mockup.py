from datetime import datetime
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).with_name('usage-markets-mockup.png')
DATA = ROOT / 'data' / 'out' / 'epaper-quotas-last-records.json'
W, H = 400, 300
BLACK, WHITE, RED = (0, 0, 0), (255, 255, 255), (255, 0, 0)
FONT6 = ROOT / 'fonts' / 'spleen-6x12.bdf'
FONT8 = ROOT / 'fonts' / 'spleen-8x16.bdf'
FONT12 = ROOT / 'fonts' / 'spleen-12x24.bdf'

def font(px):
    path, strike = (FONT6, 12) if px <= 12 else (FONT8, 16) if px <= 16 else (FONT12, 24)
    return ImageFont.truetype(str(path), strike)

img = Image.new('RGB', (W, H), WHITE)
d = ImageDraw.Draw(img)
def txt(x, y, s, px=12, color=BLACK, anchor=None):
    d.text((x, y), s, fill=color, font=font(px), anchor=anchor)

def globe(cx, cy, r=9):
    d.ellipse((cx-r, cy-r, cx+r, cy+r), outline=BLACK, width=1)
    d.arc((cx-r//2, cy-r, cx+r//2, cy+r), 70, 290, fill=RED, width=1)
    d.line((cx-r+2, cy, cx+r-2, cy), fill=BLACK, width=1)
    d.arc((cx-r+1, cy-r//2, cx+r-1, cy+r//2), 185, 355, fill=BLACK, width=1)
    d.arc((cx-r+1, cy-r//2, cx+r-1, cy+r//2), 5, 175, fill=BLACK, width=1)

def pct_text(value):
    return '—' if value is None else f'{round(float(value))}%'

# Usage snapshot from the original usage display, with the old Tao section replaced.
snapshot = json.loads(DATA.read_text(encoding='utf-8'))
records = {r['name']: r for r in snapshot['records']}
quota_specs = [
    ('CODEX', 'codex', ('5H', 'week')),
    ('GROK', 'grok', ('USED', 'week')),
    ('DEVIN', 'windsurf', ('day', 'week')),
]

def remaining_for(rec, wanted):
    for win in rec.get('windows', []):
        label = (win.get('display_label') or win.get('label') or '').lower()
        kind = wanted.lower()
        if wanted == '5H' and ('5h' in label or win.get('window_seconds') == 18000):
            return win.get('remaining_percent', 100 - win.get('used_percent', 0))
        if wanted == 'week' and ('week' in label or win.get('window_seconds') == 604800):
            return win.get('remaining_percent', 100 - win.get('used_percent', 0))
        if wanted == 'day' and ('day' in label or win.get('window_seconds') == 86400):
            return win.get('remaining_percent', 100 - win.get('used_percent', 0))
    if wanted == 'USED':
        return rec.get('used_percent')
    return rec.get('remaining_percent')

globe(17, 13, 9)
txt(32, 4, 'AI USAGE', 16)
txt(32, 17, 'QUOTA + MARKET PULSE', 12, RED)
try:
    saved = datetime.fromisoformat(snapshot['saved_at'].replace('Z', '+00:00')).astimezone()
    date_str, time_str = saved.strftime('%m/%d'), saved.strftime('%H:%M')
except Exception:
    date_str, time_str = '10/29', '13:30'
txt(390, 0, time_str, 24, BLACK, 'ra')
txt(390, 17, date_str, 12, BLACK, 'ra')
d.line((8, 25, 392, 25), fill=BLACK, width=1)

for i, (name, key, labels) in enumerate(quota_specs):
    rec = records.get(key, {})
    y = 28 + i * 34
    txt(9, y + 8, name, 16)
    for col, label in enumerate(labels):
        x = 102 + col * 102
        value = remaining_for(rec, label)
        display_label = {'USED': 'USED', 'week': 'WK', 'day': 'DAY', '5H': '5H'}[label]
        txt(x, y, display_label, 12, RED)
        txt(x, y + 12, pct_text(value), 16)
        if value is not None:
            bx, by, bw, bh = x + 42, y + 15, 48, 5
            d.rectangle((bx, by, bx+bw, by+bh), outline=BLACK, width=1)
            fill = max(0, min(bw-2, round((bw-2) * float(value) / 100)))
            if fill:
                d.rectangle((bx+1, by+1, bx+fill, by+bh-1), fill=RED)
    d.line((8, y+32, 392, y+32), fill=(190, 190, 190), width=1)

# Compact market panel, using native 6x12 and 8x16 bitmap strikes.
def section(y, code, title):
    txt(9, y, code, 12, RED)
    txt(42, y, title, 12, BLACK)
    d.line((128, y+6, 391, y+6), fill=(160, 160, 160), width=1)

section(132, 'IDX', 'GLOBAL INDICES')
indices = [
    ('SSEC', '3,426.18', '0.62', False), ('CSI300', '3,965.20', '0.54', False),
    ('HSI', '20,744.30', '0.18', False), ('SPX', '6,890.21', '0.35', False),
    ('NDX', '23,120.44', '0.81', False), ('N225', '48,301.52', '0.22', True),
]
for i, (code, value, change, down) in enumerate(indices):
    row, col = divmod(i, 2)
    x, y = 8 + col*194, 145 + row*22
    txt(x+2, y, code, 12)
    txt(x+2, y+9, value, 16)
    change_color = BLACK if down else RED
    ax = x+137
    if down:
        d.polygon([(ax, y+12), (ax+6, y+12), (ax+3, y+16)], fill=change_color)
    else:
        d.polygon([(ax, y+16), (ax+6, y+16), (ax+3, y+11)], fill=change_color)
    txt(x+145, y+9, change+'%', 12, change_color)

section(214, 'FX', 'SPOT')
txt(10, 227, 'USD/CNY  7.1234', 16)
txt(205, 227, 'EUR/USD  1.0872', 16)
section(248, 'CMD', 'COMMODITIES')
txt(10, 261, 'XAU/USD  3,982.60', 16)
txt(205, 261, 'BRENT  73.42', 16)

# Unified footer status strip.
d.line((8, 282, 392, 282), fill=BLACK, width=1)
txt(10, 285, 'DATA 13:30 CST | SAMPLE QUOTES', 12)
bx, by = 370, 285
d.rectangle((bx, by, bx+15, by+9), outline=BLACK, width=1)
d.rectangle((bx+15, by+2, bx+17, by+7), fill=BLACK)
d.rectangle((bx+2, by+2, bx+11, by+7), fill=RED)

# Keep only the panel's three ink colors.
px = img.load()
for yy in range(H):
    for xx in range(W):
        r, g, b = px[xx, yy]
        if r > 170 and g < 90 and b < 90:
            px[xx, yy] = RED
        elif max(r, g, b) < 128:
            px[xx, yy] = BLACK
        else:
            px[xx, yy] = WHITE
img.save(OUT)
print(OUT)
print(img.size)
