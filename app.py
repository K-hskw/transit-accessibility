import streamlit as st
import folium
from streamlit_folium import st_folium
from transit_engine import TransitEngine
import streamlit as st
import pandas as pd
import folium
from streamlit_folium import st_folium
from transit_engine import TransitEngine

st.set_page_config(page_title="公共交通アクセシビリティ分析", layout="wide")
st.title("公共交通アクセシビリティ分析ツール")
st.caption("室蘭市 道南バス GTFSデータに基づくシミュレーション")

@st.cache_resource
def load_engine():
    return TransitEngine()

engine = load_engine()

if "result_map" not in st.session_state:
    st.session_state.result_map = None
if "result_stats" not in st.session_state:
    st.session_state.result_stats = None

# ===== サイドバー =====
st.sidebar.header("設定")

stop_names = engine.get_stop_names()
start_stop_name = st.sidebar.selectbox(
    "出発地点", stop_names,
    index=stop_names.index("室蘭駅前") if "室蘭駅前" in stop_names else 0
)
start_stop_ids = engine.get_stop_ids_by_name(start_stop_name)
start_stop_id = start_stop_ids[0]

start_hour = st.sidebar.slider("出発時刻（時）", 5, 22, 8)
start_minute = st.sidebar.slider("出発時刻（分）", 0, 55, 0, step=5)
start_time_sec = start_hour * 3600 + start_minute * 60

max_time_min = st.sidebar.select_slider("制限時間（分）", options=[15, 30, 45, 60, 90], value=60)
max_time_sec = max_time_min * 60

mode = st.sidebar.radio("シミュレーションモード", ["到達圏のみ", "路線廃止", "バス停削除", "減便", "時間帯別到達圏"])

remove_route_id = None
selected_route_name = ""
remove_stop_ids = []
remove_stop_name = ""
reduce_mode = None
reduce_target_route_id = None
reduce_target_route_name = ""
reduce_ratio = 0.5

muroran_routes = engine.get_muroran_routes()
route_options = {r["route_name"]: r["route_id"] for r in muroran_routes}

if mode == "路線廃止":
    direct_routes, transfer_routes = engine.get_routes_grouped_by_access(start_stop_name)
    route_options_grouped = {}
    grouped_labels = []
    for r in direct_routes:
        label = f"[直接] {r['route_name']}"
        route_options_grouped[label] = r["route_id"]
        grouped_labels.append(label)
    for r in transfer_routes:
        label = f"[乗換] {r['route_name']}"
        route_options_grouped[label] = r["route_id"]
        grouped_labels.append(label)
    selected_route_label = st.sidebar.selectbox("廃止する路線", grouped_labels)
    remove_route_id = route_options_grouped[selected_route_label]
    selected_route_name = selected_route_label.split("] ", 1)[1]

elif mode == "バス停削除":
    remove_stop_name = st.sidebar.selectbox("削除するバス停", stop_names)
    remove_stop_ids = engine.get_stop_ids_by_name(remove_stop_name)
    walk_distance = st.sidebar.radio(
        "徒歩圏距離（国交省基準）",
        [300, 500],
        format_func=lambda x: f"{x}m（{'都市部基準' if x == 300 else '地方部基準'}）"
    )
