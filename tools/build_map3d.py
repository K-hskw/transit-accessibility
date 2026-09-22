"""時間空白診断の3Dマップ（単体HTML）を生成する。

アプリの「時間空白診断（3D）」モードと同じ計算結果を、Streamlit 無しで
開ける1枚のHTMLに書き出す。tools/map3d_template.html の __DATA__ を
計算結果のJSONで置換し、docs/kuhaku-scope-muroran.html に出力する。

使い方（リポジトリのルートでも tools/ でも、どこから実行してもよい）:
    python tools/build_map3d.py

出力した docs/kuhaku-scope-muroran.html は外部依存が deck.gl（jsdelivr）と
Google Fonts のみなので、ブラウザで直接開ける。地図タイルは使わない。
"""
import os
import sys
import json

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# TransitEngine 等は gtfs_data やCSVをカレントディレクトリ基準で読むので、
# 実行場所によらずリポジトリルートに移動してからインポートする。
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import numpy as np
from transit_engine import TransitEngine
from population import PopulationData
from blank_area import BlankAreaAnalyzer, aggregate_to_500m

TEMPLATE = os.path.join(HERE, "map3d_template.html")
OUTPUT = os.path.join(ROOT, "docs", "kuhaku-scope-muroran.html")

# 診断の条件（アプリの既定と揃える）
DEST_NAME = "東室蘭駅東口"
POP_CSV = "100m_mesh_pop2020_01205室蘭市.csv"
HOURS = list(range(5, 23))
DAYS = ["平日", "日祝"]   # 土曜は日祝と時刻表が同一なので代表として日祝を使う
MAX_TIME = 3600           # 制限時間60分
WALK_M = 500
WALK_SPEED = 67
LABEL_NAMES = ["東室蘭駅東口", "室蘭駅前", "母恋駅前", "輪西駅前", "白鳥台",
               "中島町", "本輪西", "崎守町", "工大前", "港北町"]


def cell_key(meshcode):
    """100mメッシュコードを、それが属する500mセルのキーに変換する"""
    s = meshcode.astype(str)
    return s.str[:8] + (s.str[8].astype(int) // 5).astype(str) + (s.str[9].astype(int) // 5).astype(str)


def compute():
    eng = TransitEngine()
    pop = PopulationData(POP_CSV)
    an = BlankAreaAnalyzer(eng, pop, max_walk_m=WALK_M, walk_speed_m_min=WALK_SPEED)
    dest = eng.get_stop_ids_by_name(DEST_NAME)
    if not dest:
        raise SystemExit(f"拠点 {DEST_NAME} が見つかりません")

    cells_index = None
    cells_out = []
    values, elderly, sev, summary = {}, {}, {}, {}

    for day in DAYS:
        eng.set_day_type(day)
        values[day], elderly[day], summary[day] = {}, {}, {}
        blank_hours = np.zeros(an.n_mesh, dtype=int)
        per_hour = {}
        for h in HOURS:
            df = an.diagnose(dest, h * 3600, MAX_TIME)
            per_hour[h] = df
            blank_hours += (~df["reachable"]).to_numpy().astype(int)
            s = an.summarize(df)
            summary[day][h] = {
                "blank_pop": s["blank_pop"], "blank_elderly": s["blank_elderly"],
                "static_blank_pop": s["static_blank_pop"], "total_pop": s["total_pop"],
            }
        for h in HOURS:
            df = per_hour[h].copy()
            # 静的空白メッシュは終日空白になるのが当たり前なので、深刻度（何時間帯
            # 空白か）は静的でないメッシュだけで数える。混ぜると駅前の丘陵地が
            # 「最も深刻な時間空白」に見えてしまう。
            df["blank_hours"] = np.where(df["static_blank"], 0, blank_hours)
            g = aggregate_to_500m(df).sort_values("cell").reset_index(drop=True)
            if cells_index is None:
                cells_index = g["cell"].tolist()
                static_pop = df.assign(
                    cell=cell_key(df["meshcode"]),
                    sp=np.where(df["static_blank"], df["pop"], 0.0),
                ).groupby("cell")["sp"].sum()
                for _, r in g.iterrows():
                    cells_out.append({
                        "id": r["cell"],
                        "lat": round(float(r["lat"]), 6),
                        "lon": round(float(r["lon"]), 6),
                        "pop": int(round(r["pop"])),
                        "elderly": int(round(r["elderly"])),
                        # セル内の静的空白（徒歩圏にバス停が無いメッシュ）の人口
                        "static_pop": int(round(static_pop.get(r["cell"], 0.0))),
                    })
            assert g["cell"].tolist() == cells_index, "セルの並びがダイヤ・時刻で変わっている"
            values[day][h] = [int(round(v)) for v in g["blank_pop"]]
            elderly[day][h] = [int(round(v)) for v in g["blank_elderly"]]
            if h == HOURS[0]:
                sev[day] = [int(v) for v in g["blank_hours"]]

    eng.set_day_type("平日")

    stops = [[round(float(r.stop_lon), 5), round(float(r.stop_lat), 5)]
             for r in eng.stop_coords.itertuples()]

    # 市域の形は100mメッシュ人口データの範囲そのもの（室蘭市で切り出されている）。
    # 地図タイルが使えないため、これを敷いて海岸線と絵鞆半島の形を出す。
    # 集計に使う500mセルでは粗すぎて市の形に見えない。
    land = [[round(float(lon), 5), round(float(lat), 5)]
            for lat, lon in zip(pop._lat, pop._lon)]
    labels = []
    for n in LABEL_NAMES:
        ids = eng.get_stop_ids_by_name(n)
        if ids:
            r = eng.stop_coords.loc[ids[0]]
            labels.append({"name": n, "lon": round(float(r["stop_lon"]), 5),
                           "lat": round(float(r["stop_lat"]), 5)})

    return {
        "meta": {
            "dest": DEST_NAME, "max_time_min": MAX_TIME // 60, "walk_m": WALK_M,
            "walk_speed": WALK_SPEED, "mesh_m": 500, "hours": HOURS, "days": DAYS,
            "half_lat": 2.5 / 1200, "half_lon": 2.5 / 800,
            # 100mメッシュ1辺の半分（緯度1/1200度・経度1/800度が100mメッシュの刻み）
            "land_half_lat": 0.5 / 1200, "land_half_lon": 0.5 / 800,
            "center": {"lat": float(np.mean([c["lat"] for c in cells_out])),
                       "lon": float(np.mean([c["lon"] for c in cells_out]))},
        },
        "cells": cells_out,
        "values": values, "elderly": elderly, "severity": sev,
        "summary": summary,
        "stops": stops, "labels": labels, "land": land,
    }


def main():
    data = compute()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))

    with open(TEMPLATE, encoding="utf-8") as f:
        template = f.read()
    if "__DATA__" not in template:
        raise SystemExit("テンプレートに __DATA__ プレースホルダがありません")
    html = template.replace("__DATA__", payload)

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)

    s = data["summary"]
    print(f"出力: {os.path.relpath(OUTPUT, ROOT)}  ({len(html) / 1024:.0f} KB)")
    print(f"セル数 {len(data['cells'])} / バス停 {len(data['stops'])} / データ {len(payload) / 1024:.0f} KB")
    print(f"平日 9時 空白人口 {s['平日'][9]['blank_pop']:,} / 日祝 7時 {s['日祝'][7]['blank_pop']:,}")


if __name__ == "__main__":
    main()
