import pandas as pd
from math import radians, sin, cos, sqrt, atan2
import os

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
    trips = pd.read_csv(os.path.join(gtfs_dir, "trips.txt"))
    calendar = pd.read_csv(os.path.join(gtfs_dir, "calendar.txt"))

    # 平日ダイヤに絞る
    weekday_services = calendar[calendar["monday"] == 1]["service_id"].tolist()
    weekday_trips = trips[trips["service_id"].isin(weekday_services)]["trip_id"].tolist()
    weekday_stop_times = stop_times[stop_times["trip_id"].isin(weekday_trips)].copy()

    print(f"平日の便数: {len(weekday_trips)}")
    print(f"平日の停車レコード数: {len(weekday_stop_times)}")

    # 時刻を秒に変換
    weekday_stop_times["arrival_sec"] = weekday_stop_times["arrival_time"].apply(time_to_seconds)
    weekday_stop_times["departure_sec"] = weekday_stop_times["departure_time"].apply(time_to_seconds)

    # バス移動エッジを作成
    weekday_stop_times = weekday_stop_times.sort_values(["trip_id", "stop_sequence"])

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

            if travel_time > 0:
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