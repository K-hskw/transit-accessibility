import os
import pandas as pd
import heapq
from contextlib import contextmanager
from math import radians, sin, cos, sqrt, atan2

from service_calendar import DAY_TYPES, available_day_types, trip_ids_for_day_type

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))


class TransitEngine:
    def __init__(self, gtfs_dir="gtfs_data", day_type="平日"):
        self.stops = pd.read_csv(f"{gtfs_dir}/stops.txt")
        self.routes = pd.read_csv(f"{gtfs_dir}/routes.txt")
        self.trips = pd.read_csv(f"{gtfs_dir}/trips.txt")
        self.calendar = pd.read_csv(f"{gtfs_dir}/calendar.txt")
        # カスタムGTFSの場合は対応するエッジファイルを使用
        if gtfs_dir == "gtfs_data_custom" and os.path.exists("bus_edges_custom.csv"):
            self.all_bus_edges = pd.read_csv("bus_edges_custom.csv")
            self.walk_edges = pd.read_csv("walk_edges_custom.csv")
        else:
            self.all_bus_edges = pd.read_csv("bus_edges.csv")
            self.walk_edges = pd.read_csv("walk_edges.csv")

        self.trip_to_route = self.trips.set_index("trip_id")["route_id"].to_dict()
        self.all_bus_edges["route_id"] = self.all_bus_edges["trip_id"].map(self.trip_to_route)

        # エッジは全ダイヤ分を持ち、bus_edges は選択中のダイヤ種別の絞り込み結果
        self.available_day_types = available_day_types(self.trips, self.calendar)
        self.day_type = None

        self.route_names = self.routes.set_index("route_id")["route_long_name"].to_dict()
        self.stop_coords = self.stops.set_index("stop_id")[["stop_lat", "stop_lon", "stop_name"]]

        self.trip_to_route_name = {}
        for trip_id, route_id in self.trip_to_route.items():
            self.trip_to_route_name[trip_id] = self.route_names.get(route_id, "不明")

        # 全ネットワークのグラフは何度も使い回すのでキャッシュする。
        # 探索本体は一瞬で終わるが、グラフ構築が calc_isochrone 1回の
        # 所要時間のほぼ全てを占めていた（時間帯別18回で約30秒）。
        # bus_edges / walk_edges を差し替えた場合は clear_graph_cache() を呼ぶこと。
        self._bus_graph_cache = None
        self._walk_graph_cache = None
        self._rev_bus_graph_cache = None

        self.set_day_type(day_type)

    def clear_graph_cache(self):
        """bus_edges / walk_edges を差し替えたときにキャッシュを破棄する"""
        self._bus_graph_cache = None
        self._walk_graph_cache = None
        self._rev_bus_graph_cache = None

    def set_day_type(self, day_type):
        """ダイヤ種別（平日/土曜/日祝）を切り替える

        全便のエッジは all_bus_edges に保持し、bus_edges をその絞り込み結果に
        差し替える。ネットワークを作り直す必要はないが、グラフのキャッシュは
        中身が変わるので破棄する。
        """
        if day_type == self.day_type:
            return
        if day_type not in DAY_TYPES:
            raise ValueError(f"未知のダイヤ種別: {day_type}（{DAY_TYPES} のいずれか）")

        trip_ids = trip_ids_for_day_type(self.trips, self.calendar, day_type)
        self.day_type = day_type
        self.bus_edges = self.all_bus_edges[
            self.all_bus_edges["trip_id"].isin(trip_ids)
        ].reset_index(drop=True)
        self.clear_graph_cache()

    @contextmanager
    def scenario(self, bus_edges=None, walk_edges=None):
        """改変後のネットワークを一時的に適用する。

        施策の効果を空白人口（逆方向到達圏ベース）で測るために使う。
        simulate_* は起点1停留所からの前方到達圏しか返さないため、
        edges_after_*() が返す改変後エッジをこれで適用してから
        BlankAreaAnalyzer.diagnose() を呼ぶ。

            before = analyzer.diagnose(dest, t, limit)
            with engine.scenario(*engine.edges_after_route_removal(rid)):
                after = analyzer.diagnose(dest, t, limit)

        抜けると元のネットワークとグラフキャッシュに戻る。
        この中で set_day_type を呼んではいけない（復帰時に上書きされる）。
        """
        saved = (self.bus_edges, self.walk_edges, self._bus_graph_cache,
                 self._walk_graph_cache, self._rev_bus_graph_cache)
        if bus_edges is not None:
            self.bus_edges = bus_edges
        if walk_edges is not None:
            self.walk_edges = walk_edges
        self.clear_graph_cache()
        try:
            yield self
        finally:
            (self.bus_edges, self.walk_edges, self._bus_graph_cache,
             self._walk_graph_cache, self._rev_bus_graph_cache) = saved

    def _full_bus_graph(self):
        if self._bus_graph_cache is None:
            self._bus_graph_cache = self._build_bus_graph(self.bus_edges)
        return self._bus_graph_cache

    def _full_walk_graph(self):
        if self._walk_graph_cache is None:
            self._walk_graph_cache = self._build_walk_graph(self.walk_edges)
        return self._walk_graph_cache

    def _full_graphs(self):
        """全ネットワークのバスグラフ・徒歩グラフ（キャッシュ付き）"""
        return self._full_bus_graph(), self._full_walk_graph()

    def get_muroran_stops(self, lat_min=42.28, lat_max=42.42, lon_min=140.88, lon_max=141.05):
        mask = (
            (self.stops["stop_lat"] >= lat_min) & (self.stops["stop_lat"] <= lat_max) &
            (self.stops["stop_lon"] >= lon_min) & (self.stops["stop_lon"] <= lon_max)
        )
        return self.stops[mask]

    def get_muroran_routes(self, exclude_highway=True):
        """路線の一覧を返す（他地域対応版）"""
        route_ids = self.bus_edges["route_id"].unique()
        result = []
        for rid in route_ids:
            name = self.route_names.get(rid, "不明")
            if exclude_highway:
                if name and any(kw in str(name) for kw in ["高速", "都市間", "Express", "express"]):
                    continue
            result.append({"route_id": rid, "route_name": name})
        return result

    def get_stop_names(self):
        names = sorted(self.stops["stop_name"].unique())
        return names

    def get_stop_ids_by_name(self, stop_name):
        return self.stops[self.stops["stop_name"] == stop_name]["stop_id"].tolist()

    def get_routes_grouped_by_access(self, start_stop_name, exclude_highway=True):
        """出発バス停から直接乗車可能な路線と、乗り換えで利用する路線を分けて返す"""
        start_stop_ids = self.get_stop_ids_by_name(start_stop_name)
        start_set = set(start_stop_ids)

        walk_reachable = set(start_stop_ids)
        for _, row in self.walk_edges.iterrows():
            if row["from_stop"] in start_set:
                walk_reachable.add(row["to_stop"])
            if row["to_stop"] in start_set:
                walk_reachable.add(row["from_stop"])

        direct_edges = self.bus_edges[self.bus_edges["from_stop"].isin(walk_reachable)]
        direct_route_ids = set(direct_edges["route_id"].unique())

        all_routes = self.get_muroran_routes(exclude_highway=exclude_highway)

        direct_routes = []
        transfer_routes = []
        for route in all_routes:
            if route["route_id"] in direct_route_ids:
                direct_routes.append(route)
            else:
                transfer_routes.append(route)

        return direct_routes, transfer_routes

    def _build_bus_graph(self, edges_df):
        # itertuples は iterrows の約10倍速い（3万行で1.4秒 -> 0.15秒）
        graph = {}
        for row in edges_df.itertuples(index=False):
            graph.setdefault(row.from_stop, []).append((
                int(row.departure_sec),
                int(row.arrival_sec),
                row.to_stop,
                row.trip_id
            ))
        for stop_id in graph:
            graph[stop_id].sort(key=lambda x: x[0])
        return graph

    def _build_walk_graph(self, walk_df):
        graph = {}
        for row in walk_df.itertuples(index=False):
            graph.setdefault(row.from_stop, []).append((row.to_stop, int(row.walk_time)))
        return graph

    def _sec_to_time(self, sec):
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        return f"{h:02d}:{m:02d}"

    #時間依存最短経路探索アルゴリズム
    def _dijkstra(self, bus_graph, walk_graph, start_stop, start_time_sec, max_time_sec, track_path=False):
        best_arrival = {start_stop: start_time_sec}
        queue = [(start_time_sec, start_stop, "start")]
        deadline = start_time_sec + max_time_sec

        prev = {}
        if track_path:
            prev[start_stop] = None

        best_state = {(start_stop, "start"): start_time_sec}

        while queue:
            current_time, current_stop, last_mode = heapq.heappop(queue)

            state = (current_stop, last_mode)
            if current_time > best_state.get(state, float("inf")):
                continue
            if current_time > deadline:
                continue

            if current_stop in bus_graph:
                for dep_sec, arr_sec, to_stop, trip_id in bus_graph[current_stop]:
                    if dep_sec >= current_time and arr_sec <= deadline:
                        new_state = (to_stop, "bus")
                        if arr_sec < best_state.get(new_state, float("inf")):
                            best_state[new_state] = arr_sec
                            if arr_sec < best_arrival.get(to_stop, float("inf")):
                                best_arrival[to_stop] = arr_sec
                                if track_path:
                                    prev[to_stop] = (current_stop, trip_id, dep_sec, arr_sec)
                            heapq.heappush(queue, (arr_sec, to_stop, "bus"))

            if last_mode != "walk" and current_stop in walk_graph:
                for to_stop, walk_time in walk_graph[current_stop]:
                    arr_time = current_time + walk_time
                    if arr_time <= deadline:
                        new_state = (to_stop, "walk")
                        if arr_time < best_state.get(new_state, float("inf")):
                            best_state[new_state] = arr_time
                            if arr_time < best_arrival.get(to_stop, float("inf")):
                                best_arrival[to_stop] = arr_time
                                if track_path:
                                    prev[to_stop] = (current_stop, "walk", current_time, arr_time)
                            heapq.heappush(queue, (arr_time, to_stop, "walk"))

        if track_path:
            return best_arrival, prev
        return best_arrival

    def reconstruct_path(self, prev, target_stop_id):
        if target_stop_id not in prev:
            return None

        path = []
        current = target_stop_id
        while prev[current] is not None:
            from_stop, trip_or_walk, dep_sec, arr_sec = prev[current]

            if trip_or_walk == "walk":
                mode = "徒歩"
                route_name = ""
            else:
                mode = "バス"
                route_name = self.trip_to_route_name.get(trip_or_walk, "")

            from_name = self.stop_coords.loc[from_stop, "stop_name"] if from_stop in self.stop_coords.index else from_stop
            to_name = self.stop_coords.loc[current, "stop_name"] if current in self.stop_coords.index else current

            path.append({
                "from_stop": from_name,
                "to_stop": to_name,
                "mode": mode,
                "route_name": route_name,
                "departure": self._sec_to_time(dep_sec),
                "arrival": self._sec_to_time(arr_sec),
                "duration_min": round((arr_sec - dep_sec) / 60, 1)
            })
            current = from_stop

        path.reverse()
        return path

    def calc_isochrone(self, start_stop_id, start_time_sec, max_time_sec, track_path=False):
        bus_graph, walk_graph = self._full_graphs()
        return self._dijkstra(bus_graph, walk_graph, start_stop_id, start_time_sec, max_time_sec, track_path)

    def edges_after_route_removal(self, remove_route_id):
        """路線廃止後の (bus_edges, walk_edges) を返す。徒歩エッジは変わらない。"""
        if isinstance(remove_route_id, list):
            edges_after = self.bus_edges[~self.bus_edges["route_id"].isin(remove_route_id)]
        else:
            edges_after = self.bus_edges[self.bus_edges["route_id"] != remove_route_id]
        return edges_after, self.walk_edges

    def simulate_route_removal(self, start_stop_id, start_time_sec, max_time_sec, remove_route_id, track_path=False):
        edges_after, _ = self.edges_after_route_removal(remove_route_id)
        bus_graph = self._build_bus_graph(edges_after)
        walk_graph = self._full_walk_graph()
        return self._dijkstra(bus_graph, walk_graph, start_stop_id, start_time_sec, max_time_sec, track_path)

    def edges_after_stop_removal(self, remove_stop_ids, walk_distance=300):
        """バス停削除後の (bus_edges, walk_edges) を返す。

        削除バス停は通過扱い（前後を直通エッジで接続）とし、路線自体は維持する。
        徒歩エッジは削除バス停ぶんを除き、その徒歩近隣どうしを繋ぎ直す。
        """
        remove_set = set(remove_stop_ids)

        # === バスエッジ: 削除バス停を通過扱いにする ===
        new_edges = []
        for trip_id, group in self.bus_edges[self.bus_edges["trip_id"].notna()].groupby("trip_id"):
            group_sorted = group.sort_values("departure_sec")
            rows = group_sorted.to_dict("records")

            stops_in_trip = []
            for r in rows:
                if len(stops_in_trip) == 0 or stops_in_trip[-1]["stop_id"] != r["from_stop"]:
                    stops_in_trip.append({
                        "stop_id": r["from_stop"],
                        "departure_sec": int(r["departure_sec"]),
                        "arrival_sec": int(r["departure_sec"])
                    })
                stops_in_trip.append({
                    "stop_id": r["to_stop"],
                    "departure_sec": int(r["arrival_sec"]),
                    "arrival_sec": int(r["arrival_sec"])
                })

            filtered = [s for s in stops_in_trip if s["stop_id"] not in remove_set]

            for i in range(len(filtered) - 1):
                dep_sec = filtered[i]["departure_sec"]
                arr_sec = filtered[i + 1]["arrival_sec"]
                # build_network と同じく、分単位GTFSで所要0分になる区間も残す。
                # arr_sec > dep_sec（厳密）だと通過再接続のたびに0分区間が消え、
                # 削除ゼロでも到達圏が壊れていた（212→91）。時刻逆転のみ除外する。
                if arr_sec >= dep_sec and filtered[i]["stop_id"] != filtered[i + 1]["stop_id"]:
                    new_edges.append({
                        "from_stop": filtered[i]["stop_id"],
                        "to_stop": filtered[i + 1]["stop_id"],
                        "departure_sec": dep_sec,
                        "arrival_sec": arr_sec,
                        "travel_time": arr_sec - dep_sec,
                        "trip_id": trip_id,
                        "type": "bus",
                        "route_id": group_sorted.iloc[0]["route_id"]
                    })

        edges_after = pd.DataFrame(new_edges)

        # === 徒歩エッジ: 削除バス停と「徒歩で」つながっていたバス停同士のみ接続 ===
        walk_speed = 67  # メートル/分

        # 削除バス停と徒歩エッジでつながっていたバス停のみを収集
        # （バスエッジでつながっていたバス停は含めない）
        walk_neighbors = set()
        for _, row in self.walk_edges.iterrows():
            if row["from_stop"] in remove_set and row["to_stop"] not in remove_set:
                walk_neighbors.add(row["to_stop"])
            if row["to_stop"] in remove_set and row["from_stop"] not in remove_set:
                walk_neighbors.add(row["from_stop"])

        # 徒歩近隣バス停同士のみを直接つなぐ
        new_walk_edges = []
        neighbor_list = list(walk_neighbors)
        neighbor_coords = {}
        for sid in neighbor_list:
            if sid in self.stop_coords.index:
                neighbor_coords[sid] = (
                    self.stop_coords.loc[sid, "stop_lat"],
                    self.stop_coords.loc[sid, "stop_lon"]
                )

        for i in range(len(neighbor_list)):
            for j in range(i + 1, len(neighbor_list)):
                sid_a = neighbor_list[i]
                sid_b = neighbor_list[j]
                if sid_a not in neighbor_coords or sid_b not in neighbor_coords:
                    continue
                dist = haversine(
                    neighbor_coords[sid_a][0], neighbor_coords[sid_a][1],
                    neighbor_coords[sid_b][0], neighbor_coords[sid_b][1]
                )
                # UIで選ぶ徒歩圏（国交省ハンドブック基準の300m/500m）と同じ距離で
                # 再接続する。以前は walk_distance*2（600m/1000m）で繋いでおり、
                # 基準より緩い徒歩エッジを生んでいた。実測では到達数への影響は無いが、
                # 説明可能性のため基準に揃える。
                if dist <= walk_distance:
                    walk_time = max(1, int((dist / walk_speed) * 60))
                    new_walk_edges.append({
                        "from_stop": sid_a, "to_stop": sid_b,
                        "walk_time": walk_time, "distance": round(dist, 1), "type": "walk"
                    })
                    new_walk_edges.append({
                        "from_stop": sid_b, "to_stop": sid_a,
                        "walk_time": walk_time, "distance": round(dist, 1), "type": "walk"
                    })

        # 既存の徒歩エッジから削除バス停を除外して、新規エッジを追加
        walk_after = self.walk_edges[
            (~self.walk_edges["from_stop"].isin(remove_set)) &
            (~self.walk_edges["to_stop"].isin(remove_set))
        ]
        if new_walk_edges:
            walk_after = pd.concat([walk_after, pd.DataFrame(new_walk_edges)], ignore_index=True)

        return edges_after, walk_after

    def simulate_stop_removal(self, start_stop_id, start_time_sec, max_time_sec, remove_stop_ids,
                              walk_distance=300, track_path=False):
        edges_after, walk_after = self.edges_after_stop_removal(remove_stop_ids, walk_distance)
        bus_graph = self._build_bus_graph(edges_after)
        walk_graph = self._build_walk_graph(walk_after)
        return self._dijkstra(bus_graph, walk_graph, start_stop_id, start_time_sec, max_time_sec, track_path)

    def edges_after_frequency_reduction(self, reduce_mode, target_route_id=None, reduce_ratio=0.5):
        """減便後の (bus_edges, walk_edges) を返す。徒歩エッジは変わらない。"""
        if reduce_mode == "half":
            target_trips = self.bus_edges[self.bus_edges["route_id"] == target_route_id]["trip_id"].unique()
            keep_trips = target_trips[::2]
            remove_trips = set(target_trips) - set(keep_trips)
            edges_after = self.bus_edges[~self.bus_edges["trip_id"].isin(remove_trips)]

        elif reduce_mode == "interval":
            keep_every = max(2, int(reduce_ratio))
            target_trips = self.bus_edges[self.bus_edges["route_id"] == target_route_id]["trip_id"].unique()
            trip_dep_times = {}
            for tid in target_trips:
                trip_edges = self.bus_edges[self.bus_edges["trip_id"] == tid]
                if len(trip_edges) > 0:
                    trip_dep_times[tid] = trip_edges["departure_sec"].min()
            sorted_trips = sorted(trip_dep_times.keys(), key=lambda t: trip_dep_times[t])
            keep_trips = set(sorted_trips[::keep_every])
            remove_trips = set(sorted_trips) - keep_trips
            edges_after = self.bus_edges[~self.bus_edges["trip_id"].isin(remove_trips)]

        elif reduce_mode == "all":
            remove_trips = set()
            for route_id in self.bus_edges["route_id"].unique():
                route_trips = self.bus_edges[self.bus_edges["route_id"] == route_id]["trip_id"].unique()
                if len(route_trips) <= 1:
                    continue
                trip_dep_times = {}
                for tid in route_trips:
                    trip_edges = self.bus_edges[self.bus_edges["trip_id"] == tid]
                    if len(trip_edges) > 0:
                        trip_dep_times[tid] = trip_edges["departure_sec"].min()
                sorted_trips = sorted(trip_dep_times.keys(), key=lambda t: trip_dep_times[t])
                n_remove = max(1, int(len(sorted_trips) * reduce_ratio))
                step = len(sorted_trips) / n_remove
                remove_indices = set()
                for i in range(n_remove):
                    idx = int(i * step)
                    if idx < len(sorted_trips):
                        remove_indices.add(idx)
                for idx in remove_indices:
                    remove_trips.add(sorted_trips[idx])
            edges_after = self.bus_edges[~self.bus_edges["trip_id"].isin(remove_trips)]

        else:
            edges_after = self.bus_edges

        return edges_after, self.walk_edges

    def simulate_frequency_reduction(self, start_stop_id, start_time_sec, max_time_sec,
                                     reduce_mode, target_route_id=None, reduce_ratio=0.5,
                                     track_path=False):
        edges_after, _ = self.edges_after_frequency_reduction(reduce_mode, target_route_id, reduce_ratio)
        bus_graph = self._build_bus_graph(edges_after)
        walk_graph = self._full_walk_graph()
        return self._dijkstra(bus_graph, walk_graph, start_stop_id, start_time_sec, max_time_sec, track_path)

    def compare_results(self, result_before, result_after, start_time_sec, threshold_min, remove_stop_ids=None):
        if remove_stop_ids is None:
            remove_stop_ids = []
        lost = set(result_before.keys()) - set(result_after.keys()) - set(remove_stop_ids)
        degraded = {}
        for stop_id in result_before:
            if stop_id in result_after and stop_id not in lost and stop_id not in remove_stop_ids:
                before_min = (result_before[stop_id] - start_time_sec) / 60
                after_min = (result_after[stop_id] - start_time_sec) / 60
                diff = after_min - before_min
                if diff >= threshold_min:
                    degraded[stop_id] = diff
        return lost, degraded


    def edges_after_route_replacement(self, remove_route_id, new_route_stops,
                                      interval_min=30, speed_kmh=25):
        """路線廃止＋代替路線追加後の (bus_edges, walk_edges) を返す。

        remove_route_idを廃止し、new_route_stops（バス停IDのリスト）を結ぶ新路線を追加。
        interval_min分間隔で運行、平均速度speed_kmhで所要時間を計算。
        徒歩エッジは変わらない。
        """
        from math import radians, sin, cos, sqrt, atan2
        import pandas as pd

        # 既存路線を廃止
        if isinstance(remove_route_id, list):
            edges_after = self.bus_edges[~self.bus_edges["route_id"].isin(remove_route_id)].copy()
        elif remove_route_id is None or remove_route_id == "":
            edges_after = self.bus_edges.copy()
        else:
            edges_after = self.bus_edges[self.bus_edges["route_id"] != remove_route_id].copy()

        def hav(lat1, lon1, lat2, lon2):
            R = 6371000
            dlat = radians(lat2 - lat1)
            dlon = radians(lon2 - lon1)
            a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
            return R * 2 * atan2(sqrt(a), sqrt(1-a))

        # 新路線のエッジを生成
        new_edges = []
        if len(new_route_stops) >= 2:
            # 5時〜22時の間、interval_min間隔で運行
            interval_sec = interval_min * 60
            for trip_num, dep_base_sec in enumerate(range(5*3600, 22*3600, interval_sec)):
                trip_id = f"NEW_TRIP_{trip_num}"
                cumulative_time = 0
                for i in range(len(new_route_stops) - 1):
                    s1 = new_route_stops[i]
                    s2 = new_route_stops[i+1]
                    if s1 not in self.stop_coords.index or s2 not in self.stop_coords.index:
                        continue
                    lat1 = self.stop_coords.loc[s1, "stop_lat"]
                    lon1 = self.stop_coords.loc[s1, "stop_lon"]
                    lat2 = self.stop_coords.loc[s2, "stop_lat"]
                    lon2 = self.stop_coords.loc[s2, "stop_lon"]
                    dist_m = hav(lat1, lon1, lat2, lon2)
                    travel_sec = int(dist_m / (speed_kmh * 1000 / 3600))

                    dep_sec = dep_base_sec + cumulative_time
                    arr_sec = dep_sec + travel_sec
                    cumulative_time += travel_sec + 30  # 停車30秒

                    new_edges.append({
                        "from_stop": s1,
                        "to_stop": s2,
                        "departure_sec": dep_sec,
                        "arrival_sec": arr_sec,
                        "travel_time": travel_sec,
                        "trip_id": trip_id,
                        "route_id": "NEW_ROUTE",
                        "type": "bus"
                    })
                    # 反対方向も追加
                    new_edges.append({
                        "from_stop": s2,
                        "to_stop": s1,
                        "departure_sec": dep_sec,
                        "arrival_sec": arr_sec,
                        "travel_time": travel_sec,
                        "trip_id": trip_id + "_R",
                        "route_id": "NEW_ROUTE",
                        "type": "bus"
                    })

        if new_edges:
            new_edges_df = pd.DataFrame(new_edges)
            edges_after = pd.concat([edges_after, new_edges_df], ignore_index=True)

        return edges_after, self.walk_edges

    def simulate_route_replacement(self, start_stop_id, start_time_sec, max_time_sec,
                                   remove_route_id, new_route_stops, interval_min=30,
                                   speed_kmh=25, track_path=False):
        edges_after, _ = self.edges_after_route_replacement(
            remove_route_id, new_route_stops, interval_min, speed_kmh)
        bus_graph = self._build_bus_graph(edges_after)
        walk_graph = self._full_walk_graph()
        return self._dijkstra(bus_graph, walk_graph, start_stop_id, start_time_sec, max_time_sec, track_path)

    def calc_reverse_isochrone(self, dest_stop_id, arrival_time_sec, max_time_sec, track_path=False):
        """逆方向到達圏計算（集客圏分析）
        dest_stop_idにarrival_time_sec までに到達できる出発地点を計算する。
        バスエッジを逆向きに辿り、arrival_time_secから過去方向に探索する。
        """
        import heapq

        # 逆向きバスグラフの構築（キャッシュ付き）
        # 通常: from_stop -> (dep_sec, arr_sec, to_stop, trip_id)
        # 逆向き: to_stop -> (arr_sec, dep_sec, from_stop, trip_id)
        if self._rev_bus_graph_cache is None:
            rev_bus_graph = {}
            for row in self.bus_edges.itertuples(index=False):
                rev_bus_graph.setdefault(row.to_stop, []).append((
                    int(row.arrival_sec),
                    int(row.departure_sec),
                    row.from_stop,
                    row.trip_id
                ))
            self._rev_bus_graph_cache = rev_bus_graph
        rev_bus_graph = self._rev_bus_graph_cache

        # 徒歩グラフはそのまま使用（無向グラフなので逆向きも同じ）
        walk_graph = self._full_walk_graph()

        # 逆向きダイクストラ
        # best_departure[stop] = そのバス停から出発できる最遅の出発時刻
        # 順方向 _dijkstra と同じく (停留所, 直前の移動手段) を状態に持ち、
        # 徒歩の連続を禁止する。これが無いと300m徒歩エッジを無制限に連鎖でき、
        # 集客圏が順方向到達圏より過大になる（室蘭で18%過大だった）。
        earliest_time = arrival_time_sec - max_time_sec
        best_departure = {dest_stop_id: arrival_time_sec}
        best_state = {(dest_stop_id, "start"): arrival_time_sec}
        # キューは (-departure_time, stop, mode) で最遅出発時刻を優先
        queue = [(-arrival_time_sec, dest_stop_id, "start")]

        prev = {}
        if track_path:
            prev[dest_stop_id] = None

        while queue:
            neg_time, current_stop, last_mode = heapq.heappop(queue)
            current_time = -neg_time

            if current_time < best_state.get((current_stop, last_mode), float("-inf")):
                continue

            # 逆向きバスエッジを辿る
            if current_stop in rev_bus_graph:
                for arr_sec, dep_sec, from_stop, trip_id in rev_bus_graph[current_stop]:
                    # このバスはcurrent_timeまでに到着し、earliest_time以降に出発する
                    if arr_sec <= current_time and dep_sec >= earliest_time:
                        new_state = (from_stop, "bus")
                        if dep_sec > best_state.get(new_state, float("-inf")):
                            best_state[new_state] = dep_sec
                            if dep_sec > best_departure.get(from_stop, float("-inf")):
                                best_departure[from_stop] = dep_sec
                                if track_path:
                                    prev[from_stop] = (current_stop, trip_id, dep_sec, arr_sec)
                            heapq.heappush(queue, (-dep_sec, from_stop, "bus"))

            # 徒歩エッジを逆向きに辿る（直前も徒歩なら辿らない）
            if last_mode != "walk" and current_stop in walk_graph:
                for to_stop, walk_time in walk_graph[current_stop]:
                    dep_time = current_time - walk_time
                    if dep_time >= earliest_time:
                        new_state = (to_stop, "walk")
                        if dep_time > best_state.get(new_state, float("-inf")):
                            best_state[new_state] = dep_time
                            if dep_time > best_departure.get(to_stop, float("-inf")):
                                best_departure[to_stop] = dep_time
                                if track_path:
                                    prev[to_stop] = (current_stop, "walk", dep_time, current_time)
                            heapq.heappush(queue, (-dep_time, to_stop, "walk"))

        if track_path:
            return best_departure, prev
        return best_departure