elif mode == "減便":
    reduce_mode = st.sidebar.radio("減便方式", [
        "特定路線の便数を半分にする",
        "特定路線の便を間引く（N本に1本残す）",
        "全路線一律で削減"
    ])

    if reduce_mode == "特定路線の便数を半分にする":
        direct_r, transfer_r = engine.get_routes_grouped_by_access(start_stop_name)
        reduce_labels = {}
        reduce_label_list = []
        for r in direct_r:
            label = f"[直接] {r['route_name']}"
            reduce_labels[label] = r["route_id"]
            reduce_label_list.append(label)
        for r in transfer_r:
            label = f"[乗換] {r['route_name']}"
            reduce_labels[label] = r["route_id"]
            reduce_label_list.append(label)
        selected_reduce_label = st.sidebar.selectbox("対象路線", reduce_label_list)
        reduce_target_route_id = reduce_labels[selected_reduce_label]
        reduce_target_route_name = selected_reduce_label.split("] ", 1)[1]

    elif reduce_mode == "特定路線の便を間引く（N本に1本残す）":
        direct_r, transfer_r = engine.get_routes_grouped_by_access(start_stop_name)
        reduce_labels = {}
        reduce_label_list = []
        for r in direct_r:
            label = f"[直接] {r['route_name']}"
            reduce_labels[label] = r["route_id"]
            reduce_label_list.append(label)
        for r in transfer_r:
            label = f"[乗換] {r['route_name']}"
            reduce_labels[label] = r["route_id"]
            reduce_label_list.append(label)
        selected_reduce_label = st.sidebar.selectbox("対象路線", reduce_label_list)
        reduce_target_route_id = reduce_labels[selected_reduce_label]
        reduce_target_route_name = selected_reduce_label.split("] ", 1)[1]

    elif reduce_mode == "全路線一律で削減":
        reduce_pct = st.sidebar.slider("削減率（%）", 10, 80, 50, step=10)
        reduce_ratio = reduce_pct / 100

threshold_min = st.sidebar.slider("悪化閾値（分）", 1, 15, 1)

# ===== 地図生成ヘルパー =====
def build_popup(stop_id, prev, start_time_sec, engine, prefix="", extra=""):
    if stop_id not in engine.stop_coords.index:
        return ""
    row = engine.stop_coords.loc[stop_id]
    path = engine.reconstruct_path(prev, stop_id)
    popup_text = f"<b>{row['stop_name']}</b><br>{extra}<br>"
    if prefix:
        popup_text += f"<b>{prefix}</b><br>"
    if path:
        for step in path:
            if step["mode"] == "バス":
                popup_text += f"🚌 {step['departure']} {step['from_stop']}<br>"
                popup_text += f"　→ {step['arrival']} {step['to_stop']}<br>"
                popup_text += f"　　<b>{step['route_name']}</b> ({step['duration_min']}分)<br>"
            else:
                popup_text += f"🚶 {step['from_stop']} → {step['to_stop']}<br>"
                popup_text += f"　　(徒歩 {step['duration_min']}分)<br>"
    return popup_text

