import streamlit as st
import numpy as np
import pandas as pd
import folium
import os
import zipfile
import tempfile
import shutil
import pydeck as pdk
from streamlit_folium import st_folium
from transit_engine import TransitEngine
from population import PopulationData, FacilityData
from build_network import build_network
from blank_area import BlankAreaAnalyzer, aggregate_to_500m
from prescription import Plan, compare, rank_routes_by_blank_coverage

st.set_page_config(page_title="公共交通アクセシビリティ分析", layout="wide")
st.title("公共交通アクセシビリティ分析ツール")

# ===== データ管理 =====
DATA_DIR = "."
GTFS_DIR_DEFAULT = "gtfs_data"        # デフォルト（室蘭）
GTFS_DIR_CUSTOM = "gtfs_data_custom"  # アップロード用

# セッション状態の初期化
if "data_source" not in st.session_state:
    st.session_state.data_source = "default"
if "engine" not in st.session_state:
    st.session_state.engine = None
if "pop_data" not in st.session_state:
    st.session_state.pop_data = None
if "facility_data" not in st.session_state:
    st.session_state.facility_data = None

# --- デフォルトデータの読み込み ---
@st.cache_resource
def load_engine():
    return TransitEngine(gtfs_dir=GTFS_DIR_DEFAULT)

@st.cache_resource
def load_custom_engine(gtfs_dir):
    return TransitEngine(gtfs_dir=gtfs_dir)

@st.cache_resource
def load_population():
    if os.path.exists("100m_mesh_pop2020_01205室蘭市.csv"):
        return PopulationData("100m_mesh_pop2020_01205室蘭市.csv")
    return None

@st.cache_resource
def load_facilities():
    if os.path.exists("facilities.csv"):
        return FacilityData("facilities.csv")
    return None

@st.cache_resource
def load_blank_analyzer(_engine, _pop_data, max_walk_m, walk_speed):
    # メッシュ×バス停の徒歩対応表は徒歩条件ごとに1度だけ作れば足りる
    return BlankAreaAnalyzer(_engine, _pop_data,
                             max_walk_m=max_walk_m, walk_speed_m_min=walk_speed)

# デフォルトデータをロード
if st.session_state.engine is None:
    if os.path.exists(GTFS_DIR_CUSTOM) and os.path.exists("bus_edges_custom.csv"):
        st.session_state.engine = load_custom_engine(GTFS_DIR_CUSTOM)
    else:
        st.session_state.engine = load_engine()
if st.session_state.pop_data is None:
    st.session_state.pop_data = load_population()
if st.session_state.facility_data is None:
    st.session_state.facility_data = load_facilities()

engine = st.session_state.engine
pop_data = st.session_state.pop_data
facility_data = st.session_state.facility_data

