import numpy as np

from transit_engine import TransitEngine
from population import PopulationData
from blank_area import BlankAreaAnalyzer

engine = TransitEngine()
pop_data = PopulationData("100m_mesh_pop2020_01205室蘭市.csv")
analyzer = BlankAreaAnalyzer(engine, pop_data, max_walk_m=500, walk_speed_m_min=67)

errors = []
tests_run = 0
tests_passed = 0


def test(name, condition, detail=""):
    global tests_run, tests_passed
    tests_run += 1
    if condition:
        tests_passed += 1
    else:
        errors.append(f"FAIL: {name} - {detail}")
        print(f"  FAIL: {name} - {detail}")


print("=" * 70)
print("時間空白診断（BlankAreaAnalyzer）テスト")
print("=" * 70)

dest = engine.get_stop_ids_by_name("東室蘭駅東口")
max_time = 60 * 60
assert dest, "拠点の停留所名が見つからない（テスト自体の設定ミス）"

# ===== 1. 事前計算の整合性 =====
print("\n--- 1. メッシュ×バス停 対応表 ---")
test("メッシュ数が人口データと一致", analyzer.n_mesh == len(pop_data.df),
     f"{analyzer.n_mesh} vs {len(pop_data.df)}")
test("徒歩圏の組が存在する", len(analyzer.pair_mesh) > 0, f"{len(analyzer.pair_mesh)}")
test("徒歩距離が上限以内", float(analyzer.pair_dist.max()) <= 500 + 1e-6,
     f"最大{analyzer.pair_dist.max():.1f}m")
test("静的空白メッシュは対応表に現れない",
     not np.isin(np.nonzero(analyzer.static_blank)[0], analyzer.pair_mesh).any())
print(f"  メッシュ{analyzer.n_mesh} / 組{len(analyzer.pair_mesh)} / "
      f"静的空白{int(analyzer.static_blank.sum())}")

# ===== 2. 空白判定の基本性質 =====
print("\n--- 2. 空白判定 ---")
df = analyzer.diagnose(dest, 9 * 3600, max_time)
summary = analyzer.summarize(df)
test("静的空白メッシュは必ず到達不能", not df.loc[df["static_blank"], "reachable"].any())
test("空白人口 <= 総人口", summary["blank_pop"] <= summary["total_pop"],
     f"{summary['blank_pop']} vs {summary['total_pop']}")
test("静的空白人口 <= 空白人口",
     summary["static_blank_pop"] <= summary["blank_pop"],
     f"{summary['static_blank_pop']} vs {summary['blank_pop']}")
test("到達可能メッシュには余裕時間がある", df.loc[df["reachable"], "margin_min"].notna().all())
test("到達不能メッシュに余裕時間は無い", df.loc[~df["reachable"], "margin_min"].isna().all())
test("余裕時間は制限時間以内",
     float(df["margin_min"].max(skipna=True)) <= max_time / 60 + 1e-6,
     f"最大{df['margin_min'].max(skipna=True):.1f}分")
print(f"  空白人口 {summary['blank_pop']:,} / 総人口 {summary['total_pop']:,}")

# ===== 3. 単調性 =====
print("\n--- 3. 単調性 ---")
wide = analyzer.summarize(analyzer.diagnose(dest, 9 * 3600, 90 * 60))
test("制限時間を延ばすと空白人口は減る（増えない）",
     wide["blank_pop"] <= summary["blank_pop"],
     f"60分{summary['blank_pop']} -> 90分{wide['blank_pop']}")

slow = analyzer.summarize(analyzer.diagnose(dest, 9 * 3600, max_time, speed_factor=0.5))
test("徒歩を遅くすると空白人口は増える（減らない）",
     slow["blank_pop"] >= summary["blank_pop"],
     f"標準{summary['blank_pop']} -> 半減{slow['blank_pop']}")

deep_night = analyzer.summarize(analyzer.diagnose(dest, 4 * 3600, max_time))
test("バスの無い時間帯はほぼ全域が空白",
     deep_night["blank_pop"] >= summary["blank_pop"],
     f"9時{summary['blank_pop']} -> 4時{deep_night['blank_pop']}")

# ===== 4. 拠点を増やすと空白は減る =====
print("\n--- 4. 拠点の追加 ---")
dest2 = dest + engine.get_stop_ids_by_name("室蘭駅前")
multi = analyzer.summarize(analyzer.diagnose(dest2, 9 * 3600, max_time))
test("拠点を増やすと空白人口は減る（増えない）",
     multi["blank_pop"] <= summary["blank_pop"],
     f"1拠点{summary['blank_pop']} -> 2拠点{multi['blank_pop']}")

# 存在しない停留所名を渡した事故が「空白人口＝総人口」として通らないこと
raised = False
try:
    analyzer.diagnose(engine.get_stop_ids_by_name("存在しない停留所"), 9 * 3600, max_time)
except ValueError:
    raised = True
test("拠点が空ならエラーになる（黙って全域空白にしない）", raised)

# ===== 5. 時間帯別 =====
print("\n--- 5. 時間帯別集計 ---")
by_hour = analyzer.diagnose_by_hour(dest, range(7, 20), max_time)
test("時間帯の行数が一致", len(by_hour) == 13, f"{len(by_hour)}")
test("時間帯によって空白人口が変動する", by_hour["blank_pop"].nunique() > 1)
test("空白人口は常に非負", (by_hour["blank_pop"] >= 0).all())
print(f"  最小 {by_hour['blank_pop'].min():,} ({int(by_hour.loc[by_hour['blank_pop'].idxmin(), 'hour'])}時) / "
      f"最大 {by_hour['blank_pop'].max():,} ({int(by_hour.loc[by_hour['blank_pop'].idxmax(), 'hour'])}時)")

