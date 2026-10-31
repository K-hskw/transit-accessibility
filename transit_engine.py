import copy
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
    def __init__(self, gtfs_dir="gtfs_data", day_type="平日", edges_dir=None):
        """edges_dir を渡すとそこから bus_edges.csv / walk_edges.csv を読む。

        省略時はカレントディレクトリの既定エッジ（室蘭）を読む。アップロードされた
        GTFSは訪問者ごとの一時ディレクトリにエッジを作り、ここで指定する。
        """
        self.gtfs_dir = gtfs_dir
        self.stops = pd.read_csv(f"{gtfs_dir}/stops.txt")
        self.routes = pd.read_csv(f"{gtfs_dir}/routes.txt")
        # service_id は文字列として読む。"01" のようなゼロ詰めのIDを pandas が
        # 数値と推測して 1 に変換してしまい、trips.txt 側（他の値が混在して
        # 文字列のまま）と突合できなくなる。旭川電気軌道のフィードでは
        # これにより全ダイヤが0便と判定されていた。
        self.trips = pd.read_csv(f"{gtfs_dir}/trips.txt", dtype={"service_id": str})
        self.calendar = pd.read_csv(f"{gtfs_dir}/calendar.txt", dtype={"service_id": str})
        edges_dir = edges_dir or "."
        self.all_bus_edges = pd.read_csv(os.path.join(edges_dir, "bus_edges.csv"))
        self.walk_edges = pd.read_csv(os.path.join(edges_dir, "walk_edges.csv"))

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
        self._route_length_cache = {}

        self.set_day_type(day_type)

    def feed_info(self):
        """feed_info.txt の事業者名と有効期間（YYYY-MM-DD）。無ければ None

        出典の表示と、有効期間切れの時刻表で分析していないかの確認に使う。
        """
        path = os.path.join(self.gtfs_dir, "feed_info.txt")
        if not os.path.exists(path):
            return None
        try:
            row = pd.read_csv(path, dtype=str).iloc[0]
        except (pd.errors.EmptyDataError, IndexError):
            return None

        def ymd(v):
            v = str(v) if isinstance(v, str) else ""
            return f"{v[:4]}-{v[4:6]}-{v[6:8]}" if len(v) == 8 and v.isdigit() else None

        publisher = row.get("feed_publisher_name")
        return {"publisher": publisher if isinstance(publisher, str) else None,
                "start": ymd(row.get("feed_start_date")), "end": ymd(row.get("feed_end_date"))}

    def clear_graph_cache(self):
        """bus_edges / walk_edges を差し替えたときにキャッシュを破棄する"""
        self._bus_graph_cache = None
        self._walk_graph_cache = None
        self._rev_bus_graph_cache = None

    def session_copy(self):
        """読み込んだデータは共有し、状態だけを分けた複製を返す

        Streamlit の cache_resource は全訪問者で同じオブジェクトを共有する。
        そのまま set_day_type() や scenario() を呼ぶと、ある訪問者の処方計算中に
        別の訪問者の診断がその差し替えたエッジで計算される（室蘭の平日9時で
        空白人口 19,867人が 77,690人と表示されるのを再現した）。

        状態の変更はすべて属性の再代入で行っているので、浅いコピーで分離できる。
        その場で書き換えるのは路線長のキャッシュ（dict）だけなので、これだけ複製する。
        GTFS・全便エッジ・構築済みグラフは読み取り専用として共有し、メモリを抑える。
        """
        c = copy.copy(self)
        c._route_length_cache = dict(self._route_length_cache)
        return c

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
        self._route_length_cache = {}   # 種別により経路パターンが変わりうる
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

    @staticmethod
    def _thinning_order(n):
        """時刻順に並んだ n 本の便を、間引く順に並べた添字を返す

        先頭から k 本を取ると、どの k でも時刻に偏りなく散らばり、k を増やしても
        前に選んだ便は選ばれたまま残る（入れ子）。そのため減便率を上げたときに
        一度削った便が復活することがなく、「減便率を上げれば空白は増えない方向に
        しか動かない」が組み立てとして成り立つ。並べ方は添字+1を2進数で左右反転した
        値（van der Corput 列）の小さい順で、k = n//2 ではちょうど2本目・4本目…が
        選ばれる（1本おきに残す）。
        """
        def radical_inverse(x):
            v, base = 0.0, 0.5
            while x:
                x, r = divmod(x, 2)
                v += r * base
                base /= 2
            return v
        return sorted(range(n), key=lambda i: (radical_inverse(i + 1), i))

    def edges_after_frequency_reduction(self, reduce_mode, target_route_id=None, reduce_ratio=0.5,
                                        keep_every=None):
        """減便後の (bus_edges, walk_edges) を返す。徒歩エッジは変わらない。

        便は停留所パターン（方向・経路）ごとに時刻順に並べて間引く。以前は路線の便を
        trip_id 順や方向を混ぜた時刻順で間引いており、2パターン以上ある53路線中18路線で
        片方向の便が全部残るか全部消えていた（「半分」なのに片方向だけ全廃になる）。
            half     : 各パターンで1本おきに残す（1本目・3本目…）
            interval : 各パターンで keep_every 本に1本残す（1本目・(N+1)本目…）
            all      : 全路線の各パターンで、便数×reduce_ratio の前後の本数を削る。
                       削る便は _thinning_order の順なので、率を上げると削る便が増えるだけ
        """
        remove_trips = set()
        if reduce_mode in ("half", "interval"):
            if reduce_mode == "half":
                n_keep = 2
            else:
                # keep_every を渡さない古い呼び出しは reduce_ratio に N を入れていた
                n_keep = max(2, int(keep_every if keep_every is not None else reduce_ratio))
            for trips in self._trip_patterns(target_route_id).values():
                remove_trips.update(tid for i, (_dep, tid, _g) in enumerate(trips) if i % n_keep)

        elif reduce_mode == "all":
            # パターンごとに削る本数は k = floor(便数×率 + φ)。φ はパターンごとに固定の
            # 0〜1の値で、黄金比で散らす。四捨五入（φ=0.5）だと便数1〜3本のパターンが多いため
            # 切り上げに偏り、50%指定で実際は61%削れていた。φ を散らすと切り上げと切り捨てが
            # パターン間で相殺されて全体の削減率が指定に近づき、率を上げても k は減らない。
            patterns = []
            for route_id in sorted(self.bus_edges["route_id"].dropna().unique(), key=str):
                pats = self._trip_patterns(route_id)
                patterns.extend(pats[key] for key in sorted(pats, key=str))
            for j, trips in enumerate(patterns):
                phi = ((j + 1) * 0.6180339887498949) % 1.0
                k = int(len(trips) * reduce_ratio + phi)
                order = self._thinning_order(len(trips))
                remove_trips.update(trips[i][1] for i in order[:k])

        if not remove_trips:
            return self.bus_edges, self.walk_edges
        return self.bus_edges[~self.bus_edges["trip_id"].isin(remove_trips)], self.walk_edges

    def simulate_frequency_reduction(self, start_stop_id, start_time_sec, max_time_sec,
                                     reduce_mode, target_route_id=None, reduce_ratio=0.5,
                                     track_path=False, keep_every=None):
        edges_after, _ = self.edges_after_frequency_reduction(
            reduce_mode, target_route_id, reduce_ratio, keep_every)
        bus_graph = self._build_bus_graph(edges_after)
        walk_graph = self._full_walk_graph()
        return self._dijkstra(bus_graph, walk_graph, start_stop_id, start_time_sec, max_time_sec, track_path)

    # ===== 改善施策（処方） =====
    # 核心が「時間空白」なので、処方も時間軸のもの（増便・ダイヤシフト）を中心に置く。
    # 効果は engine.scenario() で適用して空白人口の前後差で測る。

    def _trip_patterns(self, target_route_id):
        """路線の便を停留所パターンごとにまとめる。

        1つの路線に方向違い・経路違いが混在する（例: 鉄北北口線は4パターン）ため、
        増便やダイヤシフトはパターン単位で扱わないと、行きの便を複製して
        帰りの便として足すような不整合が起きる。
        返り値: {(from停留所列, to停留所列): [(始発時刻, trip_id, エッジ群), ...]}（時刻順）
        """
        sub = self.bus_edges[self.bus_edges["route_id"] == target_route_id]
        patterns = {}
        for trip_id, g in sub.groupby("trip_id"):
            g = g.sort_values("departure_sec")
            key = (tuple(g["from_stop"]), tuple(g["to_stop"]))
            patterns.setdefault(key, []).append((int(g["departure_sec"].min()), trip_id, g))
        for key in patterns:
            patterns[key].sort(key=lambda x: x[0])
        return patterns

    def _shifted_trip(self, edges_of_trip, new_dep_sec, new_trip_id):
        """便のエッジ群を、始発時刻が new_dep_sec になるようずらして複製する"""
        delta = int(new_dep_sec) - int(edges_of_trip["departure_sec"].min())
        out = edges_of_trip.copy()
        out["departure_sec"] = out["departure_sec"] + delta
        out["arrival_sec"] = out["arrival_sec"] + delta
        out["trip_id"] = new_trip_id
        return out

    @staticmethod
    def _via_offset(edges_of_trip, via_stops):
        """便の始発から via_stops のどれかを最初に通るまでの秒数（通らなければ None）"""
        if not via_stops:
            return None
        start = int(edges_of_trip["departure_sec"].min())
        times = []
        dep = edges_of_trip.loc[edges_of_trip["from_stop"].isin(via_stops), "departure_sec"]
        arr = edges_of_trip.loc[edges_of_trip["to_stop"].isin(via_stops), "arrival_sec"]
        if len(dep):
            times.append(int(dep.min()))
        if len(arr):
            times.append(int(arr.min()))
        return min(times) - start if times else None

    def _anchored(self, trips, via_stops):
        """パターンの便を (基準時刻, 始発時刻, trip_id, エッジ群) にする

        基準時刻は拠点（via_stops）を通る時刻。拠点を通らないパターンや
        via_stops を渡さない場合は始発時刻。
        """
        out = []
        for dep, tid, g in trips:
            off = self._via_offset(g, via_stops)
            out.append((dep + (off or 0), dep, tid, g))
        return out

    def edges_after_frequency_increase(self, target_route_id, start_hour, end_hour, n_trips=1,
                                       via_stops=None):
        """指定時間帯を増便した (bus_edges, walk_edges) を返す。

        停留所パターンごとに n_trips 便を時間帯内へ等間隔に挿入する。方向別に
        足すので、パターンが2方向あれば実際の追加便数は 2×n_trips になる。
        所要時間と停車パターンは、その時間帯に最も近い既存便をそのまま使う。

        via_stops（拠点の停留所ID）を渡すと、足す便が拠点を通る時刻を時間帯内に
        等間隔に置く（拠点を通らないパターンは始発時刻で置く）。始発時刻だけで置くと、
        既定の「5〜9時に1便」が7:00発になり、7時台に拠点へ着く便にならなかった。

        既存便の隙間を埋める方式にはしていない。空白が深刻な時間帯ほど既存便が
        1本以下で隙間が定義できず、最も増便したい路線に限って増便できなくなるため。
        """
        lo, hi = start_hour * 3600, end_hour * 3600
        via = set(via_stops or ())
        added, n = [], 0
        for trips in self._trip_patterns(target_route_id).values():
            if not trips:
                continue
            anchored = self._anchored(trips, via)
            # 時間帯内の便を雛形にする。無ければ時間帯に最も近い便で代用する
            in_window = [a for a in anchored if lo <= a[0] < hi]
            anc, dep, _tid, template = in_window[0] if in_window else min(
                anchored, key=lambda a: min(abs(a[0] - lo), abs(a[0] - hi)))
            for i in range(n_trips):
                n += 1
                target = lo + (hi - lo) * (i + 1) // (n_trips + 1)
                added.append(self._shifted_trip(template, target - (anc - dep),
                                                f"ADD_{target_route_id}_{n}"))
        if not added:
            return self.bus_edges, self.walk_edges
        return pd.concat([self.bus_edges] + added, ignore_index=True), self.walk_edges

    def edges_after_timetable_shift(self, target_route_id, from_hours, to_hours, n_trips=1,
                                    via_stops=None):
        """便数を変えずに時間帯を移した (bus_edges, walk_edges) を返す（費用中立）。

        from_hours=(開始,終了) に発車する便を、パターンごとに最大 n_trips 本
        to_hours の時間帯へ移す。総便数・総走行距離は変わらないため、
        「増便せずに時間帯を組み替えるだけで空白を減らせるか」を検証できる。
        移す便は from_hours の遅い側から選ぶ（早朝側のサービスを残すため）。
        via_stops を渡すと、時間帯の判定と移動先の時刻を「拠点を通る時刻」で行う。
        """
        lo_f, hi_f = from_hours[0] * 3600, from_hours[1] * 3600
        lo_t, hi_t = to_hours[0] * 3600, to_hours[1] * 3600
        via = set(via_stops or ())
        removed_ids, added, n = set(), [], 0
        for trips in self._trip_patterns(target_route_id).values():
            in_window = [a for a in self._anchored(trips, via) if lo_f <= a[0] < hi_f]
            picked = in_window[-n_trips:] if n_trips > 0 else []
            for i, (anc, dep, tid, g) in enumerate(picked):
                n += 1
                target = lo_t + (hi_t - lo_t) * (i + 1) // (len(picked) + 1)
                removed_ids.add(tid)
                added.append(self._shifted_trip(g, target - (anc - dep),
                                                f"SHIFT_{target_route_id}_{n}"))
        if not added:
            return self.bus_edges, self.walk_edges
        kept = self.bus_edges[~self.bus_edges["trip_id"].isin(removed_ids)]
        return pd.concat([kept] + added, ignore_index=True), self.walk_edges

    def route_length_km(self, route_id):
        """路線の片道距離(km)。費用代理（便数×路線長）に使う。

        停留所数が最も多い便の連続区間を実距離で足す。shapes.txt を使えば
        道路形状に沿った距離になるが、費用の相対比較には停留所間直線距離で足りる。

        費用代理の算出で全路線ぶんを繰り返し呼ぶため結果をキャッシュする。
        路線の経路はシナリオ（増便・ダイヤシフト）では変わらないので、
        ダイヤ種別を切り替えたときだけ破棄すればよい。
        """
        if route_id in self._route_length_cache:
            return self._route_length_cache[route_id]
        patterns = self._trip_patterns(route_id)
        if not patterns:
            self._route_length_cache[route_id] = 0.0
            return 0.0
        longest = max((t[2] for trips in patterns.values() for t in trips), key=len)
        total = 0.0
        for row in longest.itertuples(index=False):
            if row.from_stop in self.stop_coords.index and row.to_stop in self.stop_coords.index:
                a = self.stop_coords.loc[row.from_stop]
                b = self.stop_coords.loc[row.to_stop]
                total += haversine(a["stop_lat"], a["stop_lon"], b["stop_lat"], b["stop_lon"])
        self._route_length_cache[route_id] = total / 1000.0
        return self._route_length_cache[route_id]

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

        # 新路線のエッジを生成。行きは指定順、帰りは逆順に、それぞれ始発から時刻を積み上げる。
        # 以前は帰りの便に行きと同じ区間時刻を割り当てていたため、帰りは時刻をさかのぼる
        # 並びになって2区間以上続けて乗れず、区間ごとに次の便を待つ計算になっていた
        # （5停留所・30分間隔で、行き20分に対し帰り95分）。
        new_edges = []
        stops = [s for s in new_route_stops if s in self.stop_coords.index]
        if len(stops) >= 2:
            # 区間ごとの所要秒数（直線距離÷平均速度）
            seg_sec = []
            for s1, s2 in zip(stops, stops[1:]):
                a, b = self.stop_coords.loc[s1], self.stop_coords.loc[s2]
                dist_m = hav(a["stop_lat"], a["stop_lon"], b["stop_lat"], b["stop_lon"])
                seg_sec.append(int(dist_m / (speed_kmh * 1000 / 3600)))

            # 5時〜22時の間、interval_min間隔で両方向とも始発を出す
            interval_sec = interval_min * 60
            for trip_num, dep_base_sec in enumerate(range(5*3600, 22*3600, interval_sec)):
                for suffix, seq, secs in (("", stops, seg_sec), ("_R", stops[::-1], seg_sec[::-1])):
                    t = dep_base_sec
                    for (s1, s2), travel_sec in zip(zip(seq, seq[1:]), secs):
                        new_edges.append({
                            "from_stop": s1,
                            "to_stop": s2,
                            "departure_sec": t,
                            "arrival_sec": t + travel_sec,
                            "travel_time": travel_sec,
                            "trip_id": f"NEW_TRIP_{trip_num}{suffix}",
                            "route_id": "NEW_ROUTE",
                            "type": "bus"
                        })
                        t += travel_sec + 30  # 停車30秒

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