# --- サイドバー: データアップロード ---
with st.sidebar.expander("📂 データ設定", expanded=False):
    st.markdown("**施設データ（CSV）**")
    st.caption("カラム: name, type, latitude, longitude")
    st.caption("[国土数値情報 医療機関](https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-P04-v3_0.html)")
    uploaded_facilities = st.file_uploader("施設CSV", type=["csv"], key="fac_upload")
    if uploaded_facilities is not None:
        try:
            fac_df = pd.read_csv(uploaded_facilities)
            required = {"name", "type", "latitude", "longitude"}
            if required.issubset(set(fac_df.columns)):
                fac_df.to_csv("uploaded_facilities.csv", index=False)
                st.session_state.facility_data = FacilityData("uploaded_facilities.csv")
                facility_data = st.session_state.facility_data
                st.success(f"施設データ読み込み完了: {len(fac_df)}件")
            else:
                st.error(f"必須カラムが不足: {required - set(fac_df.columns)}")
        except Exception as e:
            st.error(f"読み込みエラー: {e}")

    st.markdown("**人口データ（CSV）**")
    st.caption("カラム: Meshcode, PopT, Pop65over 等")
    st.caption("[簡易100mメッシュ人口](https://gtfs-gis.jp/teikyo/)")
    uploaded_pop = st.file_uploader("人口CSV", type=["csv"], key="pop_upload")
    if uploaded_pop is not None:
        try:
            pop_df = pd.read_csv(uploaded_pop)
            if "Meshcode" in pop_df.columns and "PopT" in pop_df.columns:
                pop_df.to_csv("uploaded_population.csv", index=False)
                st.session_state.pop_data = PopulationData("uploaded_population.csv")
                pop_data = st.session_state.pop_data
                st.success(f"人口データ読み込み完了: {len(pop_df)}メッシュ")
            else:
                st.error("必須カラム（Meshcode, PopT）が不足")
        except Exception as e:
            st.error(f"読み込みエラー: {e}")

    st.markdown("**GTFSデータ（ZIP）**")
    st.caption("stops.txt, stop_times.txt 等を含むzip")
    st.caption("[GTFS公開データ一覧](https://ckan.hoda.jp/dataset/gtfs-data) / [その他地域](https://gtfs-data.jp/)")
    uploaded_gtfs = st.file_uploader("GTFS ZIP", type=["zip"], key="gtfs_upload")
    if uploaded_gtfs is not None:
        if st.button("GTFSデータを適用（数分かかります）"):
            with st.spinner("GTFSデータを展開・ネットワーク構築中..."):
                try:
                    # zipを展開
                    tmp_dir = "uploaded_gtfs"
                    if os.path.exists(tmp_dir):
                        shutil.rmtree(tmp_dir)
                    os.makedirs(tmp_dir, exist_ok=True)
                    with zipfile.ZipFile(uploaded_gtfs, "r") as z:
                        z.extractall(tmp_dir)

                    # GTFSファイルを探す（サブフォルダ対応）
                    gtfs_path = tmp_dir
                    for root, dirs, files in os.walk(tmp_dir):
                        if "stops.txt" in files and "stop_times.txt" in files:
                            gtfs_path = root
                            break

                    # カスタムGTFSフォルダに展開（デフォルトを上書きしない）
                    os.makedirs(GTFS_DIR_CUSTOM, exist_ok=True)
                    for fname in ["stops.txt", "stop_times.txt", "routes.txt",
                                  "trips.txt", "calendar.txt", "shapes.txt",
                                  "agency.txt", "feed_info.txt"]:
                        src = os.path.join(gtfs_path, fname)
                        dst = os.path.join(GTFS_DIR_CUSTOM, fname)
                        if os.path.exists(src):
                            shutil.copy2(src, dst)

                    # カスタムエッジファイルを生成
                    build_network(gtfs_path, DATA_DIR)
                    shutil.copy2("bus_edges.csv", "bus_edges_custom.csv")
                    shutil.copy2("walk_edges.csv", "walk_edges_custom.csv")
                    # デフォルトエッジに戻す
                    shutil.copy2("bus_edges_default.csv", "bus_edges.csv")
                    shutil.copy2("walk_edges_default.csv", "walk_edges.csv")

                    # エンジン再読み込み（カスタム）
                    st.cache_resource.clear()
                    st.session_state.engine = TransitEngine(gtfs_dir=GTFS_DIR_CUSTOM)
                    engine = st.session_state.engine
                    st.success("GTFSデータ適用完了！ネットワークを再構築しました。")
                    st.rerun()
                except Exception as e:
                    st.error(f"GTFSデータ適用エラー: {e}")

    # デフォルトデータに戻すボタン
    if os.path.exists(GTFS_DIR_CUSTOM):
        st.divider()
        if st.button("🔄 デフォルト（室蘭）に戻す"):
            if os.path.exists(GTFS_DIR_CUSTOM):
                shutil.rmtree(GTFS_DIR_CUSTOM)
            if os.path.exists("bus_edges_custom.csv"):
                os.remove("bus_edges_custom.csv")
            if os.path.exists("walk_edges_custom.csv"):
                os.remove("walk_edges_custom.csv")
            st.cache_resource.clear()
            st.session_state.engine = load_engine()
            engine = st.session_state.engine
            st.success("デフォルトデータに戻しました")
            st.rerun()

# データソース表示
if os.path.exists(GTFS_DIR_CUSTOM):
    area_name = "カスタムデータ"
else:
    area_name = "室蘭市 道南バス"
st.caption(f"{area_name} GTFSデータに基づくシミュレーション")

if "result_map" not in st.session_state:
    st.session_state.result_map = None
if "result_stats" not in st.session_state:
    st.session_state.result_stats = None

# ===== サイドバー =====
# ===== サイドバー =====
st.sidebar.header("設定")

# ダイヤ種別。交通空白は休日にこそ深刻になるため平日固定にしない。
day_type = st.sidebar.radio("ダイヤ", engine.available_day_types, horizontal=True)
engine.set_day_type(day_type)
st.sidebar.caption(f"{day_type}ダイヤ: {engine.bus_edges['trip_id'].nunique()}便 / "
                   f"{len(engine.bus_edges):,}エッジ")

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

mode = st.sidebar.radio("シミュレーションモード", ["到達圏のみ", "路線廃止", "バス停削除", "減便", "時間帯別到達圏", "施設アクセス", "代替路線追加", "集客圏分析", "時間空白診断（3D）", "処方（改善施策）"])
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
    selected_route_labels = st.sidebar.multiselect("廃止する路線（複数選択可）", grouped_labels)
    if selected_route_labels:
        remove_route_id = [route_options_grouped[label] for label in selected_route_labels]
        selected_route_name = ", ".join([label.split("] ", 1)[1] for label in selected_route_labels])
    else:
        remove_route_id = []
        selected_route_name = ""

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

if mode == "代替路線追加":
    st.sidebar.markdown("**廃止する既存路線（任意）**")
    direct_routes, transfer_routes = engine.get_routes_grouped_by_access(start_stop_name)
    route_options_alt = {}
    grouped_labels_alt = [""]
    for r in direct_routes:
        label = f"[直接] {r['route_name']}"
        route_options_alt[label] = r["route_id"]
        grouped_labels_alt.append(label)
    for r in transfer_routes:
        label = f"[乗換] {r['route_name']}"
        route_options_alt[label] = r["route_id"]
        grouped_labels_alt.append(label)
    alt_remove_label = st.sidebar.selectbox("廃止路線（なしでもOK）", grouped_labels_alt)
    alt_remove_route_id = route_options_alt.get(alt_remove_label, None) if alt_remove_label else None

    st.sidebar.markdown("**新規ルートのバス停（2〜8個）**")
    alt_new_stops_names = st.sidebar.multiselect(
        "新ルートが結ぶバス停（順番通り）", stop_names,
        default=[]
    )
    alt_interval = st.sidebar.slider("運行間隔（分）", 10, 120, 30, step=10)
    alt_speed = st.sidebar.slider("平均速度（km/h）", 15, 60, 25, step=5)

