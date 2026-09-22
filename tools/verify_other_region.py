"""他地域のGTFSでコードを変えずに動くかを検証する

「GTFSデータを差し替えれば全国どの地域にも適用可能」という主張の実証用。
室蘭専用の処理が残っていればここで落ちるか、不自然な結果になる。

使い方:
    python tools/verify_other_region.py <GTFSを展開したフォルダ>

例（北海道オープンデータの網走バス）:
    https://ckan.hoda.jp/dataset/gtfs-data から abashiri_bus.zip を取得・展開し
    python tools/verify_other_region.py C:\\path\\to\\abashiri

リポジトリのコードをそのまま一時フォルダへコピーし、GTFSだけ差し替えて動かす。
室蘭のデータや生成物は一切書き換えない。
"""
import os
import shutil
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULES = ["transit_engine.py", "build_network.py", "population.py",
           "blank_area.py", "prescription.py", "service_calendar.py"]


def main(gtfs_dir):
    if not os.path.isfile(os.path.join(gtfs_dir, "stop_times.txt")):
        raise SystemExit(f"stop_times.txt が見つかりません: {gtfs_dir}")

    work = os.path.join(tempfile.gettempdir(), "verify_other_region")
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(work)
    for m in MODULES:
        shutil.copy2(os.path.join(REPO, m), work)
    shutil.copytree(gtfs_dir, os.path.join(work, "gtfs_data"))

    os.chdir(work)
    sys.path.insert(0, work)

    print("=== 1. ネットワーク構築 ===")
    from build_network import build_network
    t0 = time.time()
    build_network("gtfs_data", ".")
    print(f"  所要 {time.time() - t0:.1f}秒")

    print("\n=== 2. エンジン読込とダイヤ種別 ===")
    from transit_engine import TransitEngine
    eng = TransitEngine()
    print(f"  停留所 {len(eng.stops):,} / 全エッジ {len(eng.all_bus_edges):,}")
    for d in eng.available_day_types:
        eng.set_day_type(d)
        print(f"    {d}: {eng.bus_edges['trip_id'].nunique()}便 / {len(eng.bus_edges):,}エッジ")
    eng.set_day_type(eng.available_day_types[0])

    print("\n=== 3. 地域依存が残っていないか ===")
    names = eng.get_stop_names()
    muroran = sum(1 for n in names if "室蘭" in str(n))
    print(f"  停留所名 {len(names)}種 / 『室蘭』を含む: {muroran} 件（0なら地域依存なし）")
    print(f"  路線一覧: {len(eng.get_muroran_routes())}件")

    print("\n=== 4. 到達圏・逆方向 ===")
    busiest = eng.bus_edges["from_stop"].value_counts().index[0]
    bname = eng.stop_coords.loc[busiest, "stop_name"]
    for h in (8, 12, 17):
        r = eng.calc_isochrone(busiest, h * 3600, 3600)
        print(f"  {bname} {h}時発60分: {len(r):>4}停留所")
    rev = eng.calc_reverse_isochrone(busiest, 9 * 3600, 3600)
    print(f"  {bname} 9時着60分の集客圏: {len(rev)}停留所")

    print("\n=== 5. 施策シナリオ ===")
    rid = eng.bus_edges["route_id"].value_counts().index[0]
    base = len(eng.calc_isochrone(busiest, 8 * 3600, 3600))
    with eng.scenario(*eng.edges_after_route_removal(rid)):
        after = len(eng.calc_isochrone(busiest, 8 * 3600, 3600))
    with eng.scenario(*eng.edges_after_frequency_increase(rid, 6, 9, 1)):
        inc = len(eng.calc_isochrone(busiest, 8 * 3600, 3600))
    print(f"  最多便路線({eng.route_names.get(rid)}) 廃止: {base} → {after} / 増便: {base} → {inc}")
    print(f"  路線長: {eng.route_length_km(rid):.1f} km")

    print("\n=== 結論 ===")
    print("  コードを変更せずに、構築・到達圏・逆方向・施策まで動作した")
    print(f"  作業フォルダ: {work}（室蘭のデータは書き換えていない）")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
