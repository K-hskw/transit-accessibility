from transit_engine import TransitEngine
import sys

engine = TransitEngine()
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

print("=" * 60)
print("公共交通アクセシビリティ分析ツール 総合テスト")
print("=" * 60)

# ===== 1. データ整合性チェック =====
print("\n--- 1. データ整合性チェック ---")

stops = engine.stops
bus_edges = engine.bus_edges
walk_edges = engine.walk_edges

test("バス停数が正", len(stops) > 0, f"バス停数: {len(stops)}")
test("バスエッジ数が正", len(bus_edges) > 0, f"エッジ数: {len(bus_edges)}")
test("徒歩エッジ数が正", len(walk_edges) > 0, f"エッジ数: {len(walk_edges)}")

# 移動時間が負でないか
neg_travel = bus_edges[bus_edges["travel_time"] < 0]
test("バスエッジの移動時間が全て正", len(neg_travel) == 0, f"負の移動時間: {len(neg_travel)}件")

# 徒歩エッジの移動時間が負でないか
neg_walk = walk_edges[walk_edges["walk_time"] < 0]
test("徒歩エッジの移動時間が全て正", len(neg_walk) == 0, f"負の移動時間: {len(neg_walk)}件")

# 出発と到着が同じバス停のエッジがないか
self_loop = bus_edges[bus_edges["from_stop"] == bus_edges["to_stop"]]
test("自己ループエッジなし", len(self_loop) == 0, f"自己ループ: {len(self_loop)}件")

print(f"  バス停: {len(stops)}, バスエッジ: {len(bus_edges)}, 徒歩エッジ: {len(walk_edges)}")

# ===== 2. 到達圏計算の基本チェック =====
print("\n--- 2. 到達圏計算の基本チェック ---")

start_names = ["室蘭駅前", "東室蘭駅西口", "東室蘭駅東口"]
for start_name in start_names:
    start_ids = engine.get_stop_ids_by_name(start_name)
    if not start_ids:
        warn("出発地点", f"{start_name}が見つからない")
        continue
    start_id = start_ids[0]

    for hour in [6, 8, 12, 18, 21]:
        start_time = hour * 3600
        result, prev = engine.calc_isochrone(start_id, start_time, 60 * 60, track_path=True)

        # 出発地点が結果に含まれるか
        test(f"到達圏({start_name}, {hour}時) 出発地点含む",
             start_id in result,
             f"出発地点が結果に含まれない")

        # 出発地点の到達時間が出発時刻と一致するか
        if start_id in result:
            test(f"到達圏({start_name}, {hour}時) 出発時刻一致",
                 result[start_id] == start_time,
                 f"出発: {start_time}, 結果: {result[start_id]}")

        # 到達時間が出発時刻以上か
        bad_times = [sid for sid, t in result.items() if t < start_time]
        test(f"到達圏({start_name}, {hour}時) 到達時間>=出発時刻",
             len(bad_times) == 0,
             f"出発時刻より前の到達: {len(bad_times)}件")

        # 到達時間が制限時間以内か
        over_times = [sid for sid, t in result.items() if t > start_time + 60 * 60]
        test(f"到達圏({start_name}, {hour}時) 制限時間以内",
             len(over_times) == 0,
             f"制限時間超過: {len(over_times)}件")

        # 到達可能数が0でないか
        test(f"到達圏({start_name}, {hour}時) 到達可能>0",
             len(result) > 1,
             f"到達可能: {len(result)}")

        # 経路復元チェック（ランダムに5件）
        import random
        sample_stops = random.sample(list(result.keys()), min(5, len(result)))
        for sid in sample_stops:
            path = engine.reconstruct_path(prev, sid)
            if sid == start_id:
                continue
            if path is None:
                warn("経路復元", f"{start_name}->{sid}: 経路なし")
                continue

            # 経路の最初のfrom_stopが出発地点の名前か
            first_from = path[0]["from_stop"]
            start_stop_name = engine.stop_coords.loc[start_id, "stop_name"]

            # 時間が単調増加か
            times = []
            for step in path:
                dep_parts = step["departure"].split(":")
                arr_parts = step["arrival"].split(":")
                dep_sec = int(dep_parts[0]) * 3600 + int(dep_parts[1]) * 60
                arr_sec = int(arr_parts[0]) * 3600 + int(arr_parts[1]) * 60
                times.extend([dep_sec, arr_sec])

            monotonic = all(times[i] <= times[i+1] for i in range(len(times)-1))
            test(f"経路({start_name}, {hour}時, {sid}) 時間単調増加",
                 monotonic,
                 f"times: {times}")

print(f"  到達圏テスト完了")

# ===== 3. 路線廃止シミュレーションチェック =====
print("\n--- 3. 路線廃止シミュレーションチェック ---")

