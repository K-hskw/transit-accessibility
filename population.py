import numpy as np
import pandas as pd
from math import radians, sin, cos, sqrt, atan2


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))


def haversine_matrix(lat_a, lon_a, lat_b, lon_b):
    """(len(a), len(b)) の距離行列（メートル）を返す。上の haversine と同じ式。"""
    R = 6371000
    la = np.radians(np.asarray(lat_a, dtype=float))[:, None]
    lo = np.radians(np.asarray(lon_a, dtype=float))[:, None]
    lb = np.radians(np.asarray(lat_b, dtype=float))[None, :]
    lob = np.radians(np.asarray(lon_b, dtype=float))[None, :]
    a = np.sin((lb - la) / 2) ** 2 + np.cos(la) * np.cos(lb) * np.sin((lob - lo) / 2) ** 2
    return R * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))


def mesh_level(meshcode):
    """メッシュコードの桁数から粒度を判別し、(名称, 1辺のメートル) を返す

    国勢調査のメッシュ統計は全国で 1km と 500m が整備されている一方、
    室蘭で使っている「簡易100mメッシュ人口」は10桁。桁数で見分けることで、
    どの粒度の人口データでも同じコードで扱えるようにする。

        8桁  63403779    3次メッシュ    約1km
        9桁  634037791   4次メッシュ    約500m（末尾は1〜4の象限）
       10桁  6340377901  100mメッシュ   （末尾2桁は0〜9の行・列）

    ※10桁には5次メッシュ（250m、末尾2桁が各1〜4）もあり形式だけでは
      区別できない。本ツールは100mメッシュとして扱う。
    """
    s = str(meshcode).strip()
    if len(s) == 8:
        return "1km", 1000.0
    if len(s) == 9:
        return "500m", 500.0
    if len(s) == 10:
        return "100m", 100.0
    raise ValueError(f"対応していないメッシュコードです（8/9/10桁）: {meshcode}")


def meshcode_to_latlon(meshcode):
    """メッシュコードから、そのセルの中心の緯度経度を返す（8/9/10桁に対応）"""
    s = str(meshcode).strip()
    level, _size = mesh_level(s)

    # 3次メッシュ（約1km）の南西角
    lat = int(s[0:2]) / 1.5 + int(s[4]) / 12 + int(s[6]) / 120
    lon = int(s[2:4]) + 100 + int(s[5]) / 8 + int(s[7]) / 80
    # 3次メッシュ1つぶんの大きさ
    dlat, dlon = 1 / 120, 1 / 80

    if level == "500m":
        # 4次メッシュ: 1=南西 2=南東 3=北西 4=北東
        q = int(s[8])
        dlat, dlon = dlat / 2, dlon / 2
        if q in (3, 4):
            lat += dlat
        if q in (2, 4):
            lon += dlon
    elif level == "100m":
        # 末尾2桁が3次メッシュ内の行・列（各0〜9）
        dlat, dlon = dlat / 10, dlon / 10
        lat += int(s[8]) * dlat
        lon += int(s[9]) * dlon

    # 南西角にセルの半分を足して中心にする
    return lat + dlat / 2, lon + dlon / 2


