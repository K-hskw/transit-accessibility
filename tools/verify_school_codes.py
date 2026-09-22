"""学校分類コードを、北海道全域の名称から実証する（推測しない）"""
import json, os, collections, re
SP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ksj")

def find_geojson(code):
    for root, _d, files in os.walk(os.path.join(SP, code)):
        for f in files:
            if f.endswith(".geojson"):
                return os.path.join(root, f)

feats = json.load(open(find_geojson("P29"), encoding="utf-8"))["features"]
by_code = collections.defaultdict(list)
for f in feats:
    p = f["properties"]
    by_code[str(p.get("P29_003"))].append(str(p.get("P29_004") or ""))

SUFFIX = ["幼稚園", "認定こども園", "こども園", "小学校", "中等教育学校", "中学校",
          "高等専門学校", "高等学校", "特別支援学校", "聾学校", "養護学校", "盲学校",
          "短期大学", "大学", "専門学校", "専修学校", "学院", "学校"]

print(f"{'コード':<8}{'件数':>6}  最も多い語尾（判定根拠）              名称の例")
for code in sorted(by_code):
    names = by_code[code]
    hits = collections.Counter()
    for n in names:
        for s in SUFFIX:
            if n.endswith(s):
                hits[s] += 1
                break
    top = hits.most_common(1)
    label = f"{top[0][0]}（{top[0][1]}/{len(names)}）" if top else "―"
    print(f"{code:<8}{len(names):>6}  {label:<32}{names[0][:26]}")