# ===== 計算実行 =====
if st.sidebar.button("シミュレーション実行", type="primary"):
    with st.spinner("計算中..."):
        result_before, prev_before = engine.calc_isochrone(
            start_stop_id, start_time_sec, max_time_sec, track_path=True
        )

        m = folium.Map(
            location=[42.35, 140.97], zoom_start=13,
            tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
            attr="&copy; OpenStreetMap contributors &copy; CARTO"
        )
        # 路線ルートを描画
        shapes = pd.read_csv("gtfs_data/shapes.txt")
        trips_df = pd.read_csv("gtfs_data/trips.txt")
        trip_shape = trips_df[["route_id", "shape_id"]].drop_duplicates()

        route_colors = [
            "#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4",
            "#42d4f4", "#f032e6", "#bfef45", "#fabed4", "#469990",
            "#dcbeff", "#9A6324", "#800000", "#aaffc3", "#808000",
            "#000075", "#a9a9a9"
        ]

        if mode == "路線廃止":
            target_route = remove_route_id
        elif mode == "減便" and reduce_mode != "全路線一律で削減":
            target_route = reduce_target_route_id
        else:
            target_route = None

        muroran_routes_for_map = engine.get_muroran_routes(exclude_highway=True)
        drawn_shapes = set()
        for idx, route in enumerate(muroran_routes_for_map):
            rid = route["route_id"]
            rname = route["route_name"]
            route_shapes = trip_shape[trip_shape["route_id"] == rid]["shape_id"].unique()
            for shape_id in route_shapes:
                if shape_id in drawn_shapes:
                    continue
                drawn_shapes.add(shape_id)
                shape_pts = shapes[shapes["shape_id"] == shape_id].sort_values("shape_pt_sequence")
                coords = list(zip(shape_pts["shape_pt_lat"], shape_pts["shape_pt_lon"]))
                if len(coords) < 2:
                    continue

                if rid == target_route:
                    color = "red"
                    weight = 5
                    opacity = 0.9
                else:
                    color = route_colors[idx % len(route_colors)]
                    weight = 2
                    opacity = 0.4

                folium.PolyLine(
                    locations=coords, weight=weight, color=color,
                    opacity=opacity, popup=rname
                ).add_to(m)
        stats = {}

        if mode == "到達圏のみ":
            for stop_id, arrival in result_before.items():
                if stop_id not in engine.stop_coords.index:
                    continue
                row = engine.stop_coords.loc[stop_id]
                travel_min = (arrival - start_time_sec) / 60
                if travel_min <= 15:
                    color = "green"
                elif travel_min <= 30:
                    color = "orange"
                elif travel_min <= 45:
                    color = "red"
                else:
                    color = "darkred"
                popup_text = build_popup(stop_id, prev_before, start_time_sec, engine,
                                         extra=f"到達時間: {travel_min:.0f}分")
                folium.CircleMarker(
                    location=[row["stop_lat"], row["stop_lon"]],
                    radius=6, color=color, fill=True, fill_opacity=0.8,
                    popup=folium.Popup(popup_text, max_width=350)
                ).add_to(m)

            stats = {"mode": "到達圏のみ", "reachable": len(result_before)}

        else:
            # シミュレーション実行
            if mode == "路線廃止":
                result_after, prev_after = engine.simulate_route_removal(
                    start_stop_id, start_time_sec, max_time_sec, remove_route_id, track_path=True
                )
                sim_label = f"路線廃止: {selected_route_name}"

            elif mode == "バス停削除":
                result_after, prev_after = engine.simulate_stop_removal(
                    start_stop_id, start_time_sec, max_time_sec, remove_stop_ids,
                    walk_distance=walk_distance, track_path=True
                )
                sim_label = f"バス停削除: {remove_stop_name}（徒歩圏{walk_distance}m）"

            elif mode == "減便":
                if reduce_mode == "特定路線の便数を半分にする":
                    result_after, prev_after = engine.simulate_frequency_reduction(
                        start_stop_id, start_time_sec, max_time_sec,
                        "half", target_route_id=reduce_target_route_id, track_path=True
                    )
                    sim_label = f"減便（半減）: {reduce_target_route_name}"

                elif reduce_mode == "特定路線の便を間引く（N本に1本残す）":
                    result_after, prev_after = engine.simulate_frequency_reduction(
                        start_stop_id, start_time_sec, max_time_sec,
                        "interval", target_route_id=reduce_target_route_id,
                        reduce_ratio=reduce_ratio, track_path=True
                    )
                    sim_label = f"減便（{int(reduce_ratio)}本に1本）: {reduce_target_route_name}"

                else:
                    result_after, prev_after = engine.simulate_frequency_reduction(
                        start_stop_id, start_time_sec, max_time_sec,
                        "all", reduce_ratio=reduce_ratio, track_path=True
                    )
                    sim_label = f"全路線一律 {int(reduce_ratio*100)}%削減"

            lost, degraded = engine.compare_results(
                result_before, result_after, start_time_sec, threshold_min, remove_stop_ids
            )

            # 影響なし
            for stop_id in result_before:
                if stop_id not in engine.stop_coords.index:
                    continue
                if stop_id in lost or stop_id in degraded or stop_id in remove_stop_ids:
                    continue
                row = engine.stop_coords.loc[stop_id]
                popup_text = build_popup(stop_id, prev_before, start_time_sec, engine,
                                         prefix="現行経路:", extra="影響なし")
                folium.CircleMarker(
                    location=[row["stop_lat"], row["stop_lon"]],
                    radius=5, color="gray", fill=True, fill_opacity=0.5,
                    popup=folium.Popup(popup_text, max_width=350)
                ).add_to(m)

            # 悪化
            # 悪化
            for stop_id, diff in degraded.items():
                if stop_id not in engine.stop_coords.index:
                    continue
                row = engine.stop_coords.loc[stop_id]
                before_min = (result_before[stop_id] - start_time_sec) / 60
                after_min = (result_after[stop_id] - start_time_sec) / 60

                # 廃止前後の経路から使用路線を抽出
                path_before = engine.reconstruct_path(prev_before, stop_id)
                path_after = engine.reconstruct_path(prev_after, stop_id)

                routes_before = []
                if path_before:
                    for step in path_before:
                        if step["mode"] == "バス" and step["route_name"] not in routes_before:
                            routes_before.append(step["route_name"])

                routes_after = []
                if path_after:
                    for step in path_after:
                        if step["mode"] == "バス" and step["route_name"] not in routes_after:
                            routes_after.append(step["route_name"])

                popup_text = f"<b>{row['stop_name']}</b><br>"
                popup_text += f"⚠️ +{diff:.0f}分悪化 ({before_min:.0f}分→{after_min:.0f}分)<br><br>"

                popup_text += "<b>【変更前】</b><br>"
                popup_text += f"使用路線: {', '.join(routes_before)}<br>"
                popup_text += build_popup(stop_id, prev_before, start_time_sec, engine)
                popup_text += "<br>"

                popup_text += "<b>【変更後（代替路線）】</b><br>"
                popup_text += f"使用路線: {', '.join(routes_after)}<br>"
                popup_text += build_popup(stop_id, prev_after, start_time_sec, engine)

                folium.CircleMarker(
                    location=[row["stop_lat"], row["stop_lon"]],
                    radius=7, color="orange", fill=True, fill_opacity=0.8,
                    popup=folium.Popup(popup_text, max_width=400)
                ).add_to(m)

            # 到達不能
            for stop_id in lost:
                if stop_id not in engine.stop_coords.index:
                    continue
                row = engine.stop_coords.loc[stop_id]
                before_min = (result_before[stop_id] - start_time_sec) / 60
                popup_text = f"<b>{row['stop_name']}</b><br>"
                popup_text += f"❌ 到達不能（変更前: {before_min:.0f}分）<br><br>"
                popup_text += build_popup(stop_id, prev_before, start_time_sec, engine, prefix="【変更前の経路】")

                folium.CircleMarker(
                    location=[row["stop_lat"], row["stop_lon"]],
                    radius=8, color="red", fill=True, fill_opacity=0.9,
                    popup=folium.Popup(popup_text, max_width=400)
                ).add_to(m)

            # 削除バス停（バス停削除モード時のみ）
            for sid in remove_stop_ids:
                if sid not in engine.stop_coords.index:
                    continue
                row = engine.stop_coords.loc[sid]
                folium.Marker(
                    location=[row["stop_lat"], row["stop_lon"]],
                    popup=f"削除: {row['stop_name']}",
                    icon=folium.Icon(color="black", icon="remove", prefix="glyphicon")
                ).add_to(m)

            stats = {
                "mode": "simulation",
                "sim_label": sim_label,
                "before": len(result_before),
                "after": len(result_after),
                "lost": lost,
                "degraded": degraded
            }

        # 出発地点
        start_row = engine.stop_coords.loc[start_stop_id]
        folium.Marker(
            location=[start_row["stop_lat"], start_row["stop_lon"]],
            popup=f"出発: {start_row['stop_name']}",
            icon=folium.Icon(color="blue", icon="star")
        ).add_to(m)

        st.session_state.result_map = m
        st.session_state.result_stats = stats

