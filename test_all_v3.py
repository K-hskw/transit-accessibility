from transit_engine import TransitEngine
from population import PopulationData, FacilityData

engine = TransitEngine()
pop_data = PopulationData("100m_mesh_pop2020_01205室蘭市.csv")
facility_data = FacilityData("facilities.csv")

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
print("総合テスト V3（施設アクセス連動を含む）")
print("=" * 70)

start_id = engine.get_stop_ids_by_name("室蘭駅前")[0]
start_time = 8 * 3600
max_time = 60 * 60

result_before, prev_before = engine.calc_isochrone(start_id, start_time, max_time, track_path=True)
print(f"  基準: 室蘭駅前 8時発 60分 → {len(result_before)}箇所到達可能")

# ===== 1. 施設アクセス基本チェック =====
print("\n--- 1. 施設アクセス基本チェック ---")

for ftype in facility_data.facility_types:
    facilities = facility_data.get_facilities_by_type(ftype)
    access = facility_data.calc_facility_access(
        result_before, start_time, facilities, engine.stop_coords
    )
    accessible = sum(1 for a in access if a["accessible"])
    test(f"施設({ftype}) 結果数==施設数", len(access) == len(facilities),
         f"結果{len(access)} != 施設{len(facilities)}")
    test(f"施設({ftype}) アクセス可能>=0", accessible >= 0)
    test(f"施設({ftype}) アクセス可能<=総数", accessible <= len(access))

    for a in access:
        if a["accessible"]:
            test(f"施設({a['facility_name']}) 合計時間>0",
                 a["total_time_min"] > 0, f"合計時間: {a['total_time_min']}")
            test(f"施設({a['facility_name']}) 徒歩時間>=0",
                 a["walk_time_min"] >= 0)
        test(f"施設({a['facility_name']}) 徒歩距離>=0",
             a["walk_distance_m"] >= 0)

    print(f"  {ftype}: {accessible}/{len(access)}件アクセス可能")

# ===== 2. 単一路線廃止 + 施設アクセス連動 =====
print("\n--- 2. 路線廃止 + 施設アクセス連動チェック ---")

routes = engine.get_muroran_routes(exclude_highway=True)
for route in routes[:10]:
    rid = route["route_id"]
    rname = route["route_name"]
    result_after, _ = engine.simulate_route_removal(
        start_id, start_time, max_time, rid, track_path=True
    )

    for ftype in facility_data.facility_types:
        facilities = facility_data.get_facilities_by_type(ftype)
        acc_before = facility_data.calc_facility_access(
            result_before, start_time, facilities, engine.stop_coords
        )
        acc_after = facility_data.calc_facility_access(
            result_after, start_time, facilities, engine.stop_coords
        )
        before_count = sum(1 for a in acc_before if a["accessible"])
        after_count = sum(1 for a in acc_after if a["accessible"])

        test(f"路線廃止({rname[:20]}) {ftype} 廃止後<=廃止前",
             after_count <= before_count,
             f"前{before_count} 後{after_count}")

        # アクセス不能になった施設のチェック
        for ab, aa in zip(acc_before, acc_after):
            if ab["accessible"] and not aa["accessible"]:
                test(f"路線廃止({rname[:20]}) {ab['facility_name']} 廃止前は到達可能だった",
                     ab["total_time_min"] is not None and ab["total_time_min"] > 0)

# ===== 3. 複数路線同時廃止 + 施設アクセス =====
print("\n--- 3. 複数路線同時廃止 + 施設アクセス ---")

if len(routes) >= 3:
    rid_list = [routes[0]["route_id"], routes[1]["route_id"], routes[2]["route_id"]]
    result_after, _ = engine.simulate_route_removal(
        start_id, start_time, max_time, rid_list, track_path=True
    )
    for ftype in facility_data.facility_types:
        facilities = facility_data.get_facilities_by_type(ftype)
        acc_before = facility_data.calc_facility_access(
            result_before, start_time, facilities, engine.stop_coords
        )
        acc_after = facility_data.calc_facility_access(
            result_after, start_time, facilities, engine.stop_coords
        )
        before_count = sum(1 for a in acc_before if a["accessible"])
        after_count = sum(1 for a in acc_after if a["accessible"])
        test(f"3路線同時廃止 {ftype} 廃止後<=廃止前",
             after_count <= before_count,
             f"前{before_count} 後{after_count}")

# ===== 4. バス停削除 + 施設アクセス =====
print("\n--- 4. バス停削除 + 施設アクセス ---")