# ===== 6. 500mメッシュ集約 =====
print("\n--- 6. 500mメッシュ集約 ---")
from blank_area import aggregate_to_500m

df9 = analyzer.diagnose(dest, 9 * 3600, max_time)
df9["blank_hours"] = analyzer.blank_hours_by_mesh(dest, range(7, 20), max_time)
cells = aggregate_to_500m(df9)

test("集約でメッシュ数が減る", len(cells) < len(df9), f"{len(df9)} -> {len(cells)}")
test("集約しても総人口は保存される",
     abs(cells["pop"].sum() - df9["pop"].sum()) < 1e-6,
     f"{cells['pop'].sum()} vs {df9['pop'].sum()}")
test("集約しても空白人口は保存される",
     abs(cells["blank_pop"].sum() - df9.loc[~df9["reachable"], "pop"].sum()) < 1e-6)
test("空白人口 <= 人口（各セル）", (cells["blank_pop"] <= cells["pop"] + 1e-9).all())
test("深刻度が集約後も範囲内",
     cells["blank_hours"].between(0, 13).all(), f"最大{cells['blank_hours'].max()}")

# 500mセルの中心が構成メッシュの範囲内にあること（座標計算の検算）
merged = df9.assign(
    cell=df9["meshcode"].astype(str).str[:8]
         + (df9["meshcode"].astype(str).str[8].astype(int) // 5).astype(str)
         + (df9["meshcode"].astype(str).str[9].astype(int) // 5).astype(str)
).merge(cells[["cell", "lat", "lon"]], on="cell", suffixes=("", "_cell"))
test("500mセル中心と構成メッシュの距離が354m以内",
     ((merged["lat"] - merged["lat_cell"]).abs() < 0.0025).all()
     and ((merged["lon"] - merged["lon_cell"]).abs() < 0.0035).all(),
     f"最大 lat差{(merged['lat']-merged['lat_cell']).abs().max():.5f} "
     f"lon差{(merged['lon']-merged['lon_cell']).abs().max():.5f}")
print(f"  100mメッシュ {len(df9)} -> 500mセル {len(cells)}")

# ===== 7. メッシュ粒度の判別 =====
# 国勢調査のメッシュ統計は全国で500mと1kmが整備されている。100m限定だと
# 他地域に適用できないため、桁数で粒度を見分けられることを検証する。
print("\n--- 7. メッシュ粒度の判別 ---")
from population import mesh_level, meshcode_to_latlon

test("8桁を1kmと判定", mesh_level("63403779")[0] == "1km")
test("9桁を500mと判定", mesh_level("634037791")[0] == "500m")
test("10桁を100mと判定", mesh_level("6340377901")[0] == "100m")
bad = False
try:
    mesh_level("640")
except ValueError:
    bad = True
test("対応外の桁数はエラー", bad)


def _old_100m(s):
    s = str(s)
    return (int(s[0:2]) / 1.5 + int(s[4]) / 12 + int(s[6]) / 120 + int(s[8]) / 1200 + 1 / 2400,
            int(s[2:4]) + 100 + int(s[5]) / 8 + int(s[7]) / 80 + int(s[9]) / 800 + 1 / 1600)


worst = max(max(abs(a - b) for a, b in zip(meshcode_to_latlon(c), _old_100m(c)))
            for c in pop_data.df["Meshcode"].astype(str).head(2000))
test("100mメッシュの中心が従来実装と一致する", worst < 1e-9, f"最大差 {worst}")

# 粗い粒度のセル中心は、その中に含まれる細かいセルの近くに来るはず
km_lat, km_lon = meshcode_to_latlon("63403779")
m100_lat, m100_lon = meshcode_to_latlon("6340377901")
test("1kmセル中心と、その中の100mセル中心が1kmメッシュ内に収まる",
     abs(km_lat - m100_lat) <= 1 / 120 and abs(km_lon - m100_lon) <= 1 / 80,
     f"差 {abs(km_lat-m100_lat):.5f}, {abs(km_lon-m100_lon):.5f}")

# 500m/1km を入力したとき、集約が素通しで人口を保存すること
import pandas as _pd
for _lvl, _code in [("500m", lambda s: s.str[:8] + "1"), ("1km", lambda s: s.str[:8])]:
    _src = pop_data.df.head(3000)
    _c = _code(_src["Meshcode"].astype(str))
    _df = _pd.DataFrame({"meshcode": _c, "lat": 0.0, "lon": 0.0,
                         "pop": _src["PopT"].to_numpy(),
                         "elderly": _src["Pop65over"].to_numpy(),
                         "reachable": True, "static_blank": False})
    _cells = aggregate_to_500m(_df)
    test(f"{_lvl}入力でも人口が保存される",
         abs(_cells["pop"].sum() - _df["pop"].sum()) < 1e-6)
    test(f"{_lvl}入力ではセルを細分化しない",
         len(_cells) == _df["meshcode"].nunique(),
         f"{len(_cells)} vs {_df['meshcode'].nunique()}")

print("\n" + "=" * 70)
print(f"テスト結果: {tests_passed}/{tests_run} パス")
if errors:
    print(f"\nエラー: {len(errors)}件")
    for e in errors:
        print(f"  {e}")
else:
    print("全テスト合格！問題なし。")
print("=" * 70)