# ===== 時間帯別到達圏モード =====
if mode == "時間帯別到達圏":
    if "animation_results" not in st.session_state:
        st.session_state.animation_results = None
    if "animation_hour" not in st.session_state:
        st.session_state.animation_hour = 8

    if st.sidebar.button("全時間帯を一括計算", type="primary"):
        with st.spinner("全時間帯を計算中（数分かかります）..."):
            results = {}
            for hour in range(5, 23):
                t = hour * 3600
                result, _ = engine.calc_isochrone(start_stop_id, t, max_time_sec, track_path=True)
                results[hour] = result
            st.session_state.animation_results = results
            st.session_state.animation_hour = 8

    if st.session_state.animation_results is not None:
        results = st.session_state.animation_results
        selected_hour = st.slider("時間帯", 5, 22, st.session_state.animation_hour)
        st.session_state.animation_hour = selected_hour

        result = results[selected_hour]

        m = folium.Map(
            location=[42.35, 140.97], zoom_start=13,
            tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
            attr="&copy; OpenStreetMap contributors &copy; CARTO"
        )

        for stop_id, arrival in result.items():
            if stop_id not in engine.stop_coords.index:
                continue
            row = engine.stop_coords.loc[stop_id]
            travel_min = (arrival - selected_hour * 3600) / 60
            if travel_min <= 15:
                color = "green"
            elif travel_min <= 30:
                color = "orange"
            elif travel_min <= 45:
                color = "red"
            else:
                color = "darkred"
            folium.CircleMarker(
                location=[row["stop_lat"], row["stop_lon"]],
                radius=6, color=color, fill=True, fill_opacity=0.8,
                popup=f"{row['stop_name']}<br>{travel_min:.0f}分"
            ).add_to(m)

        start_row = engine.stop_coords.loc[start_stop_id]
        folium.Marker(
            location=[start_row["stop_lat"], start_row["stop_lon"]],
            popup=f"出発: {start_row['stop_name']}",
            icon=folium.Icon(color="blue", icon="star")
        ).add_to(m)

        st.subheader(f"{selected_hour}:00 出発 — 到達可能バス停: {len(result)}箇所")
        st_folium(m, width=1200, height=700, returned_objects=[])

        st.markdown("**時間帯別の到達可能数**")
        hours = list(range(5, 23))
        chart_data = pd.DataFrame({
            "時刻": [f"{h:02d}:00" for h in hours],
            "到達可能数": [len(results[h]) for h in hours]
        }).set_index("時刻")
        st.bar_chart(chart_data)

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**凡例**")
            st.markdown("🟢 0-15分　🟠 15-30分　🔴 30-45分　🟤 45-60分")
