"""GTFSの運行日（calendar.txt）からダイヤ種別を判定する

交通空白は休日にこそ深刻になるが、従来は calendar.txt の monday==1 だけを
見ており平日ダイヤしか扱えなかった。ここで平日/土曜/日祝を切り替えられるようにする。

限界: calendar_dates.txt（特定日の運行追加・運休）は見ていない。
祝日ダイヤは「日祝」の service_id で近似する。特定の日付を指定して
calendar_dates を厳密に適用する対応は将来の課題。
"""

DAY_TYPES = ["平日", "土曜", "日祝"]

_WEEKDAY_COLUMNS = ["monday", "tuesday", "wednesday", "thursday", "friday"]


def service_ids_for_day_type(calendar, day_type):
    """指定ダイヤ種別で運行する service_id の集合を返す"""
    if day_type not in DAY_TYPES:
        raise ValueError(f"未知のダイヤ種別: {day_type}（{DAY_TYPES} のいずれか）")

    if day_type == "平日":
        cols = [c for c in _WEEKDAY_COLUMNS if c in calendar.columns]
        if not cols:
            return set()
        mask = calendar[cols].eq(1).any(axis=1)
    elif day_type == "土曜":
        if "saturday" not in calendar.columns:
            return set()
        mask = calendar["saturday"] == 1
    else:  # 日祝
        if "sunday" not in calendar.columns:
            return set()
        mask = calendar["sunday"] == 1

    return set(calendar.loc[mask, "service_id"])


def trip_ids_for_day_type(trips, calendar, day_type):
    """指定ダイヤ種別で運行する trip_id の集合を返す"""
    services = service_ids_for_day_type(calendar, day_type)
    return set(trips.loc[trips["service_id"].isin(services), "trip_id"])


def available_day_types(trips, calendar):
    """便が1本以上あるダイヤ種別だけを返す（土日運休の地域を弾く）"""
    return [d for d in DAY_TYPES if trip_ids_for_day_type(trips, calendar, d)]


def describe_day_types(trips, calendar):
    parts = []
    for d in DAY_TYPES:
        parts.append(f"{d} {len(trip_ids_for_day_type(trips, calendar, d))}便")
    return "ダイヤ種別ごとの便数: " + " / ".join(parts)
