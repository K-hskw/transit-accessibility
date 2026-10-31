"""README・資料に載せる数字を、毎回同じ手順で計算し直す

数字はすべて到着（出発）時刻を5分刻みにして、時間帯の平均と幅で出す。
以前は毎時00分の1点で測っており、「休日7時に46.1%」は7時台で最も悪い1分、
「時間帯格差27.6倍」は5:00発（始発前）を5時台の代表にした値だった。

使い方（リポジトリのルートでも tools/ でも、どこから実行してもよい）:
    python tools/key_numbers.py
        室蘭（リポジトリの既定データ）の数字を出す
    python tools/key_numbers.py --region <GTFSフォルダ> <人口CSV> <拠点の停留所名>
        他地域の診断表を出す（例: 旭川電気軌道のGTFSと旭川市の100mメッシュ人口、共栄バスセンター）
"""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import numpy as np

from transit_engine import TransitEngine
from population import PopulationData
from blank_area import BlankAreaAnalyzer, arrival_times, clock

LIMIT = 3600          # 制限時間60分
HUB = "東室蘭駅東口"
ORIGIN = "室蘭駅前"
POP_CSV = "100m_mesh_pop2020_01205室蘭市.csv"


def pct(v, total):
    return f"{100 * v / total:.1f}%"


def hour_line(day, h):
    s = day.hour_summary(h)
    t = s["total_pop"]
    return (f"{h}時台 平均 {s['blank_pop']:,}人（{pct(s['blank_pop'], t)}）"
            f" 幅 {s['blank_pop_min']:,}人（{clock(s['time_min'])}着）〜"
            f"{s['blank_pop_max']:,}人（{clock(s['time_max'])}着）")


def reach_by_hour(eng, origin_id, hour):
    """その時間帯（5分刻みの12通り）に出発したときの到達停留所数"""
    return [len(eng.calc_isochrone(origin_id, int(t), LIMIT)) for t in arrival_times([hour])]


def isochrones(eng, origin_id, hour):
    """その時間帯（5分刻みの12通り）に出発したときの到達停留所の集合"""
    return [set(eng.calc_isochrone(origin_id, int(t), LIMIT)) for t in arrival_times([hour])]


def lost_by_hour(eng, origin_id, hour, edges, before, removed=()):
    """改変後のネットワークで到達できなくなる停留所数（出発時刻ごと）

    scenario() に入るたびにグラフを作り直すので、改変後の12通りは1回の scenario で
    まとめて計算する（出発時刻ごとに入ると100路線で5分かかった）。
    """
    removed = set(removed)
    with eng.scenario(*edges):
        after = isochrones(eng, origin_id, hour)
    return [len(b - a - removed) for b, a in zip(before, after)]