# ===== 結果表示 =====
if st.session_state.result_map is not None:
    col1, col2 = st.columns([3, 1])

    with col1:
        st_folium(st.session_state.result_map, width=900, height=600, returned_objects=[])

    with col2:
        stats = st.session_state.result_stats

        if stats["mode"] == "到達圏のみ":
            st.metric("到達可能バス停数", f"{stats['reachable']}箇所")
            st.markdown("**凡例**")
            st.markdown("🟢 0-15分")
            st.markdown("🟠 15-30分")
            st.markdown("🔴 30-45分")
            st.markdown("🟤 45-60分")
            st.info("バス停をクリック→乗り継ぎ経路表示")
        else:
            st.subheader("影響分析")
            st.markdown(f"**{stats['sim_label']}**")
            st.metric("変更前", f"{stats['before']}箇所")
            st.metric("変更後", f"{stats['after']}箇所")
            st.metric("到達不能", f"{len(stats['lost'])}箇所",
                      delta=f"-{len(stats['lost'])}", delta_color="inverse")
            st.metric("到達時間悪化", f"{len(stats['degraded'])}箇所")

            st.markdown("**凡例**")
            st.markdown("⚫ 影響なし")
            st.markdown("🟠 到達時間悪化")
            st.markdown("🔴 到達不能")
            st.info("バス停をクリック→経路比較表示")

            if stats["lost"]:
                with st.expander("到達不能バス停"):
                    for sid in stats["lost"]:
                        if sid in engine.stop_coords.index:
                            st.markdown(f"- {engine.stop_coords.loc[sid, 'stop_name']}")

            if stats["degraded"]:
                with st.expander("到達時間悪化（上位10件）"):
                    sorted_d = sorted(stats["degraded"].items(), key=lambda x: -x[1])[:10]
                    for sid, diff in sorted_d:
                        if sid in engine.stop_coords.index:
                            st.markdown(f"- {engine.stop_coords.loc[sid, 'stop_name']}: +{diff:.0f}分")