from transit_engine import TransitEngine
from population import PopulationData
import random

engine = TransitEngine()
pop_data = PopulationData("100m_mesh_pop2020_01205室蘭市.csv")

errors = []
warnings = []
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

def warn(name, detail):
    warnings.append(f"WARN: {name} - {detail}")

print("=" * 70)
print("総合テスト V2（人口統合・複数路線同時廃止を含む）")
print("=" * 70)

# ===== 1. 人口データ整合性チェック =====
print("\n--- 1. 人口データ整合性チェック ---")

total_pop = round(pop_data.df["PopT"].sum())
total_elderly = round(pop_data.df["Pop65over"].sum())

test("人口データ読み込み", len(pop_data.df) > 0, f"メッシュ数: {len(pop_data.df)}")
test("総人口が正", total_pop > 0, f"総人口: {total_pop}")
test("高齢者人口が正", total_elderly > 0, f"高齢者: {total_elderly}")
test("高齢者 <= 総人口", total_elderly <= total_pop, f"高齢者{total_elderly} > 総人口{total_pop}")
test("緯度が室蘭市範囲内", pop_data.df["lat"].min() >= 42.2 and pop_data.df["lat"].max() <= 42.5,
     f"lat: {pop_data.df['lat'].min():.3f} - {pop_data.df['lat'].max():.3f}")
test("経度が室蘭市範囲内", pop_data.df["lon"].min() >= 140.8 and pop_data.df["lon"].max() <= 141.2,
     f"lon: {pop_data.df['lon'].min():.3f} - {pop_data.df['lon'].max():.3f}")
test("人口が負でない", (pop_data.df["PopT"] >= 0).all(), "負の人口あり")

print(f"  人口データ: {len(pop_data.df)}メッシュ, 総人口{total_pop}, 高齢者{total_elderly}")

# ===== 2. 人口計算の整合性チェック =====
print("\n--- 2. 人口計算の整合性チェック ---")

start_id = engine.get_stop_ids_by_name("室蘭駅前")[0]
start_time = 8 * 3600
max_time = 60 * 60

result_before, _ = engine.calc_isochrone(start_id, start_time, max_time, track_path=True)

# カバー人口が総人口以下か
pop_before = pop_data.get_population_near_stops(
    engine.stop_coords, list(result_before.keys()), radius_m=300
)
test("カバー人口 <= 総人口", pop_before["total"] <= total_pop,
     f"カバー{pop_before['total']} > 総人口{total_pop}")
test("カバー人口 > 0", pop_before["total"] > 0, f"カバー人口: {pop_before['total']}")
test("カバー高齢者 <= カバー人口", pop_before["elderly"] <= pop_before["total"],
     f"高齢者{pop_before['elderly']} > 総人口{pop_before['total']}")

print(f"  カバー人口: {pop_before['total']}, 高齢者: {pop_before['elderly']}")

# ===== 3. 単一路線廃止 + 人口影響チェック =====
print("\n--- 3. 単一路線廃止 + 人口影響チェック ---")

routes = engine.get_muroran_routes(exclude_highway=True)
for route in routes:
    rid = route["route_id"]
    rname = route["route_name"]

    result_after, _ = engine.simulate_route_removal(
        start_id, start_time, max_time, rid, track_path=True
    )

    lost = set(result_before.keys()) - set(result_after.keys())

    if lost:
        pop_impact = pop_data.calc_impact_population(
            engine.stop_coords, list(lost), list(result_before.keys()), radius_m=300
        )
        test(f"単一廃止({rname}) 影響人口>=0",
             pop_impact["affected_total"] >= 0,
             f"影響人口: {pop_impact['affected_total']}")
        test(f"単一廃止({rname}) 影響高齢者<=影響人口",
             pop_impact["affected_elderly"] <= pop_impact["affected_total"],
             f"高齢者{pop_impact['affected_elderly']} > 総{pop_impact['affected_total']}")

        pop_after = pop_data.get_population_near_stops(
            engine.stop_coords, list(result_after.keys()), radius_m=300
        )
        test(f"単一廃止({rname}) 廃止後カバー人口<=廃止前",
             pop_after["total"] <= pop_before["total"] + 1,
             f"前{pop_before['total']} 後{pop_after['total']}")

print(f"  単一路線廃止テスト完了")

# ===== 4. 複数路線同時廃止チェック =====
print("\n--- 4. 複数路線同時廃止チェック ---")

