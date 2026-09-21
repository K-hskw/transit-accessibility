"""処方（増便・ダイヤシフト）と効果比較のテスト"""
import pandas as pd

from transit_engine import TransitEngine
from population import PopulationData
from blank_area import BlankAreaAnalyzer
from prescription import (Plan, compare, evaluate, operating_km,
                          blank_area_stops, rank_routes_by_blank_coverage)

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
print("処方（増便・ダイヤシフト）テスト")
print("=" * 70)

dest = engine.get_stop_ids_by_name("東室蘭駅東口")
LIM = 3600
engine.set_day_type("日祝")
diag = analyzer.diagnose(dest, 7 * 3600, LIM)
base_trips = engine.bus_edges["trip_id"].nunique()

# ===== 1. 候補路線の選定 =====
print("\n--- 1. 候補路線の選定 ---")
cand = rank_routes_by_blank_coverage(engine, analyzer, diag)
test("候補路線が見つかる", len(cand) > 0, f"{len(cand)}")
test("空白地域の停留所数で降順", cand["空白地域の停留所"].is_monotonic_decreasing)
test("空白地域の停留所数 <= 全停留所数", (cand["空白地域の停留所"] <= cand["全停留所"]).all())
test("路線長が正", (cand["路線長km"] > 0).all())

# 便数が多い＝候補として上位、ではないことを確認（便数で選ぶと外れる）
busiest = engine.bus_edges.groupby("route_id")["trip_id"].nunique().idxmax()
test("最多便の路線が候補の筆頭とは限らない",
     cand.iloc[0]["route_id"] != busiest or len(cand) == 1,
     f"最多便 {busiest} / 候補筆頭 {cand.iloc[0]['route_id']}")
print(f"  候補 {len(cand)} 路線 / 筆頭 {cand.iloc[0]['route_name'][:24]}"
      f"（空白地域 {cand.iloc[0]['空白地域の停留所']}停留所）")

stops = blank_area_stops(analyzer, diag)
test("空白地域の停留所が実在するIDである",
     stops and stops <= set(engine.stop_coords.index), f"{len(stops)}")

# ===== 2. 増便 =====
print("\n--- 2. 増便 ---")
rid = cand.iloc[0]["route_id"]
bus_inc, walk_inc = engine.edges_after_frequency_increase(rid, 5, 9, 1)
added = bus_inc["trip_id"].nunique() - base_trips
test("便が増える", added > 0, f"{added}")
test("徒歩エッジは変わらない", walk_inc is engine.walk_edges or len(walk_inc) == len(engine.walk_edges))
new_trips = bus_inc[bus_inc["trip_id"].astype(str).str.startswith("ADD_")]
deps = new_trips.groupby("trip_id")["departure_sec"].min()
test("追加便の始発が指定時間帯に入る",
     ((deps >= 5 * 3600) & (deps < 9 * 3600)).all(),
     f"{[int(d)//3600 for d in deps[:3]]}")
test("追加便の路線IDが元の路線と同じ", set(new_trips["route_id"]) == {rid})
test("追加便の所要時間が正", (new_trips["arrival_sec"] >= new_trips["departure_sec"]).all())

# 便数の少ない路線でも増便できること（隙間方式だと出来ない＝この設計の要点）
sparse = cand[cand["便数"] <= 2]
if len(sparse):
    srid = sparse.iloc[0]["route_id"]
    b2, _ = engine.edges_after_frequency_increase(srid, 5, 9, 1)
    test("便数1〜2本の路線でも増便できる",
         b2["trip_id"].nunique() > base_trips,
         f"{srid} 便数 {sparse.iloc[0]['便数']}")

# ===== 3. ダイヤシフト（費用中立） =====
print("\n--- 3. ダイヤシフト ---")
shift_rid = cand[cand["便数"] >= 6].iloc[0]["route_id"]
bus_sh, _ = engine.edges_after_timetable_shift(shift_rid, (10, 15), (5, 9), 2)
test("総便数が変わらない（費用中立）",
     bus_sh["trip_id"].nunique() == base_trips,
     f"{base_trips} -> {bus_sh['trip_id'].nunique()}")
km_before = operating_km(engine, engine.bus_edges)
with engine.scenario(bus_sh, _):
    km_after = operating_km(engine, engine.bus_edges)
test("総運行キロが変わらない", abs(km_after - km_before) < 1e-6,
     f"{km_before:.1f} -> {km_after:.1f}")
moved = bus_sh[bus_sh["trip_id"].astype(str).str.startswith("SHIFT_")]
mdeps = moved.groupby("trip_id")["departure_sec"].min()
test("移した便の始発が移動先の時間帯に入る",
     ((mdeps >= 5 * 3600) & (mdeps < 9 * 3600)).all())

# ===== 4. 効果比較 =====
print("\n--- 4. 効果比較 ---")
plans = [
    Plan("増便", lambda e: e.edges_after_frequency_increase(rid, 5, 9, 1)),
    Plan("シフト", lambda e: e.edges_after_timetable_shift(shift_rid, (10, 15), (5, 9), 2)),
]
table = compare(engine, analyzer, dest, plans, arrival_hour=7, max_time_sec=LIM)
test("現状の行が先頭", table.iloc[0]["施策"] == "現状（何もしない）")
test("現状の削減はゼロ", table.iloc[0]["全日の削減"] == 0)
test("全施策が表に出る", len(table) == len(plans) + 1, f"{len(table)}")
test("全日の削減で降順に並ぶ", table.iloc[1:]["全日の削減"].is_monotonic_decreasing)
test("費用中立の施策は運行キロ増がゼロ",
     table[table["施策"] == "シフト"].iloc[0]["運行キロ増"] == 0.0)
test("増便は運行キロが増える",
     table[table["施策"] == "増便"].iloc[0]["運行キロ増"] > 0)
print(table[["施策", "7時の削減", "全日の削減", "悪化した時間帯", "追加便数", "運行キロ増"]].to_string(index=False))

# ダイヤシフトは便を抜いた時間帯が悪化する。単一時刻だけ見ると見落とすので、
# 全日評価が悪化を捕まえられているかを確かめる
sh_row = table[table["施策"] == "シフト"].iloc[0]
test("シフトの悪化時間帯を検出できている（狙った時刻が改善なら必ず悪化側がある）",
     sh_row["7時の削減"] <= 0 or sh_row["悪化した時間帯"] > 0,
     f"7時削減 {sh_row['7時の削減']} / 悪化 {sh_row['悪化した時間帯']}")
test("全日の削減は狙った時刻の削減以下（他時間帯の悪化を含むため）",
     sh_row["全日の削減"] <= sh_row["7時の削減"] or sh_row["悪化した時間帯"] == 0,
     f"全日 {sh_row['全日の削減']} / 7時 {sh_row['7時の削減']}")

# ===== 5. 評価しても状態が壊れないこと =====
print("\n--- 5. 副作用が無いこと ---")
test("比較後も便数が元のまま", engine.bus_edges["trip_id"].nunique() == base_trips)
test("比較後も診断結果が元のまま",
     analyzer.summarize(analyzer.diagnose(dest, 7 * 3600, LIM))
     == analyzer.summarize(diag))

engine.set_day_type("平日")

print("\n" + "=" * 70)
print(f"テスト結果: {tests_passed}/{tests_run} パス")
if errors:
    print(f"\nエラー: {len(errors)}件")
    for e in errors:
        print(f"  {e}")
else:
    print("全テスト合格！問題なし。")
print("=" * 70)
