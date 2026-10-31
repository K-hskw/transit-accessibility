"""時間空白の診断

「空白人口」を、起点1バス停からの到達圏ではなく
『各メッシュから生活拠点に、指定時刻までに公共交通で着けるか』で定義する。
正確には「到着時刻 T の制限時間前（T − max_time_sec）以降に家を出て、T までに
拠点へ着ける経路があるか」。早く着いて拠点で待つ時間も制限時間に含むので、
T を遅らせても空白が減るとは限らない（休日 7:45着17.5% → 8:00着27.5%）。
早すぎる到着を「着ける」と数えると、始発で6時に着いて9時まで待つ人も
「9時に着ける」ことになり、時間空白が見えなくなるため、この定義にしている。

この向きで定義すると、拠点1つ・時間帯1つあたり逆方向ダイクストラ1回で
全メッシュ分の判定が出る。全メッシュを起点に順方向探索する必要がない。

メッシュ重心から最寄りバス停までの徒歩区間をネットワークに含める点が
既存の到達圏計算との違い。バス停間の徒歩エッジ(300m)だけでは
「そもそもバス停まで歩けるか」が評価されない。

時間帯の値は、到着時刻を5分刻みにして判定した結果の平均とする（DayDiagnosis）。
毎時00分の1点だけで測ると、数分の違いで便に間に合うかが変わり、結果が大きく
揺れる。室蘭の休日では 7:00着 46.1% / 7:15着 28.2% / 7:30着 20.4% で、
7:00着は7時台で最も悪い時刻だった。
"""

import numpy as np
import pandas as pd

from population import haversine_matrix, mesh_level

# 到着時刻の刻み（分）と、診断する時間帯（5時台〜22時台 ＝ 5:00〜22:55着）
STEP_MIN = 5
DAY_HOURS = tuple(range(5, 23))


def arrival_times(hours=DAY_HOURS, step_min=STEP_MIN):
    """各時間帯の到着時刻（秒）を刻み幅ごとに並べる。H時台 ＝ H:00〜H:55着（5分刻み）"""
    if step_min <= 0 or 60 % step_min:
        raise ValueError(f"刻み幅は60の約数（分）にしてください: {step_min}")
    return np.array([h * 3600 + m * 60 for h in hours for m in range(0, 60, step_min)])


def clock(sec):
    """秒を 7:05 の形にする"""
    sec = int(sec)
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}"