# 2路線同時廃止
for i in range(min(5, len(routes))):
    for j in range(i+1, min(6, len(routes))):
        rid_list = [routes[i]["route_id"], routes[j]["route_id"]]
        rname_list = f"{routes[i]['route_name']} + {routes[j]['route_name']}"

        result_after, _ = engine.simulate_route_removal(
            start_id, start_time, max_time, rid_list, track_path=True
        )

        # 同時廃止後の到達可能数 <= 廃止前
        test(f"2路線同時廃止({rname_list[:40]}) 到達可能<=廃止前",
             len(result_after) <= len(result_before),
             f"前{len(result_before)} 後{len(result_after)}")

        # 同時廃止後に新規到達なし
        new_stops = set(result_after.keys()) - set(result_before.keys())
        test(f"2路線同時廃止({rname_list[:40]}) 新規到達なし",
             len(new_stops) == 0,
             f"新規: {len(new_stops)}")

        # 各路線を個別に廃止した場合よりも、同時廃止の方が影響が大きい（または同じ）
        result_a, _ = engine.simulate_route_removal(
            start_id, start_time, max_time, rid_list[0], track_path=True
        )
        result_b, _ = engine.simulate_route_removal(
            start_id, start_time, max_time, rid_list[1], track_path=True
        )
        test(f"2路線同時廃止({rname_list[:40]}) 同時>=個別A",
             len(result_after) <= len(result_a),
             f"同時{len(result_after)} 個別A{len(result_a)}")
        test(f"2路線同時廃止({rname_list[:40]}) 同時>=個別B",
             len(result_after) <= len(result_b),
             f"同時{len(result_after)} 個別B{len(result_b)}")

# 3路線同時廃止
if len(routes) >= 3:
    rid_list = [routes[0]["route_id"], routes[1]["route_id"], routes[2]["route_id"]]
    result_after, _ = engine.simulate_route_removal(
        start_id, start_time, max_time, rid_list, track_path=True
    )
    test("3路線同時廃止 到達可能<=廃止前",
         len(result_after) <= len(result_before),
         f"前{len(result_before)} 後{len(result_after)}")

# 全路線同時廃止（バスなしの状態）
all_route_ids = [r["route_id"] for r in routes]
result_no_bus, _ = engine.simulate_route_removal(
    start_id, start_time, max_time, all_route_ids, track_path=True
)
test("全路線同時廃止 到達可能が最小",
     len(result_no_bus) <= len(result_before),
     f"前{len(result_before)} 後{len(result_no_bus)}")
test("全路線同時廃止 出発地点のみ or 徒歩圏のみ",
     len(result_no_bus) < 20,
     f"到達可能: {len(result_no_bus)}（徒歩のみのはず）")

print(f"  複数路線同時廃止テスト完了")

# ===== 5. 複数路線同時廃止の人口影響チェック =====
print("\n--- 5. 複数路線同時廃止の人口影響チェック ---")

# 影響の大きい路線を複数同時廃止
impact_routes = []
for route in routes:
    rid = route["route_id"]
    result_after, _ = engine.simulate_route_removal(
        start_id, start_time, max_time, rid, track_path=True
    )
    lost = set(result_before.keys()) - set(result_after.keys())
    if lost:
        impact_routes.append(route)

if len(impact_routes) >= 2:
    rid_list = [r["route_id"] for r in impact_routes[:3]]
    result_after, _ = engine.simulate_route_removal(
        start_id, start_time, max_time, rid_list, track_path=True
    )
    lost = set(result_before.keys()) - set(result_after.keys())

    pop_impact = pop_data.calc_impact_population(
        engine.stop_coords, list(lost), list(result_before.keys()), radius_m=300
    )
    pop_after = pop_data.get_population_near_stops(
        engine.stop_coords, list(result_after.keys()), radius_m=300
    )

    test("複数路線廃止 影響人口>=0", pop_impact["affected_total"] >= 0)
    test("複数路線廃止 影響高齢者<=影響人口",
         pop_impact["affected_elderly"] <= pop_impact["affected_total"])
    test("複数路線廃止 廃止後カバー<=廃止前",
         pop_after["total"] <= pop_before["total"] + 1,
         f"前{pop_before['total']} 後{pop_after['total']}")

    print(f"  影響路線{len(impact_routes[:3])}路線同時廃止: 到達不能{len(lost)}, 影響人口{pop_impact['affected_total']}")

print(f"  複数路線人口影響テスト完了")

