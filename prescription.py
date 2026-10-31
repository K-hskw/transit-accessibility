"""処方（改善施策）の効果を空白人口で比較する

診断（どこが時間空白か）に対して、処方は「何をすればその空白が埋まるか」を
同じ指標で答える。効果は空白人口の削減数、費用は運行キロの増分（追加便数×
路線長）で代理し、「空白を1人・時間解消するのに必要な運行キロ」で施策を並べる。

効果は到着時刻を5分刻みにして測る。毎時00分の1点だけで測ると、足した便が
00分の直前に着くかどうかで効果が決まってしまい、1便足すと全日で8,847減るのに
2便足すと0になるような逆転が起きていた。
- 「H時台の空白」は H:00〜H:55着の平均（人）
- 「全日の削減」は空白人口の減少を時間で積み上げた値（人・時間）。
  100人が1時間着けなかった状態を解消すると100人・時間になる

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

from blank_area import DAY_HOURS, STEP_MIN


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


DEFAULT_HOURS = DAY_HOURS

# 同じ判定なら空白人口は完全に一致するが、念のため端数の誤差を変化と数えない
_EPS = 1e-6


def blank_by_hour(analyzer, dest_stop_ids, max_time_sec, hours=DEFAULT_HOURS,
                  step_min=STEP_MIN):
    """時間帯ごとの (空白人口の平均, うち高齢者の平均) を、hours と同じ並びの配列で返す"""
    return analyzer.diagnose_day(dest_stop_ids, max_time_sec, hours, step_min).hourly_blank()


def evaluate(engine, analyzer, dest_stop_ids, plan, arrival_hour, max_time_sec,
             hours=DEFAULT_HOURS, baseline_hours=None, baseline_km=None):
    """施策ひとつを評価して1行ぶんの結果を返す

    狙った時間帯だけでなく全時間帯を見る。ダイヤシフトは便を移すだけなので
    狙った時間帯は必ず良くなるが、便を抜いた時間帯は悪化する。単一時刻だけで
    測ると「費用ゼロで改善」に見えてしまい、実態を取り違える。
    """
    hours = list(hours)
    if baseline_hours is None:
        baseline_hours = blank_by_hour(analyzer, dest_stop_ids, max_time_sec, hours)
    if baseline_km is None:
        baseline_km = operating_km(engine, engine.bus_edges)

    bus_after, walk_after = plan.build(engine)
    added_trips = bus_after["trip_id"].nunique() - engine.bus_edges["trip_id"].nunique()
    with engine.scenario(bus_after, walk_after):
        after_hours = blank_by_hour(analyzer, dest_stop_ids, max_time_sec, hours)
        km_after = operating_km(engine, engine.bus_edges)

    (base_pop, base_eld), (after_pop, after_eld) = baseline_hours, after_hours
    # 各時間帯の平均を1時間ぶんの値として足すと、5分刻みの積み上げ（人・時間）になる
    diff = after_pop - base_pop
    worse = diff[diff > _EPS]
    hi = hours.index(arrival_hour)
    reduced_day = round(float((base_pop - after_pop).sum()))
    extra_km = km_after - baseline_km
    # 移す便が時間帯に1本も無いシフトなどは、ネットワークがそのまま返る。
    # 0が並ぶ行だけだと「効果が無い施策」と読まれるので、変えられなかったことを書く
    unchanged = bus_after is engine.bus_edges and walk_after is engine.walk_edges
    return {
        "施策": plan.name + ("（対象の便が無く変更なし）" if unchanged else ""),
        f"{arrival_hour}時台の空白": round(float(after_pop[hi])),
        f"{arrival_hour}時台の削減": round(float(base_pop[hi] - after_pop[hi])),
        "全日の削減": reduced_day,
        "全日うち高齢者": round(float((base_eld - after_eld).sum())),
        "悪化した時間帯": int(len(worse)),
        "最大悪化": round(float(worse.max())) if len(worse) else 0,
        "追加便数": added_trips,
        "運行キロ増": round(extra_km, 1),
        # 全日で悪化する施策に費用対効果を出すと誤解を招くので、純改善のときだけ出す
        "1人・時間あたり運行キロ": (round(extra_km / reduced_day, 3)
                                    if reduced_day > 0 and extra_km > 0 else None),
    }


def compare(engine, analyzer, dest_stop_ids, plans, arrival_hour, max_time_sec,
            hours=DEFAULT_HOURS):
    """複数の施策を同じ条件で比較する表を返す（全日の削減が大きい順）"""
    hours = list(hours)
    baseline_hours = blank_by_hour(analyzer, dest_stop_ids, max_time_sec, hours)
    baseline_km = operating_km(engine, engine.bus_edges)

    rows = [{
        "施策": "現状（何もしない）",
        f"{arrival_hour}時台の空白": round(float(baseline_hours[0][hours.index(arrival_hour)])),
        f"{arrival_hour}時台の削減": 0,
        "全日の削減": 0,
        "全日うち高齢者": 0,
        "悪化した時間帯": 0,
        "最大悪化": 0,
        "追加便数": 0,
        "運行キロ増": 0.0,
        "1人・時間あたり運行キロ": None,
    }]
    for plan in plans:
        rows.append(evaluate(engine, analyzer, dest_stop_ids, plan, arrival_hour,
                             max_time_sec, hours, baseline_hours, baseline_km))

    df = pd.DataFrame(rows)
    head, rest = df.iloc[:1], df.iloc[1:].sort_values("全日の削減", ascending=False)
    return pd.concat([head, rest], ignore_index=True)