def aggregate_to_500m(df):
    """診断結果を表示用のセルに集約する（入力メッシュの粒度を自動判別）

    100mメッシュなら5×5をまとめて500mセルにする。設計メモは250mを想定して
    いたが、100mメッシュは1kmを10等分したものなので250m（4等分）には整数で
    割り切れない。標準メッシュ体系で100mから素直に作れるのが500mのため。

    国勢調査のメッシュ統計は全国で500mと1kmが整備されており、これらが
    入力された場合はすでに表示に適した粒度なのでそのまま1セルとして扱う。

    人口は合計、空白判定は「そのセルの人口の過半が空白なら空白」とする。
    時間帯の平均（DayDiagnosis.hour_mesh）のように blank_pop 列がすでにあれば、
    メッシュの空白人口は「人口×着けなかった時刻の割合」なので、それをそのまま合計する。
    """
    code = df["meshcode"].astype(str)
    km = code.str[:8]
    # 3次メッシュ（約1km）南西角
    base_lat = km.str[0:2].astype(int) / 1.5 + km.str[4].astype(int) / 12 + km.str[6].astype(int) / 120
    base_lon = km.str[2:4].astype(int) + 100 + km.str[5].astype(int) / 8 + km.str[7].astype(int) / 80

    level = mesh_level(code.iloc[0])[0] if len(code) else "100m"
    if level == "100m":
        # 5×5 の100mセルを1つの500mセルにまとめる
        half_row = code.str[8].astype(int) // 5
        half_col = code.str[9].astype(int) // 5
        cell_id = km + half_row.astype(str) + half_col.astype(str)
        # セル中心はコードから直接求める（構成セルが欠けても重心がずれない）
        cell_lat = base_lat + (half_row * 5 + 2.5) / 1200
        cell_lon = base_lon + (half_col * 5 + 2.5) / 800
    elif level == "500m":
        # すでに500m。象限（1=南西 2=南東 3=北西 4=北東）から中心を出す
        q = code.str[8].astype(int)
        cell_id = code
        cell_lat = base_lat + np.where(q.isin([3, 4]), 1.5, 0.5) / 240
        cell_lon = base_lon + np.where(q.isin([2, 4]), 1.5, 0.5) / 160
    else:
        # 1km。分割せずそのまま1セルとする
        cell_id = code
        cell_lat = base_lat + 0.5 / 120
        cell_lon = base_lon + 0.5 / 80

    if "blank_pop" in df.columns:
        blank_pop = df["blank_pop"].to_numpy(dtype=float)
        blank_elderly = df["blank_elderly"].to_numpy(dtype=float)
    else:
        blank_pop = np.where(df["reachable"], 0.0, df["pop"])
        blank_elderly = np.where(df["reachable"], 0.0, df["elderly"])
    work = df.assign(
        cell=cell_id,
        cell_lat=cell_lat,
        cell_lon=cell_lon,
        blank_pop=blank_pop,
        blank_elderly=blank_elderly,
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

    def served_share(self):
        """歩ける範囲にバス停がある人口の割合（0〜1）

        人口と時刻表の地域がずれていないかの確認に使う。別の都市のGTFSに室蘭の人口が
        残ったまま診断すると、全メッシュが静的空白になり、エラーも出ずに空白人口100%と
        表示されていた。
        """
        total = float(self.pop._pop.sum())
        return float(self.pop._pop[~self.static_blank].sum()) / total if total > 0 else 0.0

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

    def diagnose_day(self, dest_stop_ids, max_time_sec, hours=DAY_HOURS, step_min=STEP_MIN,
                     speed_factor=1.0):
        """到着時刻を刻み幅ごとに動かして診断する（時間帯の値はその平均）

        1時刻あたり約10msなので、5〜22時台を5分刻み（216通り）にしても2秒台で済む。
        """
        times = arrival_times(hours, step_min)
        earliest = times - max_time_sec
        blank = np.empty((len(times), self.n_mesh), dtype=bool)
        for i, t in enumerate(times):
            mesh_dep = self.latest_departure_by_mesh(
                dest_stop_ids, int(t), max_time_sec, speed_factor)
            blank[i] = ~(mesh_dep >= earliest[i])
        return DayDiagnosis(self, hours, step_min, times, blank)

    def blank_hours_by_mesh(self, dest_stop_ids, hours, max_time_sec, speed_factor=1.0):
        """各メッシュが1日のうち何時間、拠点に着けないか（色分けの「深刻度」用）

        終日空白の地域と、特定の時間帯だけ空白になる地域を区別するための指標。
        「同じ場所が時間帯で空白化する」という主張はこの差で示す。
        以前は毎時00分に空白だった回数を数えていたが、00分の前後で便に
        間に合うかが変わるだけで1時間ぶん増減したため、5分刻みの時間にした。
        """
        return self.diagnose_day(dest_stop_ids, max_time_sec, hours,
                                 speed_factor=speed_factor).blank_hours()

    def diagnose_by_hour(self, dest_stop_ids, hours, max_time_sec, speed_factor=1.0):
        """時間帯別の空白人口（各時間帯の平均と、到着時刻による幅）"""
        return self.diagnose_day(dest_stop_ids, max_time_sec, hours,
                                 speed_factor=speed_factor).hourly()


class DayDiagnosis:
    """到着時刻を5分刻みにした診断結果

    blank[i, m] が True なら、到着時刻 times[i] にメッシュ m の人は拠点に着けない。
    時間帯（H時台）の空白人口は H:00〜H:55着の12通りの平均とする。
    メッシュの空白人口は「人口×その時間帯で着けなかった時刻の割合」になり、
    全メッシュを足すと時間帯の平均に一致する。
    """

    def __init__(self, analyzer, hours, step_min, times, blank):
        self.hours = [int(h) for h in hours]
        self.step_min = int(step_min)
        self.per_hour = 60 // self.step_min
        self.times = np.asarray(times)
        self.blank = blank
        pop = analyzer.pop
        self.meshcode = pop.df["Meshcode"].astype(str).to_numpy()
        self.lat, self.lon = pop._lat, pop._lon
        self.pop, self.elderly = pop._pop, pop._eld
        self.static_blank = analyzer.static_blank
        # 到着時刻ごとの空白人口。行列積は環境によって足す順序が変わり端数が
        # ずれうるので、行ごとに同じ順序で足す（施策の前後比較で、同じ判定なのに
        # 端数の差を「変化した」と数えないため）。
        self.blank_pop_t = np.array([self.pop[row].sum() for row in blank])
        self.blank_elderly_t = np.array([self.elderly[row].sum() for row in blank])

    def _rows(self, hour):
        if hour not in self.hours:
            raise ValueError(f"{hour}時台は計算していません（{self.hours[0]}〜{self.hours[-1]}時台）")
        i = self.hours.index(hour) * self.per_hour
        return slice(i, i + self.per_hour)

    def hour_times(self, hour):
        """その時間帯の到着時刻（秒）"""
        return self.times[self._rows(hour)]

    def hour_share(self, hour):
        """メッシュごとに、その時間帯で着けなかった到着時刻の割合（0〜1）"""
        return self.blank[self._rows(hour)].mean(axis=0)

    def hour_mesh(self, hour):
        """その時間帯のメッシュ別の結果（空白人口は時間帯の平均）"""
        share = self.hour_share(hour)
        return pd.DataFrame({
            "meshcode": self.meshcode,
            "lat": self.lat,
            "lon": self.lon,
            "pop": self.pop,
            "elderly": self.elderly,
            "blank_share": share,
            "blank_pop": self.pop * share,
            "blank_elderly": self.elderly * share,
            # その時間帯のどの到着時刻でも着けるなら空白ではない
            "reachable": share == 0,
            "static_blank": self.static_blank,
        })

    def hour_summary(self, hour):
        """その時間帯の空白人口（平均）と、到着時刻による幅"""
        r = self._rows(hour)
        bp, be, ts = self.blank_pop_t[r], self.blank_elderly_t[r], self.times[r]
        return {
            "hour": hour,
            "blank_pop": round(float(bp.mean())),
            "blank_elderly": round(float(be.mean())),
            "blank_mesh": int((self.hour_share(hour) > 0).sum()),
            "total_pop": round(float(self.pop.sum())),
            "static_blank_pop": round(float(self.pop[self.static_blank].sum())),
            "blank_pop_min": round(float(bp.min())),
            "blank_pop_max": round(float(bp.max())),
            "time_min": int(ts[bp.argmin()]),
            "time_max": int(ts[bp.argmax()]),
        }

    def hourly(self):
        """全時間帯の hour_summary を表にする"""
        return pd.DataFrame([self.hour_summary(h) for h in self.hours])

    def hourly_blank(self):
        """時間帯ごとの (空白人口の平均, うち高齢者の平均)。端数は丸めない"""
        shape = (len(self.hours), self.per_hour)
        return (self.blank_pop_t.reshape(shape).mean(axis=1),
                self.blank_elderly_t.reshape(shape).mean(axis=1))

    def series(self):
        """到着時刻ごと（5分刻み）の空白人口"""
        return pd.DataFrame({
            "time_sec": self.times,
            "blank_pop": self.blank_pop_t,
            "blank_elderly": self.blank_elderly_t,
        })

    def blank_hours(self):
        """メッシュごとに、1日のうち拠点に着けない時間（時間）＝ 刻み幅 × 着けなかった回数"""
        return self.blank.sum(axis=0) * self.step_min / 60.0
