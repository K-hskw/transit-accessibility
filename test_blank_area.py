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

# ===== 8. 到着時刻の5分刻み =====
# 毎時00分の1点だけで測ると、数分の違いで便に間に合うかが変わり結果が大きく
# 揺れる（休日7:00着 46.1% / 7:15着 28.2%）。時間帯の値は5分刻みの平均にした。
print("\n--- 8. 5分刻みの診断 ---")
from blank_area import arrival_times, DayDiagnosis

times = arrival_times()
test("5〜22時台を5分刻みで216通り", len(times) == 216 and times[0] == 5 * 3600
     and times[-1] == 22 * 3600 + 55 * 60, f"{len(times)} {times[0]} {times[-1]}")
bad_step = False
try:
    arrival_times(step_min=7)
except ValueError:
    bad_step = True
test("60の約数でない刻みはエラー", bad_step)

engine.set_day_type("日祝")
day = analyzer.diagnose_day(dest, max_time)
test("診断結果の形が 時刻×メッシュ", day.blank.shape == (216, analyzer.n_mesh), str(day.blank.shape))

# 1時刻ずつの診断（従来の diagnose）と行が一致すること
for t in (5 * 3600, 7 * 3600, 7 * 3600 + 35 * 60, 22 * 3600 + 55 * 60):
    one = analyzer.diagnose(dest, t, max_time)
    i = int(np.nonzero(day.times == t)[0][0])
    test(f"{t // 3600}:{(t % 3600) // 60:02d}着の行が単独の診断と一致",
         np.array_equal(day.blank[i], ~one["reachable"].to_numpy()))

s7 = day.hour_summary(7)
singles = [analyzer.summarize(analyzer.diagnose(dest, int(t), max_time))["blank_pop"]
           for t in day.hour_times(7)]
test("時間帯の値は12通りの平均", abs(s7["blank_pop"] - np.mean(singles)) <= 0.5,
     f"{s7['blank_pop']} vs {np.mean(singles):.2f}")
test("最小 <= 平均 <= 最大",
     s7["blank_pop_min"] <= s7["blank_pop"] <= s7["blank_pop_max"],
     f"{s7['blank_pop_min']} / {s7['blank_pop']} / {s7['blank_pop_max']}")
test("00分ちょうどの値も幅の中にある（従来の測り方は特殊な1点）",
     s7["blank_pop_min"] <= singles[0] <= s7["blank_pop_max"])
test("同じ時間帯の中でも到着時刻で空白人口が変わる（時間空白を捉えている）",
     s7["blank_pop_max"] > s7["blank_pop_min"],
     f"{s7['blank_pop_min']}〜{s7['blank_pop_max']}")

hm = day.hour_mesh(7)
test("メッシュの空白割合は0〜1", hm["blank_share"].between(0, 1).all())
test("静的空白メッシュは常に空白（割合1）", (hm.loc[hm["static_blank"], "blank_share"] == 1).all())
# singles は1時刻ごとに整数へ丸めた値なので、丸める前の値どうしで比べる
exact7 = float(day.blank_pop_t[(day.times >= 7 * 3600) & (day.times < 8 * 3600)].mean())
test("メッシュの空白人口を足すと時間帯の平均に一致",
     abs(hm["blank_pop"].sum() - exact7) < 1e-6,
     f"{hm['blank_pop'].sum():.4f} vs {exact7:.4f}")
cells7 = aggregate_to_500m(hm)
test("500mに集約しても時間帯の平均が保存される",
     abs(cells7["blank_pop"].sum() - hm["blank_pop"].sum()) < 1e-6)

bh = day.blank_hours()
test("1日のうち着けない時間は0〜18時間", (bh >= 0).all() and (bh <= 18).all(), f"最大{bh.max()}")
test("静的空白メッシュは18時間すべて空白", (bh[analyzer.static_blank] == 18).all())
test("時間帯別の表は18行", len(day.hourly()) == 18)
hp, he = day.hourly_blank()
test("時間帯ごとの平均（丸め前）が要約と一致",
     all(round(float(hp[i])) == day.hour_summary(h)["blank_pop"] for i, h in enumerate(day.hours)))

no_hour = False
try:
    analyzer.diagnose_day(dest, max_time, hours=[9]).hour_summary(7)
except ValueError:
    no_hour = True
test("計算していない時間帯を求めるとエラー", no_hour)
engine.set_day_type("平日")

# ===== 9. 人口と時刻表の地域のずれ =====
# 別の都市のGTFSに室蘭の人口が残ったまま診断すると、全メッシュが静的空白になり、
# エラーも出ずに空白人口100%と表示されていた。地域が重なっているかを確かめられること。
print("\n--- 9. 人口と時刻表の地域のずれ ---")
test("室蘭の人口と時刻表は重なっている（歩ける範囲にバス停がある人口が8割超）",
     analyzer.served_share() > 0.8, f"{analyzer.served_share():.3f}")
moved = engine.session_copy()
moved.stop_coords = engine.stop_coords.assign(stop_lat=engine.stop_coords["stop_lat"] + 1.5)
far = BlankAreaAnalyzer(moved, pop_data, max_walk_m=500, walk_speed_m_min=67)
test("停留所が別の地域にあると、重なりは0になる（アプリはこれで警告する）",
     far.served_share() == 0.0, f"{far.served_share():.3f}")

print("\n" + "=" * 70)
print(f"テスト結果: {tests_passed}/{tests_run} パス")
if errors:
    print(f"\nエラー: {len(errors)}件")
    for e in errors:
        print(f"  {e}")
else:
    print("全テスト合格！問題なし。")
print("=" * 70)
