import pandas as pd
from math import radians, sin, cos, sqrt, atan2
import os

from service_calendar import describe_day_types

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))

def time_to_seconds(t):
    parts = t.split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])

def build_network(gtfs_dir="gtfs_data", output_dir="."):
    """GTFSデータからバスエッジ・徒歩エッジを生成して保存する"""

    # データ読み込み
    stops = pd.read_csv(os.path.join(gtfs_dir, "stops.txt"))
    stop_times = pd.read_csv(os.path.join(gtfs_dir, "stop_times.txt"))
    # service_id は文字列として読む（"01" が 1 に変換されて突合できなくなるため）
    trips = pd.read_csv(os.path.join(gtfs_dir, "trips.txt"), dtype={"service_id": str})
    calendar = pd.read_csv(os.path.join(gtfs_dir, "calendar.txt"), dtype={"service_id": str})

    # ダイヤ種別で絞り込まず全便のエッジを作る。
    # 平日/土曜/日祝の切り替えは TransitEngine 側で行う（種別ごとに
    # ネットワークを作り直さずに済み、休日の空白も同じデータで測れる）。
    all_stop_times = stop_times[stop_times["trip_id"].isin(set(trips["trip_id"]))].copy()

    # 通過時刻を省略するGTFS（timepointのみ記載）があるため、時刻が無い
    # 停車レコードは落とす。落とさないと time_to_seconds が NaN で落ちる。
    before = len(all_stop_times)
    all_stop_times = all_stop_times.dropna(subset=["arrival_time", "departure_time"])
    if len(all_stop_times) < before:
        print(f"時刻が空の停車レコードを除外: {before - len(all_stop_times)} 件")

    print(f"全便数: {all_stop_times['trip_id'].nunique()}")
    print(f"全停車レコード数: {len(all_stop_times)}")
    print(describe_day_types(trips, calendar))

    # 時刻を秒に変換
    all_stop_times["arrival_sec"] = all_stop_times["arrival_time"].apply(time_to_seconds)
    all_stop_times["departure_sec"] = all_stop_times["departure_time"].apply(time_to_seconds)

    # バス移動エッジを作成
    weekday_stop_times = all_stop_times.sort_values(["trip_id", "stop_sequence"])

    edges = []
    for trip_id, group in weekday_stop_times.groupby("trip_id"):
        rows = group.values
        cols = group.columns.tolist()
        stop_id_idx = cols.index("stop_id")
        dep_idx = cols.index("departure_sec")
        arr_idx = cols.index("arrival_sec")

        for i in range(len(rows) - 1):
            from_stop = rows[i][stop_id_idx]
            to_stop = rows[i+1][stop_id_idx]
            dep_time = int(rows[i][dep_idx])
            arr_time = int(rows[i+1][arr_idx])
            travel_time = arr_time - dep_time

            # GTFS-JPの時刻は分単位のため、近接する停留所間は travel_time が
            # 0 になる。これを除外すると便の連鎖が切れ、その先の停留所が
            # 到達不能になる（道南バスでは2,737区間・148停留所が消えていた）。
            # 時刻表の時刻は絶対値なので0秒エッジを残しても誤差は停留所あたり
            # 1分未満で、便の総所要時間には累積しない。時刻逆転（負値）のみ除外。
            if travel_time >= 0:
                edges.append({
                    "from_stop": from_stop,
                    "to_stop": to_stop,
                    "departure_sec": dep_time,
                    "arrival_sec": arr_time,
                    "travel_time": travel_time,
                    "trip_id": trip_id,
                    "type": "bus"
                })

    edges_df = pd.DataFrame(edges)
    print(f"\nバス移動エッジ数: {len(edges_df)}")

    # 徒歩乗り換えエッジを作成
    WALK_SPEED = 67  # メートル/分
    MAX_WALK_DIST = 300  # メートル

    stop_coords = stops[["stop_id", "stop_lat", "stop_lon"]].drop_duplicates("stop_id")
    stop_list = stop_coords.values.tolist()

    walk_edges = []
    for i in range(len(stop_list)):
        for j in range(i+1, len(stop_list)):
            dist = haversine(stop_list[i][1], stop_list[i][2], stop_list[j][1], stop_list[j][2])
            if dist <= MAX_WALK_DIST and stop_list[i][0] != stop_list[j][0]:
                walk_time = int((dist / WALK_SPEED) * 60)
                if walk_time < 1:
                    walk_time = 1
                walk_edges.append({
                    "from_stop": stop_list[i][0],
                    "to_stop": stop_list[j][0],
                    "walk_time": walk_time,
                    "distance": round(dist, 1),
                    "type": "walk"
                })
                walk_edges.append({
                    "from_stop": stop_list[j][0],
                    "to_stop": stop_list[i][0],
                    "walk_time": walk_time,
                    "distance": round(dist, 1),
                    "type": "walk"
                })

    walk_df = pd.DataFrame(walk_edges)
    print(f"徒歩乗り換えエッジ数: {len(walk_df)}")

    # 保存
    edges_df.to_csv(os.path.join(output_dir, "bus_edges.csv"), index=False)
    walk_df.to_csv(os.path.join(output_dir, "walk_edges.csv"), index=False)
    print(f"\nbus_edges.csv と walk_edges.csv を保存しました")

    # 概要
    unique_stops = set(edges_df["from_stop"].tolist() + edges_df["to_stop"].tolist())
    print(f"\nネットワーク概要:")
    print(f"  ノード（バス停）数: {len(unique_stops)}")
    print(f"  バスエッジ数: {len(edges_df)}")
    print(f"  徒歩エッジ数: {len(walk_df)}")

    return edges_df, walk_df


if __name__ == "__main__":
    build_network()