"""処方（改善施策）の効果を空白人口で比較する

診断（どこが時間空白か）に対して、処方は「何をすればその空白が埋まるか」を
同じ指標で答える。効果は空白人口の削減数、費用は運行キロの増分（追加便数×
路線長）で代理し、「空白を1人解消するのに必要な運行キロ」で施策を並べる。

使い方:
    plans = [
        增便 := Plan("休日朝 03線を増便", lambda e: e.edges_after_frequency_increase("鉄北北口線_A", 6, 9)),
        Plan("同 時間帯シフト", lambda e: e.edges_after_timetable_shift("鉄北北口線_A", (10, 14), (6, 9), 2)),
    ]
    table = compare(engine, analyzer, dest_ids, plans, arrival_hour=7, max_time_sec=3600)
"""
from dataclasses import dataclass
from typing import Callable

import pandas as pd


@dataclass
class Plan:
    """施策ひとつ。build は engine を受け取り (bus_edges, walk_edges) を返す。"""
    name: str
    build: Callable


def blank_area_stops(analyzer, diagnosis):
    """空白メッシュの徒歩圏にある停留所IDの集合を返す

    analyzer が診断時に使っているメッシュ×停留所の徒歩対応表をそのまま使うので、
    距離計算をやり直さなくてよい。静的空白（徒歩圏に停留所が無い）のメッシュは
    そもそも対応表に現れないため、自然に除外される。
    """
    blank_mesh = (~diagnosis["reachable"]).to_numpy()
    hit = blank_mesh[analyzer.pair_mesh]
    return set(analyzer.stop_ids[analyzer.pair_stop[hit]])


def rank_routes_by_blank_coverage(engine, analyzer, diagnosis, exclude_highway=True):
    """空白地域をどれだけ通るかで路線を順位づけする（処方の候補選び）

    便数の多い路線を選ぶと外れる。室蘭では最多便の鉄北北口線は129停留所を
    通るが空白地域は0箇所で、増便しても空白人口は1人も減らない。実際に効くのは
    空白地域を通る便数の少ないローカル線のほうなので、ここで候補を絞る。
    """
    targets = blank_area_stops(analyzer, diagnosis)
    rows = []
    for r in engine.get_muroran_routes(exclude_highway=exclude_highway):
        edges = engine.bus_edges[engine.bus_edges["route_id"] == r["route_id"]]
        if edges.empty:
            continue
        stops = set(edges["from_stop"]) | set(edges["to_stop"])
        covered = stops & targets
        if not covered:
            continue
        rows.append({
            "route_id": r["route_id"],
            "route_name": r["route_name"],
            "空白地域の停留所": len(covered),
            "全停留所": len(stops),
            "便数": edges["trip_id"].nunique(),
            "路線長km": round(engine.route_length_km(r["route_id"]), 1),
        })
    return (pd.DataFrame(rows).sort_values("空白地域の停留所", ascending=False)
            .reset_index(drop=True))


def operating_km(engine, bus_edges):
    """総運行キロの代理値 = Σ(路線の便数 × 路線長)

    路線長は停留所間の直線距離の和。絶対値としては実距離より短めに出るが、
    施策どうしの相対比較にはこれで足りる。
    """
    per_route = bus_edges.groupby("route_id")["trip_id"].nunique()
    total = 0.0
    for route_id, trips in per_route.items():
        total += trips * engine.route_length_km(route_id)
    return total


DEFAULT_HOURS = tuple(range(5, 23))


def blank_by_hour(analyzer, dest_stop_ids, max_time_sec, hours=DEFAULT_HOURS):
    """時間帯ごとの (空白人口, うち高齢者) を返す"""
    out = {}
    for h in hours:
        s = analyzer.summarize(analyzer.diagnose(dest_stop_ids, h * 3600, max_time_sec))
        out[h] = (s["blank_pop"], s["blank_elderly"])
    return out


def evaluate(engine, analyzer, dest_stop_ids, plan, arrival_hour, max_time_sec,
             hours=DEFAULT_HOURS, baseline_hours=None, baseline_km=None):
    """施策ひとつを評価して1行ぶんの結果を返す

    狙った時間帯だけでなく全時間帯を見る。ダイヤシフトは便を移すだけなので
    狙った時間帯は必ず良くなるが、便を抜いた時間帯は悪化する。単一時刻だけで
    測ると「費用ゼロで改善」に見えてしまい、実態を取り違える。
    """
    if baseline_hours is None:
        baseline_hours = blank_by_hour(analyzer, dest_stop_ids, max_time_sec, hours)
    if baseline_km is None:
        baseline_km = operating_km(engine, engine.bus_edges)

    bus_after, walk_after = plan.build(engine)
    added_trips = bus_after["trip_id"].nunique() - engine.bus_edges["trip_id"].nunique()
    with engine.scenario(bus_after, walk_after):
        after_hours = blank_by_hour(analyzer, dest_stop_ids, max_time_sec, hours)
        km_after = operating_km(engine, engine.bus_edges)

    worsened = [after_hours[h][0] - baseline_hours[h][0] for h in hours
                if after_hours[h][0] > baseline_hours[h][0]]
    reduced_target = baseline_hours[arrival_hour][0] - after_hours[arrival_hour][0]
    reduced_day = sum(v[0] for v in baseline_hours.values()) - sum(v[0] for v in after_hours.values())
    reduced_elderly = sum(v[1] for v in baseline_hours.values()) - sum(v[1] for v in after_hours.values())
    extra_km = km_after - baseline_km
    return {
        "施策": plan.name,
        f"{arrival_hour}時の空白": after_hours[arrival_hour][0],
        f"{arrival_hour}時の削減": reduced_target,
        "全日の削減": reduced_day,
        "全日うち高齢者": reduced_elderly,
        "悪化した時間帯": len(worsened),
        "最大悪化": max(worsened) if worsened else 0,
        "追加便数": added_trips,
        "運行キロ増": round(extra_km, 1),
        # 全日で悪化する施策に費用対効果を出すと誤解を招くので、純改善のときだけ出す
        "1人あたり運行キロ": (round(extra_km / reduced_day, 3)
                              if reduced_day > 0 and extra_km > 0 else None),
    }


def compare(engine, analyzer, dest_stop_ids, plans, arrival_hour, max_time_sec,
            hours=DEFAULT_HOURS):
    """複数の施策を同じ条件で比較する表を返す（全日の削減が大きい順）"""
    baseline_hours = blank_by_hour(analyzer, dest_stop_ids, max_time_sec, hours)
    baseline_km = operating_km(engine, engine.bus_edges)

    rows = [{
        "施策": "現状（何もしない）",
        f"{arrival_hour}時の空白": baseline_hours[arrival_hour][0],
        f"{arrival_hour}時の削減": 0,
        "全日の削減": 0,
        "全日うち高齢者": 0,
        "悪化した時間帯": 0,
        "最大悪化": 0,
        "追加便数": 0,
        "運行キロ増": 0.0,
        "1人あたり運行キロ": None,
    }]
    for plan in plans:
        rows.append(evaluate(engine, analyzer, dest_stop_ids, plan, arrival_hour,
                             max_time_sec, hours, baseline_hours, baseline_km))

    df = pd.DataFrame(rows)
    head, rest = df.iloc[:1], df.iloc[1:].sort_values("全日の削減", ascending=False)
    return pd.concat([head, rest], ignore_index=True)