class PopulationData:
    def __init__(self, csv_path):
        # 政策文書に自治体名を載せるためにファイル名を覚えておく
        self.source_path = str(csv_path)
        self.df = pd.read_csv(csv_path)
        # メッシュコードから緯度経度を計算
        coords = self.df["Meshcode"].apply(lambda m: meshcode_to_latlon(m))
        self.df["lat"] = coords.apply(lambda x: x[0])
        self.df["lon"] = coords.apply(lambda x: x[1])

        # 距離計算をベクトル化するための配列（iterrows だとバス停×メッシュで
        # 秒単位の時間がかかり、時間帯別×全メッシュの集計に耐えられない）
        self._lat = self.df["lat"].to_numpy(dtype=float)
        self._lon = self.df["lon"].to_numpy(dtype=float)
        self._pop = self.df["PopT"].to_numpy(dtype=float)
        self._eld = self.df["Pop65over"].to_numpy(dtype=float)

    def _covered_mask(self, stop_coords, stop_ids, radius_m):
        """指定バス停群のradius_m圏内に入るメッシュの真偽値配列を返す"""
        ids = [sid for sid in stop_ids if sid in stop_coords.index]
        if not ids:
            return np.zeros(len(self.df), dtype=bool)
        sub = stop_coords.loc[ids]
        mask = np.zeros(len(self.df), dtype=bool)
        # メモリを抑えるため停留所を分割して処理する
        chunk = max(1, 4_000_000 // max(len(self.df), 1))
        for i in range(0, len(sub), chunk):
            part = sub.iloc[i:i + chunk]
            d = haversine_matrix(part["stop_lat"].to_numpy(), part["stop_lon"].to_numpy(),
                                 self._lat, self._lon)
            mask |= (d <= radius_m).any(axis=0)
        return mask

    def get_population_in_radius(self, lat, lon, radius_m):
        """指定地点からradius_m以内のメッシュの人口合計を返す"""
        d = haversine_matrix([lat], [lon], self._lat, self._lon)[0]
        mask = d <= radius_m
        return {
            "total": round(float(self._pop[mask].sum())),
            "elderly": round(float(self._eld[mask].sum())),
            "mesh_count": int(mask.sum())
        }

    def get_population_near_stops(self, stop_coords, stop_ids, radius_m=300):
        """指定バス停群のradius_m圏内の人口合計を返す（重複除外）"""
        mask = self._covered_mask(stop_coords, stop_ids, radius_m)
        return {
            "total": round(float(self._pop[mask].sum())),
            "elderly": round(float(self._eld[mask].sum())),
            "mesh_count": int(mask.sum())
        }

    def calc_impact_population(self, stop_coords, lost_stop_ids, all_reachable_stop_ids, radius_m=300):
        """
        到達不能になったバス停の周辺で、他の到達可能バス停からもカバーされていない人口を算出
        """
        lost_mask = self._covered_mask(stop_coords, lost_stop_ids, radius_m)
        remaining = set(all_reachable_stop_ids) - set(lost_stop_ids)
        covered_mask = self._covered_mask(stop_coords, remaining, radius_m)

        # 到達不能バス停のカバー範囲のうち、残存バス停でカバーされていないメッシュ
        uncovered = lost_mask & ~covered_mask
        return {
            "affected_total": round(float(self._pop[uncovered].sum())),
            "affected_elderly": round(float(self._eld[uncovered].sum())),
            "uncovered_meshes": int(uncovered.sum()),
            "lost_meshes": int(lost_mask.sum()),
            "covered_by_others": int(lost_mask.sum()) - int(uncovered.sum())
        }

class FacilityData:
    def __init__(self, csv_path="facilities.csv"):
        self.df = pd.read_csv(csv_path)
        self.facility_types = sorted(self.df["type"].unique())

    def get_facilities_by_type(self, facility_type):
        return self.df[self.df["type"] == facility_type]

    def find_nearest_stops(self, facilities, stop_coords, max_distance=500):
        """各施設の最寄りバス停を特定し、施設までの徒歩時間を返す"""
        if len(facilities) == 0 or len(stop_coords) == 0:
            return []

        # 施設×バス停の距離行列を一括計算する（二重ループ＋.loc は非常に遅い）
        d = haversine_matrix(
            facilities["latitude"].to_numpy(), facilities["longitude"].to_numpy(),
            stop_coords["stop_lat"].to_numpy(), stop_coords["stop_lon"].to_numpy()
        )
        nearest_idx = d.argmin(axis=1)
        nearest_dist = d[np.arange(len(d)), nearest_idx]
        stop_ids = stop_coords.index.to_numpy()

        results = []
        for i, (_, fac) in enumerate(facilities.iterrows()):
            best_dist = float(nearest_dist[i])
            if best_dist <= max_distance:
                results.append({
                    "facility_name": fac["name"],
                    "facility_type": fac["type"],
                    "facility_lat": fac["latitude"],
                    "facility_lon": fac["longitude"],
                    "nearest_stop": stop_ids[nearest_idx[i]],
                    "distance_m": round(best_dist),
                    "walk_time_sec": round((best_dist / 67) * 60)
                })
        return results

    def calc_facility_access(self, isochrone_result, start_time_sec, facilities, stop_coords,
                              walk_speed=67, max_walk_distance=500):
        """各施設へのアクセス可能性を計算"""
        nearest = self.find_nearest_stops(facilities, stop_coords, max_walk_distance)
        access_results = []
        for fac in nearest:
            stop_id = fac["nearest_stop"]
            walk_sec = fac["walk_time_sec"]

            if stop_id in isochrone_result:
                bus_arrival = isochrone_result[stop_id]
                total_time = (bus_arrival - start_time_sec) + walk_sec
                total_min = total_time / 60
                accessible = True
            else:
                total_min = None
                accessible = False

            stop_name = ""
            if stop_id in stop_coords.index:
                stop_name = stop_coords.loc[stop_id, "stop_name"]

            access_results.append({
                "facility_name": fac["facility_name"],
                "facility_type": fac["facility_type"],
                "facility_lat": fac["facility_lat"],
                "facility_lon": fac["facility_lon"],
                "nearest_stop": stop_name,
                "walk_distance_m": fac["distance_m"],
                "walk_time_min": round(walk_sec / 60, 1),
                "total_time_min": round(total_min, 1) if total_min else None,
                "accessible": accessible
            })

        return access_results

if __name__ == "__main__":
    pop = PopulationData("100m_mesh_pop2020_01205室蘭市.csv")
    print(f"メッシュ数: {len(pop.df)}")
    print(f"室蘭市総人口: {round(pop.df['PopT'].sum())}")
    print(f"65歳以上: {round(pop.df['Pop65over'].sum())}")
    print(f"高齢化率: {pop.df['Pop65over'].sum() / pop.df['PopT'].sum() * 100:.1f}%")

    # 室蘭駅前周辺300mの人口
    result = pop.get_population_in_radius(42.3153, 140.9734, 300)
    print(f"\n室蘭駅前 300m圏内: 人口{result['total']}人, 高齢者{result['elderly']}人")