test_stops = ["東室蘭駅東口", "東室蘭駅西口", "母恋駅前", "中島町４丁目"]
for stop_name in test_stops:
    remove_ids = engine.get_stop_ids_by_name(stop_name)
    if not remove_ids or set(remove_ids) & {start_id}:
        continue

    result_after, _ = engine.simulate_stop_removal(
        start_id, start_time, max_time, remove_ids, walk_distance=300, track_path=True
    )

    test(f"バス停削除({stop_name}) 到達可能数>=1",
         len(result_after) >= 1, f"到達可能: {len(result_after)}")

    # 削除バス停が結果に含まれないか
    removed_in_result = set(remove_ids) & set(result_after.keys())
    test(f"バス停削除({stop_name}) 削除バス停が結果に含まれない",
         len(removed_in_result) == 0)

    for ftype in facility_data.facility_types:
        facilities = facility_data.get_facilities_by_type(ftype)
        acc_before = facility_data.calc_facility_access(
            result_before, start_time, facilities, engine.stop_coords
        )
        acc_after = facility_data.calc_facility_access(
            result_after, start_time, facilities, engine.stop_coords
        )
        before_count = sum(1 for a in acc_before if a["accessible"])
        after_count = sum(1 for a in acc_after if a["accessible"])

        # 通過短縮効果で新たにアクセス可能になるケースがあり得る
        if after_count > before_count:
            warn(f"バス停削除({stop_name}) {ftype}",
                 f"通過短縮効果? 前{before_count} 後{after_count}")

    print(f"  {stop_name}: 削除後{len(result_after)}箇所到達可能")

# ===== 5. 減便 + 施設アクセス =====
print("\n--- 5. 減便 + 施設アクセス ---")

for pct in [30, 50, 70]:
    ratio = pct / 100
    result_after, _ = engine.simulate_frequency_reduction(
        start_id, start_time, max_time, "all", reduce_ratio=ratio, track_path=True
    )

    test(f"全路線{pct}%削減 到達可能数<=廃止前",
         len(result_after) <= len(result_before),
         f"前{len(result_before)} 後{len(result_after)}")

    for ftype in facility_data.facility_types:
        facilities = facility_data.get_facilities_by_type(ftype)
        acc_before = facility_data.calc_facility_access(
            result_before, start_time, facilities, engine.stop_coords
        )
        acc_after = facility_data.calc_facility_access(
            result_after, start_time, facilities, engine.stop_coords
        )
        before_count = sum(1 for a in acc_before if a["accessible"])
        after_count = sum(1 for a in acc_after if a["accessible"])
        test(f"全路線{pct}%削減 {ftype} 減便後<=減便前",
             after_count <= before_count,
             f"前{before_count} 後{after_count}")

    print(f"  {pct}%削減: {len(result_after)}箇所到達可能")

# ===== 6. 東室蘭駅東口削除の詳細検証 =====
print("\n--- 6. 東室蘭駅東口削除の詳細検証 ---")

remove_ids = engine.get_stop_ids_by_name("東室蘭駅東口")
result_after, _ = engine.simulate_stop_removal(
    start_id, start_time, max_time, remove_ids, walk_distance=300, track_path=True
)
print(f"  廃止前: {len(result_before)}箇所")
print(f"  廃止後: {len(result_after)}箇所")
lost = set(result_before.keys()) - set(result_after.keys()) - set(remove_ids)
print(f"  到達不能: {len(lost)}箇所")

# 徒歩圏500mでも試す
result_after_500, _ = engine.simulate_stop_removal(
    start_id, start_time, max_time, remove_ids, walk_distance=500, track_path=True
)
print(f"  廃止後(500m圏): {len(result_after_500)}箇所")

# 出発地点を変えて検証
for alt_start in ["東室蘭駅西口"]:
    alt_ids = engine.get_stop_ids_by_name(alt_start)
    if alt_ids:
        alt_result, _ = engine.calc_isochrone(alt_ids[0], start_time, max_time, track_path=True)
        alt_after, _ = engine.simulate_stop_removal(
            alt_ids[0], start_time, max_time, remove_ids, walk_distance=300, track_path=True
        )
        print(f"  {alt_start}発: 廃止前{len(alt_result)} → 廃止後{len(alt_after)}")

# ===== 7. 人口 + 施設 整合性 =====
print("\n--- 7. 人口 + 施設 整合性チェック ---")

pop_before = pop_data.get_population_near_stops(
    engine.stop_coords, list(result_before.keys()), radius_m=300
)
test("カバー人口 > 0", pop_before["total"] > 0)
test("カバー高齢者 <= カバー人口", pop_before["elderly"] <= pop_before["total"])

total_pop = round(pop_data.df["PopT"].sum())
test("カバー人口 <= 総人口", pop_before["total"] <= total_pop)

print(f"  カバー人口: {pop_before['total']}, 高齢者: {pop_before['elderly']}")

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