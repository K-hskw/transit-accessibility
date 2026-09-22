"""国土数値情報から室蘭市の施設POIを作る（facilities.csv 用）

出典:
  P04 医療機関（2020年）／P14 福祉施設（2021年）／P29 学校（2023年）
  いずれも国土数値情報（国土交通省）。再配布可能なライセンス。
差し替えの理由:
  従来の facilities.csv は Google Places API で取得したもので、
  規約上、保存・再配布が制限されるため公開リポジトリに置けない。
"""
import csv, json, os, collections

SP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ksj")
MURORAN = "01205"


def find_geojson(code):
    for root, _dirs, files in os.walk(os.path.join(SP, code)):
        for f in files:
            if f.endswith(".geojson"):
                return os.path.join(root, f)
    raise FileNotFoundError(code)


def load(code):
    return json.load(open(find_geojson(code), encoding="utf-8"))["features"]


def coords(f):
    g = f["geometry"]
    if g["type"] == "Point":
        return g["coordinates"][0], g["coordinates"][1]
    return None


rows = []
report = collections.Counter()

# --- P04 医療機関（行政コード列が無いので住所で絞る）---
P04_TYPE = {"1": "病院", "2": "診療所", "3": "歯科"}
for f in load("P04"):
    p = f["properties"]
    addr = str(p.get("P04_003") or "")
    if "室蘭市" not in addr:
        continue
    c = coords(f)
    if not c:
        continue
    t = P04_TYPE.get(str(p.get("P04_001")), "医療機関")
    rows.append({"name": p.get("P04_002") or "（名称不明）", "type": t,
                 "latitude": round(c[1], 6), "longitude": round(c[0], 6)})
    report[t] += 1

# --- P14 福祉施設 ---
# 室蘭市（01205）のデータが1件も無い。北海道分に含まれるのは旭川市・函館市・
# 札幌市の各区など一部の自治体のみで、全市町村を網羅していない。
# 高齢者施設は交通空白の議論で重要だが、このデータでは補えないため見送る。

# --- P29 学校（P29_001 が行政コード）---
# 分類コードは公開メタデータに定義が無いため、北海道全2,824件の名称の語尾から
# 実証して決めた（tools/verify_school_codes.py）。推測ではない。
# 当初 16013 を専修学校、16016 を各種学校と推測していたが、いずれも誤りだった。
P29_TYPE = {"16001": "小学校",       # 小学校 944/950
            "16002": "中学校",       # 中学校 558/563
            "16003": "中等教育学校",  # 2/2
            "16004": "高等学校",     # 275/275
            "16005": "高等専門学校",  # 4/4
            "16006": "短期大学",     # 7/13
            "16007": "大学",         # 58/58
            "16011": "幼稚園",       # 326/331
            "16012": "特別支援学校",  # 養護・聾・盲学校
            "16013": "認定こども園",  # 幼稚園/こども園 系
            "16014": "義務教育学校",
            "16015": "各種学校",
            "16016": "専修学校"}     # 専門学校 119/159
for f in load("P29"):
    p = f["properties"]
    if str(p.get("P29_001")) != MURORAN:
        continue
    c = coords(f)
    if not c:
        continue
    t = P29_TYPE.get(str(p.get("P29_003")), "その他の学校")
    rows.append({"name": p.get("P29_004") or "（名称不明）", "type": t,
                 "latitude": round(c[1], 6), "longitude": round(c[0], 6)})
    report[t] += 1

print(f"室蘭市の施設: 合計 {len(rows)} 件")
for t, n in report.most_common():
    print(f"  {t:16} {n:4}")

# 座標の妥当性（室蘭市の範囲内か）
lats = [r["latitude"] for r in rows]
lons = [r["longitude"] for r in rows]
print(f"\n緯度 {min(lats):.3f}〜{max(lats):.3f} / 経度 {min(lons):.3f}〜{max(lons):.3f}")
print("（室蘭市はおよそ 北緯42.28〜42.42 / 東経140.88〜141.05）")
out_of_range = [r for r in rows if not (42.2 <= r["latitude"] <= 42.5 and 140.8 <= r["longitude"] <= 141.1)]
print(f"範囲外の施設: {len(out_of_range)} 件 {[r['name'] for r in out_of_range[:3]]}")

OUT = os.path.join(os.path.dirname(SP), "facilities_ksj.csv")
with open(OUT, "w", encoding="utf-8", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["name", "type", "latitude", "longitude"])
    w.writeheader()
    w.writerows(sorted(rows, key=lambda r: (r["type"], r["name"])))
print(f"\n→ {OUT}")
