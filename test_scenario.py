"""施策シナリオ（scenario / edges_after_*）のテスト

処方モジュールの土台。simulate_* は起点1停留所からの前方到達圏しか返さない
ため、施策の効果を核心指標（空白人口）で測るには、改変後ネットワークを
取り出して逆方向到達圏ベースの診断に渡す必要がある。その仕組みを検証する。
"""
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
print("施策シナリオ（scenario / edges_after_*）テスト")
print("=" * 70)

dest = engine.get_stop_ids_by_name("東室蘭駅東口")
start = engine.get_stop_ids_by_name("室蘭駅前")[0]
H, LIM = 9 * 3600, 3600
base_edges = len(engine.bus_edges)
base_walk = len(engine.walk_edges)
base = analyzer.summarize(analyzer.diagnose(dest, H, LIM))
big_route = max(engine.get_muroran_routes(),
                key=lambda r: len(engine.bus_edges[engine.bus_edges["route_id"] == r["route_id"]]))

# ===== 1. edges_after_* と simulate_* が一致するか =====
# 片方だけ直すと両者がずれるので、refactor の要となる検証
print("\n--- 1. edges_after_* と simulate_* の整合 ---")


def dijkstra_on(bus_edges, walk_edges, start_id=start, t=8 * 3600):
    return engine._dijkstra(engine._build_bus_graph(bus_edges),
                            engine._build_walk_graph(walk_edges),
                            start_id, t, LIM)


rid = big_route["route_id"]
test("路線廃止が一致",
     dijkstra_on(*engine.edges_after_route_removal(rid))
     == engine.simulate_route_removal(start, 8 * 3600, LIM, rid))

rm_stops = engine.get_stop_ids_by_name("輪西駅前")
test("バス停削除が一致",
     dijkstra_on(*engine.edges_after_stop_removal(rm_stops, 300))
     == engine.simulate_stop_removal(start, 8 * 3600, LIM, rm_stops, walk_distance=300))

test("減便が一致",
     dijkstra_on(*engine.edges_after_frequency_reduction("all", reduce_ratio=0.3))
     == engine.simulate_frequency_reduction(start, 8 * 3600, LIM, "all", reduce_ratio=0.3))

new_route = engine.get_stop_ids_by_name("室蘭駅前")[:1] + dest[:1]
test("代替路線追加が一致",
     dijkstra_on(*engine.edges_after_route_replacement(None, new_route, 30, 25))
     == engine.simulate_route_replacement(start, 8 * 3600, LIM, None, new_route, 30, 25))

# ===== 2. scenario の適用と復帰 =====
print("\n--- 2. scenario の適用と復帰 ---")
with engine.scenario(*engine.edges_after_route_removal(rid)) as e:
    test("scenario内でバスエッジが減る", len(e.bus_edges) < base_edges,
         f"{len(e.bus_edges)} vs {base_edges}")
    inside = analyzer.summarize(analyzer.diagnose(dest, H, LIM))
test("抜けるとバスエッジが戻る", len(engine.bus_edges) == base_edges)
test("抜けると徒歩エッジも戻る", len(engine.walk_edges) == base_walk)
test("抜けると診断結果も戻る（キャッシュが残っていない）",
     analyzer.summarize(analyzer.diagnose(dest, H, LIM)) == base)

with engine.scenario(*engine.edges_after_stop_removal(rm_stops, 300)) as e:
    test("バス停削除では徒歩エッジも差し替わる", len(e.walk_edges) != base_walk,
         f"{len(e.walk_edges)} vs {base_walk}")

raised = False
try:
    with engine.scenario(*engine.edges_after_route_removal(rid)):
        raise RuntimeError("途中で失敗")
except RuntimeError:
    raised = True
test("例外が起きても復帰する",
     raised and len(engine.bus_edges) == base_edges
     and analyzer.summarize(analyzer.diagnose(dest, H, LIM)) == base)

# ===== 3. 劣化施策は空白人口を増やす（減らさない） =====
print("\n--- 3. 劣化施策の向き ---")
with engine.scenario(*engine.edges_after_route_removal(rid)):
    removed = analyzer.summarize(analyzer.diagnose(dest, H, LIM))
