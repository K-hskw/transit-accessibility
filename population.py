import pandas as pd
from math import radians, sin, cos, sqrt, atan2


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1-a))


def meshcode_to_latlon(meshcode):
    """100mメッシュコード（10桁）から中心緯度経度を返す"""
    s = str(meshcode)
    # 1次メッシュ（4桁）
    lat1 = int(s[0:2])
    lon1 = int(s[2:4])
    # 2次メッシュ（2桁）
    lat2 = int(s[4])
    lon2 = int(s[5])
    # 3次メッシュ（2桁）
    lat3 = int(s[6])
    lon3 = int(s[7])
    # 4次メッシュ（100mメッシュ、2桁）
    lat4 = int(s[8])
    lon4 = int(s[9])

    lat = lat1 / 1.5 + lat2 / 12 + lat3 / 120 + lat4 / 1200 + 1/2400
    lon = lon1 + 100 + lon2 / 8 + lon3 / 80 + lon4 / 800 + 1/1600

    return lat, lon


class PopulationData:
    def __init__(self, csv_path):
        self.df = pd.read_csv(csv_path)
        # メッシュコードから緯度経度を計算
        coords = self.df["Meshcode"].apply(lambda m: meshcode_to_latlon(m))
        self.df["lat"] = coords.apply(lambda x: x[0])
        self.df["lon"] = coords.apply(lambda x: x[1])

    def get_population_in_radius(self, lat, lon, radius_m):
        """指定地点からradius_m以内のメッシュの人口合計を返す"""
        total = 0
        total_elderly = 0
        count = 0
        for _, row in self.df.iterrows():
            dist = haversine(lat, lon, row["lat"], row["lon"])
            if dist <= radius_m:
                total += row["PopT"]
                total_elderly += row["Pop65over"]
                count += 1
        return {
            "total": round(total),
            "elderly": round(total_elderly),
            "mesh_count": count
        }

    def get_population_near_stops(self, stop_coords, stop_ids, radius_m=300):
        """指定バス停群のradius_m圏内の人口合計を返す（重複除外）"""
        covered_meshes = set()
        total_pop = 0
        total_elderly = 0

        for sid in stop_ids:
            if sid not in stop_coords.index:
                continue
            slat = stop_coords.loc[sid, "stop_lat"]
            slon = stop_coords.loc[sid, "stop_lon"]

            for idx, row in self.df.iterrows():
                if idx in covered_meshes:
                    continue
                dist = haversine(slat, slon, row["lat"], row["lon"])
                if dist <= radius_m:
                    covered_meshes.add(idx)
                    total_pop += row["PopT"]
                    total_elderly += row["Pop65over"]

        return {
            "total": round(total_pop),
            "elderly": round(total_elderly),
            "mesh_count": len(covered_meshes)
        }

    def calc_impact_population(self, stop_coords, lost_stop_ids, all_reachable_stop_ids, radius_m=300):
        """
        到達不能になったバス停の周辺で、他の到達可能バス停からもカバーされていない人口を算出
        """
        # 到達不能バス停のカバー範囲
        lost_meshes = set()
        for sid in lost_stop_ids:
            if sid not in stop_coords.index:
                continue
            slat = stop_coords.loc[sid, "stop_lat"]
            slon = stop_coords.loc[sid, "stop_lon"]
            for idx, row in self.df.iterrows():
                dist = haversine(slat, slon, row["lat"], row["lon"])
                if dist <= radius_m:
                    lost_meshes.add(idx)

        # 残存する到達可能バス停のカバー範囲
        remaining_stop_ids = set(all_reachable_stop_ids) - set(lost_stop_ids)
        covered_meshes = set()
        for sid in remaining_stop_ids:
            if sid not in stop_coords.index:
                continue
            slat = stop_coords.loc[sid, "stop_lat"]
            slon = stop_coords.loc[sid, "stop_lon"]
            for idx, row in self.df.iterrows():
                dist = haversine(slat, slon, row["lat"], row["lon"])
                if dist <= radius_m:
                    covered_meshes.add(idx)

        # 到達不能バス停のカバー範囲のうち、残存バス停でカバーされていないメッシュ
        uncovered = lost_meshes - covered_meshes
        total_pop = 0
        total_elderly = 0
        for idx in uncovered:
            total_pop += self.df.loc[idx, "PopT"]
            total_elderly += self.df.loc[idx, "Pop65over"]

        return {
            "affected_total": round(total_pop),
            "affected_elderly": round(total_elderly),
            "uncovered_meshes": len(uncovered),
            "lost_meshes": len(lost_meshes),
            "covered_by_others": len(lost_meshes) - len(uncovered)
        }

class FacilityData:
    def __init__(self, csv_path="facilities.csv"):
        self.df = pd.read_csv(csv_path)
        self.facility_types = sorted(self.df["type"].unique())

    def get_facilities_by_type(self, facility_type):
        return self.df[self.df["type"] == facility_type]

    def find_nearest_stops(self, facilities, stop_coords, max_distance=500):
        """各施設の最寄りバス停を特定し、施設までの徒歩時間を返す"""
        results = []
        for _, fac in facilities.iterrows():
            best_stop = None
            best_dist = float("inf")
            for sid in stop_coords.index:
                dist = haversine(
                    fac["latitude"], fac["longitude"],
                    stop_coords.loc[sid, "stop_lat"],
                    stop_coords.loc[sid, "stop_lon"]
                )
                if dist < best_dist:
                    best_dist = dist
                    best_stop = sid
            if best_dist <= max_distance:
                results.append({
                    "facility_name": fac["name"],
                    "facility_type": fac["type"],
                    "facility_lat": fac["latitude"],
                    "facility_lon": fac["longitude"],
                    "nearest_stop": best_stop,
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