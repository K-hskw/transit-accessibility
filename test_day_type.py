from transit_engine import TransitEngine
from population import PopulationData
from blank_area import BlankAreaAnalyzer
from service_calendar import DAY_TYPES

engine = TransitEngine()
pop_data = PopulationData("100m_mesh_pop2020_01205室蘭市.csv")

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
print("ダイヤ種別（平日 / 土曜 / 日祝）テスト")
print("=" * 70)

# ===== 1. ダイヤ種別の切り替え =====
print("\n--- 1. 切り替え ---")
test("既定は平日", engine.day_type == "平日", engine.day_type)
test("3種別とも便がある", set(engine.available_day_types) == set(DAY_TYPES),
     str(engine.available_day_types))

counts = {}
edges = {}
for d in DAY_TYPES:
    engine.set_day_type(d)
    counts[d] = engine.bus_edges["trip_id"].nunique()
    edges[d] = len(engine.bus_edges)
    print(f"  {d}: {counts[d]}便 / {edges[d]:,}エッジ")

test("種別ごとに便数が異なる", len(set(counts.values())) > 1, str(counts))
test("全種別の合計が全便を超えない",
     sum(counts.values()) <= engine.all_bus_edges["trip_id"].nunique() + 1,
     f"{sum(counts.values())} vs {engine.all_bus_edges['trip_id'].nunique()}")
test("平日の便数が最多", counts["平日"] == max(counts.values()), str(counts))

engine.set_day_type("平日")
test("切り替えて戻すと元に戻る", len(engine.bus_edges) == edges["平日"])

# ===== 2. キャッシュが破棄されるか =====
print("\n--- 2. グラフキャッシュ ---")
start = engine.get_stop_ids_by_name("室蘭駅前")[0]
r_weekday = engine.calc_isochrone(start, 8 * 3600, 3600)
engine.set_day_type("日祝")
r_sunday = engine.calc_isochrone(start, 8 * 3600, 3600)
test("ダイヤを変えると到達圏が変わる（キャッシュが残っていない）",
     len(r_weekday) != len(r_sunday), f"平日{len(r_weekday)} / 日祝{len(r_sunday)}")
print(f"  室蘭駅前 8時発60分: 平日 {len(r_weekday)}箇所 / 日祝 {len(r_sunday)}箇所")

try:
    engine.set_day_type("木曜")
    test("未知のダイヤ種別でエラーになる", False, "例外が出なかった")
except ValueError:
    test("未知のダイヤ種別でエラーになる", True)
engine.set_day_type("平日")

# ===== 3. 空白人口のダイヤ別比較 =====
print("\n--- 3. 空白人口のダイヤ別比較 ---")
analyzer = BlankAreaAnalyzer(engine, pop_data, max_walk_m=500, walk_speed_m_min=67)
dest = engine.get_stop_ids_by_name("東室蘭駅東口")
blank = {}
for d in DAY_TYPES:
    engine.set_day_type(d)
    blank[d] = analyzer.summarize(analyzer.diagnose(dest, 9 * 3600, 3600))
    print(f"  {d}: 空白人口 {blank[d]['blank_pop']:,}人 "
          f"(高齢者 {blank[d]['blank_elderly']:,}人)")

# 平日と土日は互いに素な時刻表なので、どちらの空白人口が大きいかは
# 時間帯によって変わる（休日は朝が壊滅的だが日中は平日と同等以上）。
# 大小関係を決め打ちで検証してはいけない。
test("ダイヤ種別で空白人口が変わる",
     blank["平日"]["blank_pop"] != blank["日祝"]["blank_pop"],
     f"平日{blank['平日']['blank_pop']} / 日祝{blank['日祝']['blank_pop']}")
test("静的空白はダイヤに依存しない",
     blank["平日"]["static_blank_pop"] == blank["日祝"]["static_blank_pop"],
     f"{blank['平日']['static_blank_pop']} vs {blank['日祝']['static_blank_pop']}")

print("  時間帯別（空白人口）:")
for h in [7, 9, 12, 17, 20]:
    row = []
    for d in DAY_TYPES:
        engine.set_day_type(d)
        row.append(analyzer.summarize(analyzer.diagnose(dest, h * 3600, 3600))["blank_pop"])
    print(f"    {h:2d}時着  " + " / ".join(f"{d} {v:,}" for d, v in zip(DAY_TYPES, row)))

