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


def aggregate_to_500m(df):
    """100mメッシュの診断結果を500mメッシュ（1/2地域メッシュ）に集約する

    設計メモは250mメッシュを想定しているが、100mメッシュは1kmを10等分した
    ものなので250m（4等分）には整数で割り切れない。標準メッシュ体系で
    100mから素直に作れるのは500m（1kmの2等分・5×5セル）なのでこれを採る。

    人口は合計、空白判定は「そのセルの人口の過半が空白なら空白」とする。
    """
    code = df["meshcode"].astype(str)
    km = code.str[:8]
    row = code.str[8].astype(int)
    col = code.str[9].astype(int)
    half_row = row // 5
    half_col = col // 5

    # 500mセルの中心座標をメッシュコードから直接計算する（欠損セルがあっても
    # 重心がずれないよう、構成セルの平均ではなくセル自体の中心を使う）
    lat1 = km.str[0:2].astype(int)
    lon1 = km.str[2:4].astype(int)
    lat2 = km.str[4].astype(int)
    lon2 = km.str[5].astype(int)
    lat3 = km.str[6].astype(int)
    lon3 = km.str[7].astype(int)
    cell_lat = lat1 / 1.5 + lat2 / 12 + lat3 / 120 + (half_row * 5 + 2.5) / 1200
    cell_lon = lon1 + 100 + lon2 / 8 + lon3 / 80 + (half_col * 5 + 2.5) / 800

    work = df.assign(
        cell=km + half_row.astype(str) + half_col.astype(str),
        cell_lat=cell_lat,
        cell_lon=cell_lon,
        blank_pop=np.where(df["reachable"], 0.0, df["pop"]),
        blank_elderly=np.where(df["reachable"], 0.0, df["elderly"]),
    )
    agg = dict(
        lat=("cell_lat", "first"),
        lon=("cell_lon", "first"),
        pop=("pop", "sum"),
        elderly=("elderly", "sum"),
        blank_pop=("blank_pop", "sum"),
        blank_elderly=("blank_elderly", "sum"),
        static_blank=("static_blank", "all"),
    )
    if "blank_hours" in work.columns:
        # セル内で最も長く空白になるメッシュに合わせる（深刻度は過小評価しない）
        agg["blank_hours"] = ("blank_hours", "max")

    g = work.groupby("cell", as_index=False).agg(**agg)
    g["reachable"] = g["blank_pop"] <= g["pop"] / 2
    return g


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
            "meshcode": self.pop.df["Meshcode"].astype(str).to_numpy(),
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

    def blank_hours_by_mesh(self, dest_stop_ids, hours, max_time_sec, speed_factor=1.0):
        """各メッシュが何時間帯で空白になるかを数える（色分けの「深刻度」用）

        終日空白の地域と、特定の時間帯だけ空白になる地域を区別するための指標。
        「同じ場所が時間帯で空白化する」という主張はこの差で示す。
        """
        counts = None
        for h in hours:
            df = self.diagnose(dest_stop_ids, h * 3600, max_time_sec, speed_factor)
            blank = (~df["reachable"]).to_numpy().astype(int)
            counts = blank if counts is None else counts + blank
        return counts

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
