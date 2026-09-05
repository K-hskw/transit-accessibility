"""時間空白の診断

「空白人口」を、起点1バス停からの到達圏ではなく
『各メッシュから生活拠点に、指定時刻までに公共交通で着けるか』で定義する。

この向きで定義すると、拠点1つ・時間帯1つあたり逆方向ダイクストラ1回で
全メッシュ分の判定が出る。全メッシュを起点に順方向探索する必要がない。

メッシュ重心から最寄りバス停までの徒歩区間をネットワークに含める点が
既存の到達圏計算との違い。バス停間の徒歩エッジ(300m)だけでは
「そもそもバス停まで歩けるか」が評価されない。
"""

import numpy as np
import pandas as pd

from population import haversine_matrix


class BlankAreaAnalyzer:
    def __init__(self, engine, pop_data, max_walk_m=500, walk_speed_m_min=67):
        """
        engine          : TransitEngine
        pop_data        : PopulationData（メッシュ人口）
        max_walk_m      : メッシュ重心から乗車できる最大徒歩距離
        walk_speed_m_min: 徒歩速度（m/分）。一般67 / 高齢者40
        """
        self.engine = engine
        self.pop = pop_data
        self.max_walk_m = max_walk_m
        self.walk_speed_m_min = walk_speed_m_min

        self.stop_ids = engine.stop_coords.index.to_numpy()
        self._stop_pos = {sid: i for i, sid in enumerate(self.stop_ids)}

        # メッシュ×バス停のうち徒歩圏に入る組だけを保持する（疎な対応表）
        mesh_lat = pop_data._lat
        mesh_lon = pop_data._lon
        stop_lat = engine.stop_coords["stop_lat"].to_numpy(dtype=float)
        stop_lon = engine.stop_coords["stop_lon"].to_numpy(dtype=float)

        pair_mesh, pair_stop, pair_dist = [], [], []
        chunk = max(1, 4_000_000 // max(len(stop_lat), 1))
        for i in range(0, len(mesh_lat), chunk):
            d = haversine_matrix(mesh_lat[i:i + chunk], mesh_lon[i:i + chunk],
                                 stop_lat, stop_lon)
            mi, si = np.nonzero(d <= max_walk_m)
            pair_mesh.append(mi + i)
            pair_stop.append(si)
            pair_dist.append(d[mi, si])

        self.pair_mesh = np.concatenate(pair_mesh) if pair_mesh else np.array([], dtype=int)
        self.pair_stop = np.concatenate(pair_stop) if pair_stop else np.array([], dtype=int)
        self.pair_dist = np.concatenate(pair_dist) if pair_dist else np.array([], dtype=float)

        self.n_mesh = len(mesh_lat)
        # 徒歩圏にバス停が1つも無いメッシュ＝時刻によらない「静的空白」
        served = np.zeros(self.n_mesh, dtype=bool)
        served[self.pair_mesh] = True
        self.static_blank = ~served

    def _walk_sec(self, speed_factor=1.0):
        """メッシュ→バス停の徒歩所要時間（秒）。speed_factorで減速を表現する"""
        speed_m_sec = (self.walk_speed_m_min * speed_factor) / 60.0
        return self.pair_dist / speed_m_sec

    def latest_departure_by_mesh(self, dest_stop_ids, arrival_time_sec, max_time_sec,
                                 speed_factor=1.0):
        """各メッシュを『何時までに出れば拠点に間に合うか』の最遅出発時刻を返す

        到達できないメッシュは -inf。
        """
        dest_stop_ids = list(dest_stop_ids)
        if not dest_stop_ids:
            # 拠点が空だと全メッシュが「到達不能」になり、存在しない停留所名を
            # 渡した事故が『空白人口＝総人口』として黙って通ってしまう
            raise ValueError("拠点となる停留所IDが空です（停留所名の綴りを確認してください）")
        unknown = [s for s in dest_stop_ids if s not in self._stop_pos]
        if unknown:
            raise ValueError(f"未知の停留所ID: {unknown}")

        # 拠点（同名の複数ポール等）ごとに逆方向到達圏を求め、最も遅い出発時刻を採る
        best_dep = np.full(len(self.stop_ids), -np.inf)
        for dest in dest_stop_ids:
            rev = self.engine.calc_reverse_isochrone(dest, arrival_time_sec, max_time_sec)
            for sid, dep in rev.items():
                pos = self._stop_pos.get(sid)
                if pos is not None and dep > best_dep[pos]:
                    best_dep[pos] = dep

        # メッシュ→徒歩→バス停 の分だけ出発時刻を前倒しし、メッシュごとに最遅を採る
        walk = self._walk_sec(speed_factor)
        cand = best_dep[self.pair_stop] - walk
        mesh_dep = np.full(self.n_mesh, -np.inf)
        np.maximum.at(mesh_dep, self.pair_mesh, cand)
        return mesh_dep

    def diagnose(self, dest_stop_ids, arrival_time_sec, max_time_sec, speed_factor=1.0):
        """メッシュ単位の空白判定と人口集計を返す"""
        mesh_dep = self.latest_departure_by_mesh(
            dest_stop_ids, arrival_time_sec, max_time_sec, speed_factor)
        earliest = arrival_time_sec - max_time_sec
        reachable = mesh_dep >= earliest

        df = pd.DataFrame({
            "lat": self.pop._lat,
            "lon": self.pop._lon,
            "pop": self.pop._pop,
            "elderly": self.pop._eld,
            "reachable": reachable,
            "static_blank": self.static_blank,
            # 余裕時間（分）: 大きいほど早く出ても間に合う ＝ 便が選べる
            "margin_min": np.where(reachable, (mesh_dep - earliest) / 60.0, np.nan),
        })
        return df

    def summarize(self, df):
        blank = ~df["reachable"]
        return {
            "blank_pop": round(float(df.loc[blank, "pop"].sum())),
            "blank_elderly": round(float(df.loc[blank, "elderly"].sum())),
            "blank_mesh": int(blank.sum()),
            "total_pop": round(float(df["pop"].sum())),
            "static_blank_pop": round(float(df.loc[df["static_blank"], "pop"].sum())),
        }

    def diagnose_by_hour(self, dest_stop_ids, hours, max_time_sec, speed_factor=1.0):
        """時間帯別の空白人口。3Dカラム表示用のデータ源"""
        rows = []
        for h in hours:
            df = self.diagnose(dest_stop_ids, h * 3600, max_time_sec, speed_factor)
            s = self.summarize(df)
            s["hour"] = h
            rows.append(s)
        return pd.DataFrame(rows)[
            ["hour", "blank_pop", "blank_elderly", "blank_mesh", "static_blank_pop", "total_pop"]
        ]