# ===== 4. 単調性（便を増やせば到達範囲は広がる） =====
# 平日と土曜は互いに素なので大小比較はできないが、和集合は必ず各々以上になる。
# 時刻依存ダイクストラの実装が壊れていればここで検出できる。
print("\n--- 4. 単調性 ---")
from service_calendar import trip_ids_for_day_type

start_id = engine.get_stop_ids_by_name("室蘭駅前")[0]
dest_id = dest[0]
wd_trips = trip_ids_for_day_type(engine.trips, engine.calendar, "平日")
sa_trips = trip_ids_for_day_type(engine.trips, engine.calendar, "土曜")


def reach_with(trip_ids):
    engine.bus_edges = engine.all_bus_edges[
        engine.all_bus_edges["trip_id"].isin(trip_ids)
    ].reset_index(drop=True)
    engine.clear_graph_cache()
    return (set(engine.calc_isochrone(start_id, 8 * 3600, 3600)),
            set(engine.calc_reverse_isochrone(dest_id, 9 * 3600, 3600)))


f_wd, r_wd = reach_with(wd_trips)
f_sa, r_sa = reach_with(sa_trips)
f_un, r_un = reach_with(wd_trips | sa_trips)
test("順方向: 和集合は平日・土曜の到達範囲を包含する",
     f_un >= f_wd and f_un >= f_sa,
     f"平日{len(f_wd)} 土曜{len(f_sa)} 和集合{len(f_un)}")
test("逆方向: 和集合は平日・土曜の到達範囲を包含する",
     r_un >= r_wd and r_un >= r_sa,
     f"平日{len(r_wd)} 土曜{len(r_sa)} 和集合{len(r_un)}")
print(f"  順方向 平日{len(f_wd)} / 土曜{len(f_sa)} / 和集合{len(f_un)}")
print(f"  逆方向 平日{len(r_wd)} / 土曜{len(r_sa)} / 和集合{len(r_un)}")

engine.set_day_type("土曜")   # bus_edges を直接触ったので正規の経路で入れ直す
engine.set_day_type("平日")
test("直接操作後もダイヤ切替で正しい状態に戻る",
     len(engine.bus_edges) == edges["平日"],
     f"{len(engine.bus_edges)} vs {edges['平日']}")

# ===== 5. service_id をゼロ詰めのまま扱えるか =====
# "01" のようなIDを pandas が数値と推測して 1 に変換すると、trips.txt 側
# （他の値が混在して文字列のまま）と突合できず全ダイヤが0便になる。
# 旭川電気軌道のフィードで実際に起きた不具合の再発防止。
print("\n--- 5. ゼロ詰め service_id ---")
import io as _io
import pandas as _pd
from service_calendar import trip_ids_for_day_type as _tids

_cal = _pd.read_csv(_io.StringIO(
    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday\n"
    "01,1,1,1,1,1,0,0\n"
    "03,0,0,0,0,0,1,1\n"), dtype={"service_id": str})
_trips = _pd.read_csv(_io.StringIO(
    "service_id,trip_id\n01,t1\n01,t2\n03,t3\n0770-1_2026/09/01,t4\n"),
    dtype={"service_id": str})

test("ゼロ詰めIDが文字列として保持される",
     list(_cal["service_id"]) == ["01", "03"], str(list(_cal["service_id"])))
test("ゼロ詰めIDで平日の便を拾える",
     _tids(_trips, _cal, "平日") == {"t1", "t2"},
     str(_tids(_trips, _cal, "平日")))
test("ゼロ詰めIDで土曜の便を拾える",
     _tids(_trips, _cal, "土曜") == {"t3"}, str(_tids(_trips, _cal, "土曜")))
test("calendar に無い service_id の便は拾わない",
     "t4" not in _tids(_trips, _cal, "平日") | _tids(_trips, _cal, "土曜"))

# 実データ側でも service_id が文字列で読めていること
test("読み込んだ calendar の service_id が文字列",
     engine.calendar["service_id"].map(type).eq(str).all(),
     str(engine.calendar["service_id"].map(type).unique()))
test("読み込んだ trips の service_id が文字列",
     engine.trips["service_id"].map(type).eq(str).all())

print("\n" + "=" * 70)
print(f"テスト結果: {tests_passed}/{tests_run} パス")
if errors:
    print(f"\nエラー: {len(errors)}件")
    for e in errors:
        print(f"  {e}")
else:
    print("全テスト合格！問題なし。")
print("=" * 70)
