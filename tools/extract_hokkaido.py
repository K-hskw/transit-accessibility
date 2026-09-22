"""北海道の輪郭を抽出し、インセット地図用に簡略化する

出典: 地球地図日本（国土地理院）→ dataofjapan/land で都道府県単位に変換されたもの。
本島＋主要な属島だけを残し、Douglas-Peucker で点を間引いて埋め込める大きさにする。
"""
import json, math, os

SP = os.path.dirname(os.path.abspath(__file__))
src = json.load(open(os.path.join(SP, "japan.geojson"), encoding="utf-8"))

feats = src["features"]
print(f"全features: {len(feats)}")
print("属性キーの例:", list(feats[0]["properties"].keys()))

hok = [f for f in feats if f["properties"].get("nam_ja") == "北海道"
       or f["properties"].get("id") == 1]
print(f"北海道のfeature数: {len(hok)}")

# すべてのリング（外周のみ）を集める
rings = []
for f in hok:
    g = f["geometry"]
    polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
    for poly in polys:
        rings.append(poly[0])          # 外周リングのみ（穴は無視）
print(f"リング数（島の数）: {len(rings)}")


def bbox_area(ring):
    xs = [p[0] for p in ring]; ys = [p[1] for p in ring]
    return (max(xs) - min(xs)) * (max(ys) - min(ys))


rings.sort(key=bbox_area, reverse=True)
print("上位5島のbbox面積:", [round(bbox_area(r), 4) for r in rings[:5]])

# 本島だけだと利尻・礼文などが消えるが、インセットでは形が伝わればよい。
# 面積が本島の0.2%以上の島を残す。
main_area = bbox_area(rings[0])
kept = [r for r in rings if bbox_area(r) >= main_area * 0.002]
print(f"残した島: {len(kept)} / 点数合計 {sum(len(r) for r in kept):,}")


def perp_dist(p, a, b):
    if a == b:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)
    t = max(0, min(1, t))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def simplify(pts, eps):
    if len(pts) < 3:
        return pts
    dmax, idx = 0, 0
    for i in range(1, len(pts) - 1):
        d = perp_dist(pts[i], pts[0], pts[-1])
        if d > dmax:
            dmax, idx = d, i
    if dmax > eps:
        return simplify(pts[:idx + 1], eps)[:-1] + simplify(pts[idx:], eps)
    return [pts[0], pts[-1]]


import sys
sys.setrecursionlimit(20000)

# 度。約220m相当。柱と同じ本体の地図に重ねるため、室蘭のズームで海岸線が
# 破綻しない精度が要る（人口メッシュが100m刻みなのでこの程度で釣り合う）。
# 1.3km相当まで粗くすると噴火湾の海岸線がカクつく。
EPS = 0.002
out = []
for r in kept:
    s = simplify(r, EPS)
    if len(s) >= 4:
        out.append([[round(x, 3), round(y, 3)] for x, y in s])
out.sort(key=len, reverse=True)

print(f"簡略化後: {len(out)}島 / 点数合計 {sum(len(r) for r in out)}")
payload = json.dumps(out, separators=(",", ":"))
print(f"埋め込みサイズ: {len(payload)/1024:.1f} KB")

xs = [p[0] for r in out for p in r]; ys = [p[1] for r in out for p in r]
print(f"経度 {min(xs):.2f}〜{max(xs):.2f} / 緯度 {min(ys):.2f}〜{max(ys):.2f}")
print("（北海道の実際の範囲は およそ東経139.3〜148.9 / 北緯41.3〜45.6）")

with open(os.path.join(SP, "hokkaido.json"), "w", encoding="utf-8") as f:
    f.write(payload)
print("→ hokkaido.json に保存")
