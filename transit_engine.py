import pandas as pd
import heapq
from math import radians, sin, cos, sqrt, atan2


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))


class TransitEngine:
    def __init__(self, gtfs_dir="gtfs_data"):
        self.stops = pd.read_csv(f"{gtfs_dir}/stops.txt")
        self.routes = pd.read_csv(f"{gtfs_dir}/routes.txt")
        self.trips = pd.read_csv(f"{gtfs_dir}/trips.txt")
        self.calendar = pd.read_csv(f"{gtfs_dir}/calendar.txt")
        self.bus_edges = pd.read_csv("bus_edges.csv")
        self.walk_edges = pd.read_csv("walk_edges.csv")

        self.trip_to_route = self.trips.set_index("trip_id")["route_id"].to_dict()
        self.bus_edges["route_id"] = self.bus_edges["trip_id"].map(self.trip_to_route)

        self.route_names = self.routes.set_index("route_id")["route_long_name"].to_dict()
        self.stop_coords = self.stops.set_index("stop_id")[["stop_lat", "stop_lon", "stop_name"]]

        self.trip_to_route_name = {}
        for trip_id, route_id in self.trip_to_route.items():
            self.trip_to_route_name[trip_id] = self.route_names.get(route_id, "不明")

    def get_muroran_stops(self, lat_min=42.28, lat_max=42.42, lon_min=140.88, lon_max=141.05):
        mask = (
            (self.stops["stop_lat"] >= lat_min) & (self.stops["stop_lat"] <= lat_max) &
            (self.stops["stop_lon"] >= lon_min) & (self.stops["stop_lon"] <= lon_max)
        )
        return self.stops[mask]

    def get_muroran_routes(self, exclude_highway=True):
        """室蘭市内を通る路線の一覧を返す"""
        muroran_stop_ids = self.get_muroran_stops()["stop_id"].tolist()
        muroran_edges = self.bus_edges[
            (self.bus_edges["from_stop"].isin(muroran_stop_ids)) |
            (self.bus_edges["to_stop"].isin(muroran_stop_ids))
        ]
        route_ids = muroran_edges["route_id"].unique()
        result = []
        for rid in route_ids:
            name = self.route_names.get(rid, "不明")
            if exclude_highway:
                if any(kw in name for kw in ["高速", "都市間"]):
                    continue
            result.append({"route_id": rid, "route_name": name})
        return result

    def get_stop_names(self):
        muroran = self.get_muroran_stops()
        names = sorted(muroran["stop_name"].unique())
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
        graph = {}
        for _, row in edges_df.iterrows():
            from_stop = row["from_stop"]
            if from_stop not in graph:
                graph[from_stop] = []
            graph[from_stop].append((
                int(row["departure_sec"]),
                int(row["arrival_sec"]),
                row["to_stop"],
                row["trip_id"]
            ))
        for stop_id in graph:
            graph[stop_id].sort(key=lambda x: x[0])
        return graph

    def _build_walk_graph(self, walk_df):
        graph = {}
        for _, row in walk_df.iterrows():
            from_stop = row["from_stop"]
            if from_stop not in graph:
                graph[from_stop] = []
            graph[from_stop].append((row["to_stop"], int(row["walk_time"])))
        return graph

    def _sec_to_time(self, sec):
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        return f"{h:02d}:{m:02d}"

    def _dijkstra(self, bus_graph, walk_graph, start_stop, start_time_sec, max_time_sec, track_path=False):
        best_arrival = {start_stop: start_time_sec}
        queue = [(start_time_sec, start_stop)]
        deadline = start_time_sec + max_time_sec

        prev = {}
        if track_path:
            prev[start_stop] = None

        while queue:
            current_time, current_stop = heapq.heappop(queue)
            if current_time > best_arrival.get(current_stop, float("inf")):
                continue
            if current_time > deadline:
                continue

            if current_stop in bus_graph:
                for dep_sec, arr_sec, to_stop, trip_id in bus_graph[current_stop]:
                    if dep_sec >= current_time and arr_sec <= deadline:
                        if arr_sec < best_arrival.get(to_stop, float("inf")):
                            best_arrival[to_stop] = arr_sec
                            if track_path:
                                prev[to_stop] = (current_stop, trip_id, dep_sec, arr_sec)
                            heapq.heappush(queue, (arr_sec, to_stop))

            if current_stop in walk_graph:
                for to_stop, walk_time in walk_graph[current_stop]:
                    arr_time = current_time + walk_time
                    if arr_time <= deadline:
                        if arr_time < best_arrival.get(to_stop, float("inf")):
                            best_arrival[to_stop] = arr_time
                            if track_path:
                                prev[to_stop] = (current_stop, "walk", current_time, arr_time)
                            heapq.heappush(queue, (arr_time, to_stop))

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
        bus_graph = self._build_bus_graph(self.bus_edges)
        walk_graph = self._build_walk_graph(self.walk_edges)
        return self._dijkstra(bus_graph, walk_graph, start_stop_id, start_time_sec, max_time_sec, track_path)

    def simulate_route_removal(self, start_stop_id, start_time_sec, max_time_sec, remove_route_id, track_path=False):
        edges_after = self.bus_edges[self.bus_edges["route_id"] != remove_route_id]
        bus_graph = self._build_bus_graph(edges_after)
        walk_graph = self._build_walk_graph(self.walk_edges)
        return self._dijkstra(bus_graph, walk_graph, start_stop_id, start_time_sec, max_time_sec, track_path)

    def simulate_stop_removal(self, start_stop_id, start_time_sec, max_time_sec, remove_stop_ids, walk_distance=300, track_path=False):
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
                if arr_sec > dep_sec and filtered[i]["stop_id"] != filtered[i + 1]["stop_id"]:
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
                if dist <= walk_distance * 2:
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

        bus_graph = self._build_bus_graph(edges_after)
        walk_graph = self._build_walk_graph(walk_after)
        return self._dijkstra(bus_graph, walk_graph, start_stop_id, start_time_sec, max_time_sec, track_path)

    def simulate_frequency_reduction(self, start_stop_id, start_time_sec, max_time_sec,
                                     reduce_mode, target_route_id=None, reduce_ratio=0.5,
                                     track_path=False):
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

        bus_graph = self._build_bus_graph(edges_after)
        walk_graph = self._build_walk_graph(self.walk_edges)
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