def muroran():
    eng = TransitEngine()
    pop = PopulationData(POP_CSV)
    an = BlankAreaAnalyzer(eng, pop, max_walk_m=500, walk_speed_m_min=67)
    hub = eng.get_stop_ids_by_name(HUB)
    origin = eng.get_stop_ids_by_name(ORIGIN)[0]

    print(f"=== 時間空白（{HUB}へ60分以内・徒歩500m・67m/分） ===")
    days = {}
    for d in ("平日", "日祝"):
        eng.set_day_type(d)
        days[d] = an.diagnose_day(hub, LIMIT)
        for h in (7, 9):
            print(f"  {d} {hour_line(days[d], h)}")
    hd7 = days["日祝"].hour_summary(7)
    at7 = days["日祝"].blank_pop_t[days["日祝"].times == 7 * 3600][0]
    print(f"  参考: 日祝 7:00ちょうど着 {at7:,.0f}人（{pct(at7, hd7['total_pop'])}）")
    print(f"  静的空白 {hd7['static_blank_pop']:,}人（ダイヤによらず一定）")

    eng.set_day_type("平日")
    print(f"\n=== 時間帯格差（{ORIGIN}から60分以内に行ける停留所数・平日） ===")
    r5, r10 = reach_by_hour(eng, origin, 5), reach_by_hour(eng, origin, 10)
    print(f"  5時台 平均 {np.mean(r5):.1f}（{min(r5)}〜{max(r5)}） / "
          f"10時台 平均 {np.mean(r10):.1f}（{min(r10)}〜{max(r10)}） / 比 {np.mean(r10) / np.mean(r5):.1f}倍")
    print(f"  参考: 00分ちょうど発 5:00 {r5[0]} / 10:00 {r10[0]}（比 {r10[0] / r5[0]:.1f}倍）")

    print(f"\n=== 路線廃止（{ORIGIN} 8時台発・60分・平日） ===")
    routes = eng.get_muroran_routes(exclude_highway=True)
    before8 = isochrones(eng, origin, 8)
    rows = []
    for r in routes:
        lost = lost_by_hour(eng, origin, 8, eng.edges_after_route_removal(r["route_id"]), before8)
        rows.append((float(np.mean(lost)), max(lost), str(r["route_name"])))
    rows.sort(key=lambda x: (x[0], x[1]), reverse=True)
    hit = [x for x in rows if x[1] > 0]
    print(f"  路線 {len(routes)}（高速・都市間を除く）のうち、8時台のどこかの出発時刻で"
          f"到達不能を生むのは {len(hit)} 路線")
    for mean, mx, name in hit[:5]:
        print(f"    {name}: 平均 {mean:.1f} / 最大 {mx} 停留所")

    print(f"\n=== 全路線一律の減便（{ORIGIN} 8時台発・60分・平日） ===")
    n_trips = eng.bus_edges["trip_id"].nunique()
    for ratio in (0.3, 0.4, 0.5):
        edges = eng.edges_after_frequency_reduction("all", reduce_ratio=ratio)
        actual = 1 - edges[0]["trip_id"].nunique() / n_trips
        lost = lost_by_hour(eng, origin, 8, edges, before8)
        print(f"  {int(ratio * 100)}%指定（実際 {100 * actual:.1f}%）: 到達不能 平均 {np.mean(lost):.1f}"
              f"（{min(lost)}〜{max(lost)}）停留所")

    print(f"\n=== 主要停留所の削除（{HUB}・{ORIGIN} 8時台発・平日） ===")
    rm = eng.get_stop_ids_by_name(HUB)
    lost = lost_by_hour(eng, origin, 8, eng.edges_after_stop_removal(rm, 300), before8, removed=rm)
    print(f"  削除した停留所以外で到達不能になる停留所: 平均 {np.mean(lost):.1f}（最大 {max(lost)}）")

    print("\n=== 処方の候補（日祝 7時台・空白地域の停留所を通るか） ===")
    from prescription import rank_routes_by_blank_coverage
    eng.set_day_type("日祝")
    diag = days["日祝"].hour_mesh(7)
    cand = rank_routes_by_blank_coverage(eng, an, diag)
    trips = eng.bus_edges.groupby("route_id")["trip_id"].nunique()
    busiest = trips.idxmax()
    stops = set(eng.bus_edges.loc[eng.bus_edges["route_id"] == busiest, "from_stop"]) | \
        set(eng.bus_edges.loc[eng.bus_edges["route_id"] == busiest, "to_stop"])
    row = cand[cand["route_id"] == busiest]
    covered = int(row["空白地域の停留所"].iloc[0]) if len(row) else 0
    print(f"  最多便の路線 {eng.route_names.get(busiest)}（{trips.max()}便）: "
          f"{len(stops)}停留所のうち空白地域 {covered}")
    print(f"  候補の筆頭 {cand.iloc[0]['route_name']}: 空白地域 {cand.iloc[0]['空白地域の停留所']}停留所・"
          f"{cand.iloc[0]['便数']}便")


def other_region(gtfs_dir, pop_csv, hub_name):
    from build_network import build_network
    work = tempfile.mkdtemp(prefix="key_numbers_")
    t0 = time.time()
    build_network(gtfs_dir, work)
    t_build = time.time() - t0
    eng = TransitEngine(gtfs_dir=gtfs_dir, edges_dir=work)
    pop = PopulationData(pop_csv)
    an = BlankAreaAnalyzer(eng, pop, max_walk_m=500, walk_speed_m_min=67)
    hub = eng.get_stop_ids_by_name(hub_name)
    if not hub:
        raise SystemExit(f"拠点 {hub_name} が見つかりません")
    print(f"\n=== {hub_name}へ60分以内（ネットワーク構築 {t_build:.1f}秒） ===")
    print(f"  停留所 {len(eng.stops):,} / 人口 {pop._pop.sum():,.0f} / メッシュ {an.n_mesh:,}")
    for d in eng.available_day_types:
        eng.set_day_type(d)
        t0 = time.time()
        day = an.diagnose_day(hub, LIMIT)
        print(f"  {d}（{eng.bus_edges['trip_id'].nunique()}便、216通りの診断 {time.time() - t0:.1f}秒）")
        for h in (7, 9, 17, 20):
            print(f"    {hour_line(day, h)}")
        s = day.hour_summary(9)
        print(f"    9時台の内訳: 静的空白 {s['static_blank_pop']:,} ＋ 時間空白 "
              f"{s['blank_pop'] - s['static_blank_pop']:,}（空白人口のうち高齢者 {s['blank_elderly']:,}）")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--region":
        if len(sys.argv) != 5:
            raise SystemExit(__doc__)
        other_region(*sys.argv[2:5])
    else:
        muroran()