elif mode in ("時間空白診断（3D）", "処方（改善施策）"):
    # 空白の定義（拠点・徒歩条件）は診断と処方で揃える必要があるので共通化する
    st.sidebar.markdown("**生活拠点（ここに着けるかで空白を判定）**")
    blank_dest_names = st.sidebar.multiselect(
        "拠点バス停（複数可）", stop_names,
        default=[n for n in ["東室蘭駅東口"] if n in stop_names]
    )
    blank_dest_ids = []
    for n in blank_dest_names:
        blank_dest_ids.extend(engine.get_stop_ids_by_name(n))

    blank_walk_m = st.sidebar.slider("メッシュから乗車できる徒歩距離（m）", 200, 1000, 500, step=100)
    blank_walk_speed = st.sidebar.radio(
        "徒歩速度", [67, 40],
        format_func=lambda v: "一般 67m/分" if v == 67 else "高齢者 40m/分"
    )

    if mode == "時間空白診断（3D）":
        blank_mesh_level = st.sidebar.radio(
            "メッシュ粒度", [500, 100],
            format_func=lambda v: f"{v}mメッシュ"
        )
        st.sidebar.caption("100mメッシュは1kmの10等分のため250mには割り切れない。標準メッシュで作れるのは500m（5×5セル）。")
        blank_elev_scale = st.sidebar.slider("柱の高さ倍率", 1, 20, 6)
    else:
        st.sidebar.markdown("**対象とする時刻**")
        presc_hour = st.sidebar.slider("この時刻までに拠点へ着けるか（時）", 5, 22, 7)
        st.sidebar.caption("休日の朝など、空白が大きい時間帯を選ぶと施策の差が出やすい。")

        st.sidebar.markdown("**増便**")
        inc_window = st.sidebar.slider("増便する時間帯", 5, 22, (5, 9))
        inc_trips = st.sidebar.slider("パターンごとの追加便数", 1, 4, 1)
        st.sidebar.caption("方向別に足すため、往復2パターンなら実際の追加は2倍になる。")

        st.sidebar.markdown("**ダイヤシフト（増便せず時間帯を移す）**")
        shift_from = st.sidebar.slider("便を減らす時間帯", 5, 22, (10, 15))
        shift_trips = st.sidebar.slider("パターンごとの移動便数", 1, 4, 2)
        presc_n_routes = st.sidebar.slider("比較する候補路線数", 1, 6, 3)

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
            if isinstance(remove_route_id, list):
                target_route = remove_route_id
            else:
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

                if (isinstance(target_route, list) and rid in target_route) or rid == target_route:
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
                if not remove_route_id:
                    st.warning("廃止する路線を選択してください")
                    st.stop()
                result_after, prev_after = engine.simulate_route_removal(
                    start_stop_id, start_time_sec, max_time_sec, remove_route_id, track_path=True
                )
                if len(remove_route_id) == 1:
                    sim_label = f"路線廃止: {selected_route_name}"
                else:
                    sim_label = f"{len(remove_route_id)}路線同時廃止"

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

            elif mode == "代替路線追加":
                alt_new_stop_ids = []
                for name in alt_new_stops_names:
                    ids = engine.get_stop_ids_by_name(name)
                    if ids:
                        alt_new_stop_ids.append(ids[0])

                if len(alt_new_stop_ids) < 2:
                    st.warning("新ルートのバス停を2つ以上選択してください")
                    st.stop()

                result_after, prev_after = engine.simulate_route_replacement(
                    start_stop_id, start_time_sec, max_time_sec,
                    alt_remove_route_id, alt_new_stop_ids,
                    interval_min=alt_interval, speed_kmh=alt_speed, track_path=True
                )
                sim_label = f"代替路線追加（{len(alt_new_stop_ids)}箇所経由, {alt_interval}分間隔）"
                remove_stop_ids = []

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

            # 人口影響を算出
            pop_impact = pop_data.calc_impact_population(
                engine.stop_coords,
                list(lost),
                list(result_before.keys()),
                radius_m=300
            )

            # 廃止前後のカバー人口
            pop_before = pop_data.get_population_near_stops(
                engine.stop_coords, list(result_before.keys()), radius_m=300
            )
            pop_after = pop_data.get_population_near_stops(
                engine.stop_coords, list(result_after.keys()), radius_m=300
            )

            stats = {
                "mode": "simulation",
                "sim_label": sim_label,
                "before": len(result_before),
                "after": len(result_after),
                "lost": lost,
                "degraded": degraded,
                "pop_impact": pop_impact,
                "pop_before": pop_before,
                "pop_after": pop_after,
                "result_before": result_before,
                "result_after": result_after,
                "start_time_sec": start_time_sec
            }

        # 出発地点
        start_row = engine.stop_coords.loc[start_stop_id]
        folium.Marker(
            location=[start_row["stop_lat"], start_row["stop_lon"]],
            popup=f"出発: {start_row['stop_name']}",
            icon=folium.Icon(color="blue", icon="star")
        ).add_to(m)

        # 施設マーカー表示（シミュレーションモード時）
        if facility_data is not None and stats.get("mode") == "simulation":
            sim_rb = stats["result_before"]
            sim_ra = stats["result_after"]
            sim_st = stats["start_time_sec"]
            for ftype in facility_data.facility_types:
                facilities = facility_data.get_facilities_by_type(ftype)
                acc_before = facility_data.calc_facility_access(
                    sim_rb, sim_st, facilities, engine.stop_coords
                )
                acc_after = facility_data.calc_facility_access(
                    sim_ra, sim_st, facilities, engine.stop_coords
                )
                for ab, aa in zip(acc_before, acc_after):
                    if ab["accessible"] and aa["accessible"]:
                        color = "green"
                        status = "✅ アクセス可能（変化なし）"
                        time_info = f"所要: {aa['total_time_min']}分"
                    elif ab["accessible"] and not aa["accessible"]:
                        color = "red"
                        status = "❌ アクセス不能になった"
                        time_info = f"変更前: {ab['total_time_min']}分"
                    elif not ab["accessible"] and aa["accessible"]:
                        color = "green"
                        status = "🆕 新たにアクセス可能"
                        time_info = f"所要: {aa['total_time_min']}分"
                    else:
                        color = "gray"
                        status = "⚪ 元からアクセス不能"
                        time_info = ""

                    popup_text = (
                        f"<b>{ab['facility_name']}</b><br>"
                        f"種別: {ab['facility_type']}<br>"
                        f"{status}<br>"
                        f"{time_info}<br>"
                        f"最寄りバス停: {ab['nearest_stop']}<br>"
                        f"バス停から徒歩: {ab['walk_time_min']}分（{ab['walk_distance_m']}m）"
                    )

                    icon_shape = "plus" if "病院" in ab["facility_type"] or "歯科" in ab["facility_type"] else "shopping-cart" if "スーパー" in ab["facility_type"] or "コンビニ" in ab["facility_type"] else "envelope" if "郵便局" in ab["facility_type"] else "medkit" if "薬局" in ab["facility_type"] else "info-sign"

                    folium.Marker(
                        location=[ab["facility_lat"], ab["facility_lon"]],
                        popup=folium.Popup(popup_text, max_width=300),
                        icon=folium.Icon(color=color, icon=icon_shape, prefix="glyphicon")
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

# ===== 施設アクセス分析モード =====
# ===== 集客圏分析モード =====
if mode == "集客圏分析":
    if facility_data is not None:
        reverse_facility_type = st.sidebar.selectbox(
            "施設種別", facility_data.facility_types, key="rev_ftype"
        )
        reverse_facilities_df = facility_data.get_facilities_by_type(reverse_facility_type)
        reverse_facility_names = reverse_facilities_df["name"].tolist()
        reverse_facility_name = st.sidebar.selectbox(
            "目的施設", reverse_facility_names, key="rev_fname"
        )
    else:
        reverse_facility_type = None
        reverse_facility_name = None
    arrival_hour_rev = st.sidebar.slider("到着時刻（時）", 5, 22, 9, key="rev_hour")
    arrival_min_rev = st.sidebar.slider("到着時刻（分）", 0, 59, 0, step=10, key="rev_min")

    if st.sidebar.button("集客圏分析実行", type="primary"):
        if facility_data is None or reverse_facility_name is None:
            st.error("施設データが読み込まれていません")
        else:
            arrival_time_rev = arrival_hour_rev * 3600 + arrival_min_rev * 60
            facilities_df = facility_data.get_facilities_by_type(reverse_facility_type)
            target_df = facilities_df[facilities_df["name"] == reverse_facility_name]
            if target_df.empty:
                st.error("施設が見つかりません")
            else:
                nearest = facility_data.find_nearest_stops(target_df, engine.stop_coords, max_distance=500)
                if not nearest:
                    st.error("500m以内にバス停が見つかりません")
                else:
                    dest_stop_id_rev = nearest[0]["nearest_stop"]
                    dest_stop_name_rev = engine.stop_coords.loc[dest_stop_id_rev, "stop_name"] if dest_stop_id_rev in engine.stop_coords.index else str(dest_stop_id_rev)
                    walk_min_rev = round(nearest[0]["walk_time_sec"] / 60, 1)

                    with st.spinner("集客圏を計算中..."):
                        rev_result = engine.calc_reverse_isochrone(
                            dest_stop_id_rev, arrival_time_rev, max_time_sec
                        )

                    m = folium.Map(
                        location=[engine.stop_coords["stop_lat"].mean(),
                                  engine.stop_coords["stop_lon"].mean()],
                        zoom_start=13, tiles="CartoDB positron"
                    )

                    for stop_id, dep_time in rev_result.items():
                        if stop_id not in engine.stop_coords.index:
                            continue
                        row = engine.stop_coords.loc[stop_id]
                        travel_min = (arrival_time_rev - dep_time) / 60
                        if travel_min <= 15:
                            color = "green"
                        elif travel_min <= 30:
                            color = "lightgreen"
                        elif travel_min <= 45:
                            color = "orange"
                        else:
                            color = "red"
                        dep_h = int(dep_time // 3600)
                        dep_m = int((dep_time % 3600) // 60)
                        popup_text = (
                            f"<b>{row['stop_name']}</b><br>"
                            f"出発: {dep_h:02d}:{dep_m:02d}<br>"
                            f"所要: {travel_min:.0f}分"
                        )
                        folium.CircleMarker(
                            location=[row["stop_lat"], row["stop_lon"]],
                            radius=6, color=color, fill=True, fill_opacity=0.8,
                            popup=folium.Popup(popup_text, max_width=200)
                        ).add_to(m)

                    # 施設マーカー
                    target_row = target_df.iloc[0]
                    folium.Marker(
                        location=[target_row["latitude"], target_row["longitude"]],
                        popup=f"目的地: {reverse_facility_name}<br>最寄り: {dest_stop_name_rev}（徒歩{walk_min_rev}分）",
                        icon=folium.Icon(color="blue", icon="star")
                    ).add_to(m)

                    # 最寄りバス停マーカー
                    dest_row_rev = engine.stop_coords.loc[dest_stop_id_rev]
                    folium.CircleMarker(
                        location=[dest_row_rev["stop_lat"], dest_row_rev["stop_lon"]],
                        radius=10, color="blue", fill=True, fill_opacity=0.9,
                        popup=f"最寄りバス停: {dest_stop_name_rev}"
                    ).add_to(m)

                    rev_pop = None
                    if pop_data is not None:
                        rev_pop = pop_data.get_population_near_stops(
                            engine.stop_coords, list(rev_result.keys()), radius_m=300
                        )

                    st.session_state.result_map = m
                    st.session_state.result_stats = {
                        "mode": "reverse",
                        "dest_name": reverse_facility_name,
                        "dest_type": reverse_facility_type,
                        "nearest_stop": dest_stop_name_rev,
                        "walk_min": walk_min_rev,
                        "arrival_time": f"{arrival_hour_rev:02d}:{arrival_min_rev:02d}",
                        "reachable": len(rev_result),
                        "max_time_min": max_time_sec // 60,
                        "pop_result": rev_pop,
                    }
if mode == "施設アクセス":
    facility_type = st.sidebar.selectbox("施設種別", facility_data.facility_types)
    walk_speed_option = st.sidebar.radio(
        "歩行速度",
        ["一般（分速67m）", "高齢者（分速40m）"],
    )
    walk_speed = 67 if "一般" in walk_speed_option else 40

    if st.sidebar.button("施設アクセス分析実行", type="primary"):
        with st.spinner("計算中..."):
            result, prev = engine.calc_isochrone(
                start_stop_id, start_time_sec, max_time_sec, track_path=True
            )

            facilities = facility_data.get_facilities_by_type(facility_type)
            access = facility_data.calc_facility_access(
                result, start_time_sec, facilities, engine.stop_coords,
                walk_speed=walk_speed
            )

            m = folium.Map(
                location=[42.35, 140.97], zoom_start=13,
                tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
                attr="&copy; OpenStreetMap contributors &copy; CARTO"
            )

            # 到達可能バス停をグレーで表示
            for stop_id in result:
                if stop_id not in engine.stop_coords.index:
                    continue
                row = engine.stop_coords.loc[stop_id]
                folium.CircleMarker(
                    location=[row["stop_lat"], row["stop_lon"]],
                    radius=3, color="gray", fill=True, fill_opacity=0.3,
                ).add_to(m)

            # 施設をマーカーで表示
            accessible_count = 0
            for fac in access:
                if fac["accessible"]:
                    color = "green"
                    icon = "ok-sign"
                    accessible_count += 1
                    popup_text = (
                        f"<b>{fac['facility_name']}</b><br>"
                        f"✅ アクセス可能<br>"
                        f"合計: {fac['total_time_min']}分<br>"
                        f"最寄りバス停: {fac['nearest_stop']}<br>"
                        f"バス停から徒歩: {fac['walk_time_min']}分（{fac['walk_distance_m']}m）"
                    )
                else:
                    color = "red"
                    icon = "remove-sign"
                    popup_text = (
                        f"<b>{fac['facility_name']}</b><br>"
                        f"❌ アクセス不可<br>"
                        f"最寄りバス停: {fac['nearest_stop']}<br>"
                        f"バス停から徒歩: {fac['walk_time_min']}分（{fac['walk_distance_m']}m）<br>"
                        f"（バス停まで制限時間内に到達不能）"
                    )

                folium.Marker(
                    location=[fac["facility_lat"], fac["facility_lon"]],
                    popup=folium.Popup(popup_text, max_width=300),
                    icon=folium.Icon(color=color, icon=icon, prefix="glyphicon")
                ).add_to(m)

            # 出発地点
            start_row = engine.stop_coords.loc[start_stop_id]
            folium.Marker(
                location=[start_row["stop_lat"], start_row["stop_lon"]],
                popup=f"出発: {start_row['stop_name']}",
                icon=folium.Icon(color="blue", icon="star")
            ).add_to(m)

            st.subheader(f"{facility_type}へのアクセス分析")
            st.markdown(f"**出発: {start_stop_name} {start_hour:02d}:{start_minute:02d} / 制限: {max_time_min}分 / 歩行: {walk_speed_option}**")

            col1, col2 = st.columns([3, 1])
            with col1:
                st_folium(m, width=1100, height=600, returned_objects=[])
            with col2:
                st.metric(f"{facility_type}総数", f"{len(access)}件")
                st.metric("アクセス可能", f"{accessible_count}件")
                st.metric("アクセス不可", f"{len(access) - accessible_count}件")

                st.markdown("**アクセス可能な施設:**")
                for fac in sorted([f for f in access if f["accessible"]], key=lambda x: x["total_time_min"]):
                    st.markdown(f"✅ {fac['facility_name']}（{fac['total_time_min']}分）")

                if any(not f["accessible"] for f in access):
                    st.markdown("**アクセス不可の施設:**")
                    for fac in access:
                        if not fac["accessible"]:
                            st.markdown(f"❌ {fac['facility_name']}")
# ===== 時間空白診断（3Dカラム）モード =====
if mode == "時間空白診断（3D）":
    st.subheader("時間空白診断")
    st.caption("柱の高さ＝その時間帯に拠点へ着けない人口。色＝1日のうち何時間帯で空白になるか。")

    if pop_data is None:
        st.warning("メッシュ人口データが読み込まれていないため、この分析は実行できません。")
    elif not blank_dest_ids:
        st.info("サイドバーで生活拠点のバス停を1つ以上選んでください。")
    else:
        analyzer = load_blank_analyzer(engine, pop_data, blank_walk_m, blank_walk_speed)

        if st.sidebar.button("空白診断を実行", type="primary"):
            hours = list(range(5, 23))
            with st.spinner("全時間帯の空白人口を計算中..."):
                per_hour = {}
                blank_hours = np.zeros(analyzer.n_mesh, dtype=int)
                for h in hours:
                    df_h = analyzer.diagnose(blank_dest_ids, h * 3600, max_time_sec)
                    per_hour[h] = df_h
                    blank_hours += (~df_h["reachable"]).to_numpy().astype(int)
            st.session_state.blank_result = {
                "hours": hours,
                "per_hour": per_hour,
                "blank_hours": blank_hours,
                "dest_names": list(blank_dest_names),
                "max_time_min": max_time_min,
                "walk_m": blank_walk_m,
                "walk_speed": blank_walk_speed,
                "day_type": day_type,
            }

        result = st.session_state.get("blank_result")
        if result is None:
            st.info("サイドバーの「空白診断を実行」を押してください。")
        else:
            st.caption(
                f"{result.get('day_type', '?')}ダイヤ ／ 拠点: {'、'.join(result['dest_names'])} ／ "
                f"制限 {result['max_time_min']}分 ／ "
                f"徒歩 {result['walk_m']}m・{result['walk_speed']}m/分"
            )
            if result.get("day_type") != day_type:
                st.warning(f"表示中の結果は「{result.get('day_type')}ダイヤ」で計算したものです。"
                           f"現在の設定（{day_type}）で見るには再実行してください。")
            sel_hour = st.slider("到着時刻（時）", min(result["hours"]), max(result["hours"]),
                                 min(9, max(result["hours"])))
            df_sel = result["per_hour"][sel_hour].copy()
            # 静的空白（徒歩圏にバス停なし）は終日空白になるのが当たり前なので、
            # 深刻度（何時間帯で空白か）は静的でないメッシュだけで数える。
            df_sel["blank_hours"] = np.where(df_sel["static_blank"], 0, result["blank_hours"])
            df_sel["blank_pop"] = np.where(df_sel["reachable"], 0.0, df_sel["pop"])
            df_sel["blank_elderly"] = np.where(df_sel["reachable"], 0.0, df_sel["elderly"])
            df_sel["static_pop"] = np.where(df_sel["static_blank"], df_sel["pop"], 0.0)

            n_hours = len(result["hours"])
            if blank_mesh_level == 500:
                cells = aggregate_to_500m(df_sel)
                key = df_sel["meshcode"].astype(str)
                df_sel["_cell"] = (key.str[:8]
                                   + (key.str[8].astype(int) // 5).astype(str)
                                   + (key.str[9].astype(int) // 5).astype(str))
                cells["static_pop"] = cells["cell"].map(
                    df_sel.groupby("_cell")["static_pop"].sum()).fillna(0.0)
                radius, inner = 250, 210
            else:
                cells = df_sel
                radius, inner = 50, 42

            # 深刻度（1〜5）を暖色に対応させる。docs/ の単体版と同じ配色。
            SEV = ["#F7D2B8", "#F0A575", "#E07440", "#BC4C22", "#7F2E14"]
            def sev_rgb(h):
                step = 1 if h <= 3 else 2 if h <= 7 else 3 if h <= 11 else 4 if h <= 15 else 5
                c = SEV[step - 1].lstrip("#")
                return [int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)]

            base = cells[cells["static_pop"] > 0].copy()          # 灰色の土台
            base["static_disp"] = base["static_pop"].round().astype(int)
            time_cells = cells[(cells["blank_pop"] - cells["static_pop"]) > 0].copy()  # 暖色の柱
            time_cells[["r", "g", "b"]] = [sev_rgb(int(h)) for h in time_cells["blank_hours"]]
            time_cells["blank_pop_disp"] = time_cells["blank_pop"].round().astype(int)
            time_cells["blank_elderly_disp"] = time_cells["blank_elderly"].round().astype(int)
            time_cells["time_disp"] = (time_cells["blank_pop"] - time_cells["static_pop"]).round().astype(int)

            total_pop = float(df_sel["pop"].sum())
            blank_pop = float(df_sel.loc[~df_sel["reachable"], "pop"].sum())
            blank_eld = float(df_sel.loc[~df_sel["reachable"], "elderly"].sum())
            static_pop = float(df_sel.loc[df_sel["static_blank"], "pop"].sum())

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("空白人口", f"{blank_pop:,.0f}人", f"{100*blank_pop/max(total_pop,1):.1f}%")
            c2.metric("うち高齢者", f"{blank_eld:,.0f}人")
            c3.metric("静的空白（徒歩圏にバス停なし）", f"{static_pop:,.0f}人")
            c4.metric("時間空白（バス停はあるが間に合わない）",
                      f"{blank_pop - static_pop:,.0f}人")

            view = pdk.ViewState(
                latitude=float(df_sel["lat"].mean()),
                longitude=float(df_sel["lon"].mean()),
                zoom=11, pitch=50, bearing=0,
            )
            # 灰色の土台（静的空白）の上に、時間空白の柱（暖色・深刻度で色分け）を重ねる
            static_layer = pdk.Layer(
                "ColumnLayer",
                data=base[["lon", "lat", "static_pop", "static_disp"]],
                get_position=["lon", "lat"], get_elevation="static_pop",
                elevation_scale=blank_elev_scale, radius=radius,
                get_fill_color=[174, 184, 194, 200], pickable=True, auto_highlight=True,
            )
            time_layer = pdk.Layer(
                "ColumnLayer",
                data=time_cells[["lon", "lat", "blank_pop", "blank_pop_disp",
                                 "blank_elderly_disp", "time_disp", "blank_hours", "r", "g", "b"]],
                get_position=["lon", "lat"], get_elevation="blank_pop",
                elevation_scale=blank_elev_scale, radius=inner,
                get_fill_color=["r", "g", "b", 220], pickable=True, auto_highlight=True,
            )
            st.pydeck_chart(pdk.Deck(
                layers=[static_layer, time_layer],
                initial_view_state=view,
                map_style="light",
                tooltip={"text": "空白人口 {blank_pop_disp}人\n"
                                 "うち時間空白 {time_disp}人\n"
                                 "うち高齢者 {blank_elderly_disp}人\n"
                                 "時間空白になる時間帯 {blank_hours}/" + str(n_hours)},
            ))

            st.caption("灰色の土台＝静的空白（徒歩圏にバス停なし） ／ "
                       "暖色の柱＝時間空白。色が濃いほど多くの時間帯で着けない。")

            st.markdown("**時間帯別の空白人口**")
            hourly = pd.DataFrame({
                "時": result["hours"],
                "空白人口": [
                    float(result["per_hour"][h].loc[~result["per_hour"][h]["reachable"], "pop"].sum())
                    for h in result["hours"]
                ],
            }).set_index("時")
            st.bar_chart(hourly)

# ===== 処方（改善施策）モード =====
if mode == "処方（改善施策）":
    st.subheader("処方：どの施策が空白を減らすか")
    st.caption("診断で見つけた時間空白に対し、増便とダイヤシフトの効果を同じ指標（空白人口）で比較します。")

    if pop_data is None:
        st.warning("メッシュ人口データが読み込まれていないため、この分析は実行できません。")
    elif not blank_dest_ids:
        st.info("サイドバーで生活拠点のバス停を1つ以上選んでください。")
    else:
        analyzer = load_blank_analyzer(engine, pop_data, blank_walk_m, blank_walk_speed)
        diag = analyzer.diagnose(blank_dest_ids, presc_hour * 3600, max_time_sec)
        summary = analyzer.summarize(diag)

        c1, c2, c3 = st.columns(3)
        c1.metric(f"{presc_hour}時までに着けない人口", f"{summary['blank_pop']:,}人",
                  f"{100*summary['blank_pop']/max(summary['total_pop'],1):.1f}%")
        c2.metric("うち高齢者", f"{summary['blank_elderly']:,}人")
        c3.metric("うち時間空白（施策で減らせる分）",
                  f"{summary['blank_pop'] - summary['static_blank_pop']:,}人")
        st.caption(f"{day_type}ダイヤ ／ 拠点: {'、'.join(blank_dest_names)} ／ 制限 {max_time_min}分 ／ "
                   f"徒歩 {blank_walk_m}m・{blank_walk_speed}m/分")

        candidates = rank_routes_by_blank_coverage(engine, analyzer, diag)
        if candidates.empty:
            st.info("空白地域を通る路線が見つかりませんでした。拠点や時刻を変えてみてください。")
        else:
            st.markdown("**空白地域を通る路線（施策の候補）**")
            st.caption("便数の多い路線を選んでも空白は減りません。空白地域を通る路線かどうかで選ぶ必要があります。")
            st.dataframe(candidates.head(10), hide_index=True, width="stretch")

            picked = st.multiselect(
                "比較する路線", list(candidates["route_id"]),
                default=list(candidates.head(presc_n_routes)["route_id"]),
                format_func=lambda rid: candidates.set_index("route_id").loc[rid, "route_name"],
            )

            if st.button("施策を比較する", type="primary") and picked:
                names = candidates.set_index("route_id")["route_name"]
                plans = []
                for rid in picked:
                    label = str(names.loc[rid])[:18]
                    plans.append(Plan(
                        f"増便 {label}（{inc_window[0]}-{inc_window[1]}時 +{inc_trips}便/パターン）",
                        lambda e, x=rid: e.edges_after_frequency_increase(
                            x, inc_window[0], inc_window[1], inc_trips)))
                    plans.append(Plan(
                        f"シフト {label}（{shift_from[0]}-{shift_from[1]}時→{inc_window[0]}-{inc_window[1]}時 {shift_trips}便）",
                        lambda e, x=rid: e.edges_after_timetable_shift(
                            x, shift_from, inc_window, shift_trips)))
                if len(picked) > 1:
                    def _multi(e, ids=tuple(picked)):
                        bus, walk = e.bus_edges, e.walk_edges
                        for x in ids:
                            with e.scenario(bus, walk):
                                bus, walk = e.edges_after_frequency_increase(
                                    x, inc_window[0], inc_window[1], inc_trips)
                        return bus, walk
                    plans.append(Plan(f"増便 選んだ{len(picked)}路線すべて", _multi))

                with st.spinner("全時間帯で効果を計算中..."):
                    st.session_state.presc_table = compare(
                        engine, analyzer, blank_dest_ids, plans,
                        arrival_hour=presc_hour, max_time_sec=max_time_sec)

            table = st.session_state.get("presc_table")
            if table is not None:
                st.markdown("**施策の比較（全日の削減が大きい順）**")
                st.dataframe(table, hide_index=True, width="stretch")
                st.caption(
                    "「全日の削減」は5〜22時の延べ空白人口の減少。ダイヤシフトは便を移すだけなので"
                    "狙った時刻は必ず良くなりますが、便を抜いた時間帯は悪化します。"
                    "単一時刻だけで見ると『費用ゼロで改善』に見えてしまうため全日で評価しています。"
                    "「運行キロ増」は費用の代理値（追加便数×路線長）で、実際の単価を掛ければ金額になります。"
                )
                worsen = table[table["全日の削減"] < 0]
                if not worsen.empty:
                    st.warning("全日で見ると逆効果になる施策があります: "
                               + "、".join(worsen["施策"].tolist()))

# ===== 結果表示 =====
if mode not in ("時間空白診断（3D）", "処方（改善施策）") and st.session_state.result_map is not None:
    col1, col2 = st.columns([3, 1])

    with col1:
        st_folium(st.session_state.result_map, width=900, height=600, returned_objects=[])

    with col2:
        stats = st.session_state.result_stats
        if stats["mode"] == "reverse":
            st.subheader("集客圏分析")
            st.caption(f"{stats['dest_name']}（{stats.get('dest_type','')}）")
            st.caption(f"最寄りバス停: {stats.get('nearest_stop','')}（徒歩{stats.get('walk_min',0)}分）")
            st.caption(f"到着: {stats['arrival_time']} / 制限: {stats['max_time_min']}分")
            st.metric("集客圏バス停数", f"{stats['reachable']}箇所")
            if stats.get("pop_result"):
                st.divider()
                st.subheader("カバー人口")
                st.metric("カバー人口", f"{stats['pop_result']['total']:,}人")
                st.metric("うち高齢者", f"{stats['pop_result']['elderly']:,}人")
            st.divider()
            st.markdown("**凡例**")
            st.markdown("🟢 15分以内")
            st.markdown("🟡 15〜30分")
            st.markdown("🟠 30〜45分")
            st.markdown("🔴 45〜60分")
        elif stats["mode"] == "到達圏のみ":
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
            st.divider()
            st.subheader("人口影響")
            st.metric("変更前カバー人口", f"{stats['pop_before']['total']:,}人")
            st.metric("変更後カバー人口", f"{stats['pop_after']['total']:,}人")
            st.metric("影響を受ける人口", f"{stats['pop_impact']['affected_total']:,}人",
                      delta=f"-{stats['pop_impact']['affected_total']:,}人", delta_color="inverse")
            st.metric("うち高齢者(65歳以上)", f"{stats['pop_impact']['affected_elderly']:,}人")

            # 施設アクセス比較
            if facility_data is not None and "result_before" in stats:
                st.divider()
                st.subheader("施設アクセス影響")
                sim_result_before = stats["result_before"]
                sim_result_after = stats["result_after"]
                sim_start_time = stats["start_time_sec"]
                for ftype in facility_data.facility_types:
                    facilities = facility_data.get_facilities_by_type(ftype)
                    access_before = facility_data.calc_facility_access(
                        sim_result_before, sim_start_time, facilities, engine.stop_coords
                    )
                    access_after = facility_data.calc_facility_access(
                        sim_result_after, sim_start_time, facilities, engine.stop_coords
                    )
                    before_count = sum(1 for a in access_before if a["accessible"])
                    after_count = sum(1 for a in access_after if a["accessible"])
                    diff = after_count - before_count

                    if diff < 0:
                        st.metric(
                            f"{ftype}",
                            f"{after_count}/{len(access_after)}件",
                            delta=f"{diff}件", delta_color="inverse"
                        )
                    else:
                        st.metric(
                            f"{ftype}",
                            f"{after_count}/{len(access_after)}件"
                        )

                # 詳細：アクセス不能になった施設を表示
                lost_facilities = []
                for ftype in facility_data.facility_types:
                    facilities = facility_data.get_facilities_by_type(ftype)
                    access_before = facility_data.calc_facility_access(
                        sim_result_before, sim_start_time, facilities, engine.stop_coords
                    )
                    access_after = facility_data.calc_facility_access(
                        sim_result_after, sim_start_time, facilities, engine.stop_coords
                    )
                    for ab, aa in zip(access_before, access_after):
                        if ab["accessible"] and not aa["accessible"]:
                            lost_facilities.append(f"{ab['facility_name']}（{ab['facility_type']}）")

                if lost_facilities:
                    with st.expander(f"アクセス不能になった施設（{len(lost_facilities)}件）"):
                        for name in lost_facilities:
                            st.markdown(f"❌ {name}")

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