# ===== 6. バス停削除 + 人口影響チェック =====
print("\n--- 6. バス停削除 + 人口影響チェック ---")

test_stops = ["東室蘭駅東口", "東室蘭駅西口", "母恋駅前"]
for stop_name in test_stops:
    remove_ids = engine.get_stop_ids_by_name(stop_name)
    if not remove_ids or set(remove_ids) & {start_id}:
        continue

    result_after, _ = engine.simulate_stop_removal(
        start_id, start_time, max_time, remove_ids, walk_distance=300, track_path=True
    )

    lost = set(result_before.keys()) - set(result_after.keys()) - set(remove_ids)

    if lost:
        pop_impact = pop_data.calc_impact_population(
            engine.stop_coords, list(lost), list(result_before.keys()), radius_m=300
        )
        test(f"バス停削除({stop_name}) 影響人口>=0",
             pop_impact["affected_total"] >= 0)
        test(f"バス停削除({stop_name}) 影響高齢者<=影響人口",
             pop_impact["affected_elderly"] <= pop_impact["affected_total"])

    # 削除バス停が結果に含まれないか
    removed_in_result = set(remove_ids) & set(result_after.keys())
    test(f"バス停削除({stop_name}) 削除バス停が結果に含まれない",
         len(removed_in_result) == 0)

    # 通過短縮効果による新規到達は警告のみ
    new_stops = set(result_after.keys()) - set(result_before.keys()) - set(remove_ids)
    if new_stops:
        warn(f"バス停削除({stop_name})", f"通過短縮効果による新規到達: {len(new_stops)}件")

print(f"  バス停削除テスト完了")

# ===== 7. 減便 + 人口影響チェック =====
print("\n--- 7. 減便 + 人口影響チェック ---")

for pct in [30, 50, 70]:
    ratio = pct / 100
    result_after, _ = engine.simulate_frequency_reduction(
        start_id, start_time, max_time, "all", reduce_ratio=ratio, track_path=True
    )

    test(f"全路線{pct}%削減 到達可能<=廃止前",
         len(result_after) <= len(result_before),
         f"前{len(result_before)} 後{len(result_after)}")

    lost = set(result_before.keys()) - set(result_after.keys())
    if lost:
        pop_impact = pop_data.calc_impact_population(
            engine.stop_coords, list(lost), list(result_before.keys()), radius_m=300
        )
        test(f"全路線{pct}%削減 影響人口>=0", pop_impact["affected_total"] >= 0)

print(f"  減便テスト完了")

# ===== 8. 既存テスト（V1から引き継ぎ） =====
print("\n--- 8. 到達圏基本チェック ---")

start_names = ["室蘭駅前", "東室蘭駅西口"]
for start_name in start_names:
    sids = engine.get_stop_ids_by_name(start_name)
    if not sids:
        continue
    sid = sids[0]

    for hour in [6, 8, 12, 18]:
        st = hour * 3600
        result, prev = engine.calc_isochrone(sid, st, max_time, track_path=True)

        test(f"到達圏({start_name},{hour}時) 出発地点含む", sid in result)

        if sid in result:
            test(f"到達圏({start_name},{hour}時) 出発時刻一致",
                 result[sid] == st)

        bad_times = [s for s, t in result.items() if t < st]
        test(f"到達圏({start_name},{hour}時) 到達時間>=出発時刻", len(bad_times) == 0)

        over_times = [s for s, t in result.items() if t > st + max_time]
        test(f"到達圏({start_name},{hour}時) 制限時間以内", len(over_times) == 0)

# 制限時間が増えると到達可能数が増える
prev_count = 0
for max_min in [15, 30, 45, 60, 90]:
    result, _ = engine.calc_isochrone(start_id, 8 * 3600, max_min * 60, track_path=True)
    test(f"制限{max_min}分 到達可能>={prev_count}",
         len(result) >= prev_count,
         f"前{prev_count} 今{len(result)}")
    prev_count = len(result)

print(f"  到達圏基本チェック完了")

# ===== 結果サマリー =====
print("\n" + "=" * 70)
print(f"テスト結果: {tests_passed}/{tests_run} パス")
if errors:
    print(f"\nエラー: {len(errors)}件")
    for e in errors:
        print(f"  {e}")
if warnings:
    print(f"\n警告: {len(warnings)}件")
    for w in warnings:
        print(f"  {w}")
if not errors and not warnings:
    print("全テスト合格！問題なし。")
print("=" * 70)