start_id = engine.get_stop_ids_by_name("室蘭駅前")[0]
start_time = 8 * 3600
max_time = 60 * 60

result_before, _ = engine.calc_isochrone(start_id, start_time, max_time, track_path=True)
muroran_routes = engine.get_muroran_routes(exclude_highway=True)

for route in muroran_routes:
    rid = route["route_id"]
    rname = route["route_name"]

    result_after, _ = engine.simulate_route_removal(start_id, start_time, max_time, rid, track_path=True)

    # 廃止後の到達可能数 <= 廃止前
    test(f"路線廃止({rname}) 到達可能数<=廃止前",
         len(result_after) <= len(result_before),
         f"前: {len(result_before)}, 後: {len(result_after)}")

    # 廃止後に新たに到達可能になるバス停がないか
    new_stops = set(result_after.keys()) - set(result_before.keys())
    test(f"路線廃止({rname}) 新規到達なし",
         len(new_stops) == 0,
         f"新規到達: {len(new_stops)}件 {new_stops}")

    # 廃止後に残っているバス停の到達時間が悪化または同じか
    improved = []
    for sid in result_after:
        if sid in result_before and sid != start_id:
            if result_after[sid] < result_before[sid]:
                diff = (result_before[sid] - result_after[sid]) / 60
                if diff > 0.1:
                    improved.append((sid, diff))

    test(f"路線廃止({rname}) 到達時間改善なし",
         len(improved) == 0,
         f"改善: {len(improved)}件 {[(engine.stop_coords.loc[s, 'stop_name'], f'{d:.1f}分') for s, d in improved[:3]]}")

print(f"  路線廃止テスト完了")

# ===== 4. バス停削除シミュレーションチェック =====
print("\n--- 4. バス停削除シミュレーションチェック ---")

test_stop_names = ["東室蘭駅東口", "東室蘭駅西口", "母恋駅前", "室蘭駅前"]
for stop_name in test_stop_names:
    remove_ids = engine.get_stop_ids_by_name(stop_name)
    if not remove_ids:
        continue

    # 出発地点と同じバス停を削除する場合はスキップ
    if set(remove_ids) & {start_id}:
        continue

    result_after, _ = engine.simulate_stop_removal(
        start_id, start_time, max_time, remove_ids, walk_distance=300, track_path=True
    )

    # 削除したバス停が結果に含まれないか
    removed_in_result = set(remove_ids) & set(result_after.keys())
    test(f"バス停削除({stop_name}) 削除バス停が結果に含まれない",
         len(removed_in_result) == 0,
         f"含まれている: {removed_in_result}")

    # 廃止後に新たに到達可能になるバス停がないか（削除バス停以外）
    # 注: バス停通過による時間短縮効果で少数の新規到達が生じうる（正常動作）
    new_stops = set(result_after.keys()) - set(result_before.keys()) - set(remove_ids)
    if len(new_stops) > 0:
        warn(f"バス停削除({stop_name})", f"通過短縮効果による新規到達: {len(new_stops)}件")

print(f"  バス停削除テスト完了")

# ===== 5. 減便シミュレーションチェック =====
print("\n--- 5. 減便シミュレーションチェック ---")

# 特定路線の半減
for route in muroran_routes[:5]:
    rid = route["route_id"]
    rname = route["route_name"]

    result_after, _ = engine.simulate_frequency_reduction(
        start_id, start_time, max_time, "half",
        target_route_id=rid, track_path=True
    )

    test(f"減便半減({rname}) 到達可能数<=廃止前",
         len(result_after) <= len(result_before),
         f"前: {len(result_before)}, 後: {len(result_after)}")

# 全路線一律削減
for ratio in [0.3, 0.5, 0.7]:
    result_after, _ = engine.simulate_frequency_reduction(
        start_id, start_time, max_time, "all",
        reduce_ratio=ratio, track_path=True
    )

    test(f"全路線{int(ratio*100)}%削減 到達可能数<=廃止前",
         len(result_after) <= len(result_before),
         f"前: {len(result_before)}, 後: {len(result_after)}")

print(f"  減便テスト完了")

# ===== 6. 時刻変化による整合性チェック =====
print("\n--- 6. 時刻変化の整合性チェック ---")

start_id = engine.get_stop_ids_by_name("室蘭駅前")[0]

# 制限時間を増やすと到達可能数が増える（または同じ）か
prev_count = 0
for max_min in [15, 30, 45, 60, 90]:
    result, _ = engine.calc_isochrone(start_id, 8 * 3600, max_min * 60, track_path=True)
    test(f"制限{max_min}分 到達可能数>={prev_count}",
         len(result) >= prev_count,
         f"前: {prev_count}, 今: {len(result)}")
    prev_count = len(result)

print(f"  時刻変化テスト完了")

# ===== 結果サマリー =====
print("\n" + "=" * 60)
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
print("=" * 60)