test("路線廃止で空白人口は減らない", removed["blank_pop"] >= base["blank_pop"],
     f"{base['blank_pop']} -> {removed['blank_pop']}")

prev = base["blank_pop"]
ok = True
for ratio in (0.3, 0.5, 0.7):
    with engine.scenario(*engine.edges_after_frequency_reduction("all", reduce_ratio=ratio)):
        v = analyzer.summarize(analyzer.diagnose(dest, H, LIM))["blank_pop"]
    if v < prev:
        ok = False
    prev = v
test("減便率を上げると空白人口は単調に増える（減らない）", ok, f"最終 {prev}")

test("静的空白は施策で変わらない（徒歩圏の問題なので）",
     removed["static_blank_pop"] == base["static_blank_pop"],
     f"{base['static_blank_pop']} vs {removed['static_blank_pop']}")

# ===== 4. 改善施策は空白人口を減らせる =====
# 空白地域を結ぶ新路線を引けば減ること、頻度を上げるほど効くことを確認する
print("\n--- 4. 改善施策の向き ---")
df = analyzer.diagnose(dest, H, LIM)
blank = df[(~df["reachable"]) & (~df["static_blank"])].sort_values("pop", ascending=False).head(10)
import numpy as np
from population import haversine_matrix

d = haversine_matrix(blank["lat"].to_numpy(), blank["lon"].to_numpy(),
                     engine.stop_coords["stop_lat"].to_numpy(),
                     engine.stop_coords["stop_lon"].to_numpy())
stop_ids = engine.stop_coords.index.to_numpy()
cand = []
for i in range(len(blank)):
    for s in stop_ids[d[i] <= 500]:
        if s not in cand:
            cand.append(s)
improve_route = cand[:6] + [dest[0]]

with engine.scenario(*engine.edges_after_route_replacement(None, improve_route, 10, 25)):
    improved = analyzer.summarize(analyzer.diagnose(dest, H, LIM))
test("空白地域を結ぶ新路線で空白人口が減る",
     improved["blank_pop"] < base["blank_pop"],
     f"{base['blank_pop']} -> {improved['blank_pop']}")
print(f"  空白人口 {base['blank_pop']:,} -> {improved['blank_pop']:,} "
      f"({improved['blank_pop']-base['blank_pop']:+,})")

with engine.scenario(*engine.edges_after_route_replacement(None, improve_route, 60, 25)):
    sparse = analyzer.summarize(analyzer.diagnose(dest, H, LIM))
test("運行間隔が広いほど効果は小さい",
     sparse["blank_pop"] >= improved["blank_pop"],
     f"10分 {improved['blank_pop']} / 60分 {sparse['blank_pop']}")
print(f"  60分間隔だと {sparse['blank_pop']:,}（10分間隔 {improved['blank_pop']:,}）")

# ===== 5. ダイヤ種別をまたいでも使えるか =====
print("\n--- 5. ダイヤ種別との併用 ---")
engine.set_day_type("日祝")
h_base = analyzer.summarize(analyzer.diagnose(dest, 7 * 3600, LIM))
with engine.scenario(*engine.edges_after_route_replacement(None, improve_route, 10, 25)):
    h_aft = analyzer.summarize(analyzer.diagnose(dest, 7 * 3600, LIM))
test("休日ダイヤでも改善が測れる", h_aft["blank_pop"] < h_base["blank_pop"],
     f"{h_base['blank_pop']} -> {h_aft['blank_pop']}")
print(f"  休日7時着 {h_base['blank_pop']:,} -> {h_aft['blank_pop']:,}")
engine.set_day_type("平日")
test("ダイヤを戻すと基準に一致",
     analyzer.summarize(analyzer.diagnose(dest, H, LIM)) == base)

print("\n" + "=" * 70)
print(f"テスト結果: {tests_passed}/{tests_run} パス")
if errors:
    print(f"\nエラー: {len(errors)}件")
    for e in errors:
        print(f"  {e}")
else:
    print("全テスト合格！問題なし。")
print("=" * 70)
