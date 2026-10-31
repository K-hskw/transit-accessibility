import streamlit as st
import numpy as np
import pandas as pd
import folium
import os
from datetime import date
import pydeck as pdk
from streamlit_folium import st_folium
from transit_engine import TransitEngine
from population import PopulationData, FacilityData
from blank_area import BlankAreaAnalyzer, aggregate_to_500m, arrival_times, clock, STEP_MIN
from prescription import Plan, compare, rank_routes_by_blank_coverage
import policy_report
import session_data

st.set_page_config(page_title="クウハクスコープ｜時間空白の診断と改善策", layout="wide")
st.title("クウハクスコープ")

# ===== データ管理 =====
DATA_DIR = "."
GTFS_DIR_DEFAULT = "gtfs_data"        # デフォルト（室蘭）

# データを差し替えたら消す計算結果。残すと、別の都市の地図や表が
# 新しいデータの結果として表示される。
RESULT_KEYS = ("result_map", "result_stats", "result_mode", "animation_results",
               "blank_result", "presc_table", "presc_hour_used", "presc_conditions",
               "presc_doc_refined")

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
def load_population():
    if os.path.exists("100m_mesh_pop2020_01205室蘭市.csv"):
        return PopulationData("100m_mesh_pop2020_01205室蘭市.csv")
    return None

@st.cache_resource
def load_facilities():
    if os.path.exists("facilities.csv"):
        return FacilityData("facilities.csv")
    return None

def get_blank_analyzer(engine, pop_data, max_walk_m, walk_speed):
    """この訪問者のエンジンに結びついた空白診断器を返す

    以前は cache_resource で全訪問者が共有していたが、引数名の先頭が _ の
    エンジンと人口はキャッシュのキーに入らない。そのため別の都市を
    アップロードしても室蘭のエンジンに結びついた診断器が返っていた。
    徒歩対応表の構築は1秒未満なので、訪問者ごとに持てば足りる。
    """
    cache = st.session_state.setdefault("analyzers", {})
    key = (st.session_state.get("data_version", 0), max_walk_m, walk_speed)
    if key not in cache:
        cache[key] = BlankAreaAnalyzer(engine, pop_data,
                                       max_walk_m=max_walk_m, walk_speed_m_min=walk_speed)
    return cache[key]


def warn_if_regions_disjoint(analyzer):
    """人口と時刻表の地域がずれていたら警告する（ずれていると空白人口が100%と出てしまう）"""
    share = analyzer.served_share()
    if share < 0.2:
        st.warning(f"人口データと時刻表の地域がほとんど重なっていません（歩ける範囲にバス停がある人口が"
                   f"{100 * share:.0f}%）。別の地域の人口データを使っていないか、サイドバーの"
                   "「データ設定」を確認してください。")


def work_dir():
    """この訪問者のアップロードを置く一時ディレクトリ"""
    if not st.session_state.get("work_dir") or not os.path.isdir(st.session_state.work_dir):
        st.session_state.work_dir = session_data.new_work_dir()
    return st.session_state.work_dir


def data_changed():
    """データを差し替えたら、古いデータで作った結果と診断器を捨てる"""
    st.session_state.data_version = st.session_state.get("data_version", 0) + 1
    st.session_state.analyzers = {}
    for k in RESULT_KEYS:
        st.session_state.pop(k, None)


# デフォルトデータをロード。エンジンは読み込み済みデータを共有しつつ、
# ダイヤ種別やシナリオの状態は訪問者ごとに分けたコピーを使う。
if st.session_state.engine is None:
    st.session_state.engine = load_engine().session_copy()
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
                path = os.path.join(work_dir(), "uploaded_facilities.csv")
                fac_df.to_csv(path, index=False)
                st.session_state.facility_data = FacilityData(path)
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
                # 元のファイル名で保存する（政策文書の自治体名をここから取る）
                path = os.path.join(work_dir(), session_data.safe_filename(
                    uploaded_pop.name, "uploaded_population.csv"))
                pop_df.to_csv(path, index=False)
                # アップロード欄のファイルは再実行のたびに渡ってくるので、
                # 中身が変わったときだけ読み直す（毎回だと計算結果が消え続ける）
                source = (uploaded_pop.name, uploaded_pop.size)
                if st.session_state.get("pop_source") != source:
                    st.session_state.pop_data = PopulationData(path)
                    st.session_state.pop_source = source
                    data_changed()
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
                    new_engine = session_data.build_uploaded_engine(uploaded_gtfs, work_dir())
                    st.session_state.engine = new_engine.session_copy()
                    st.session_state.gtfs_label = session_data.safe_filename(uploaded_gtfs.name)
                    data_changed()
                    st.success("GTFSデータ適用完了！ネットワークを再構築しました。")
                    st.rerun()
                except Exception as e:
                    st.error(f"GTFSデータ適用エラー: {e}")

    # デフォルトデータに戻すボタン（この訪問者の表示だけが戻る）
    if st.session_state.get("gtfs_label"):
        st.divider()
        if st.button("🔄 デフォルト（室蘭）に戻す"):
            # 戻すのはGTFSだけ（人口・施設はアップロード欄に残っている限り使い続ける）
            st.session_state.pop("gtfs_label", None)
            st.session_state.engine = load_engine().session_copy()
            data_changed()
            st.success("デフォルトデータに戻しました")
            st.rerun()

# データソース表示（共有ディレクトリではなく、この訪問者の状態で判定する）
if st.session_state.get("gtfs_label"):
    area_name = f"アップロードしたデータ（{st.session_state.gtfs_label}）"
else:
    area_name = "室蘭市 道南バス"
feed = engine.feed_info()
period = (f"（有効期間 {feed['start']}〜{feed['end']}）"
          if feed and feed.get("start") and feed.get("end") else "")
st.caption(f"{area_name} GTFSデータに基づくシミュレーション{period}")
# 有効期間の切れた時刻表で分析を続けていたことがあった（4〜9月版を10月以降も使用）
if feed and feed.get("end") and feed["end"] < date.today().isoformat():
    st.warning(f"読み込んでいる時刻表の有効期間は {feed['end']} で終わっています。"
               "最新のGTFSで確認してください。")

# データの出典（簡易100mメッシュ人口は CC BY のため作成者の表示が必要）
pop_name = os.path.basename(str(getattr(pop_data, "source_path", "") or ""))
st.sidebar.caption(
    "出典: " + ("道南バス GTFS-JP（ODPT基本ライセンス）" if not st.session_state.get("gtfs_label")
               else "アップロードしたGTFS")
    + " ／ " + ("簡易100mメッシュ人口（作成: 西澤明、CC BY）" if pop_name.startswith("100m_mesh_pop2020_")
                else f"人口: {pop_name or '未読込'}")
    + " ／ 国土数値情報（医療機関・学校）")

if "result_map" not in st.session_state:
    st.session_state.result_map = None
if "result_stats" not in st.session_state:
    st.session_state.result_stats = None

# ===== サイドバー =====
# モードを「診断 → 劣化 → 処方」の3段に整理する。10個を並列に並べると
# 初見でどこから触ればよいか分からず、核心の時間空白診断が埋もれるため。
STEPS = {
    "① 空白を見つける": ["時間空白診断（3D）", "時間帯別到達圏", "到達圏のみ",
                   "集客圏分析", "施設アクセス"],
    "② 廃止・減便シミュレーション": ["路線廃止", "減便", "バス停削除"],
    "③ 改善策の比較": ["改善策", "代替路線追加"],
}
# 出発地点・出発時刻を使うのは「起点から出発する」モードだけ。
# 時間空白診断と処方は拠点と到着時刻で考えるので、出しても効かない。
ORIGIN_MODES = ("到達圏のみ", "路線廃止", "バス停削除", "減便",
                "時間帯別到達圏", "施設アクセス", "代替路線追加")
DEGRADE_MODES = ("路線廃止", "バス停削除", "減便", "代替路線追加")

st.sidebar.header("設定")

# ダイヤ種別。交通空白は休日にこそ深刻になるため平日固定にしない。
day_type = st.sidebar.radio("ダイヤ", engine.available_day_types, horizontal=True)
engine.set_day_type(day_type)
st.sidebar.caption(f"{day_type}ダイヤ: {engine.bus_edges['trip_id'].nunique()}便 / "
                   f"{len(engine.bus_edges):,}エッジ")

st.sidebar.divider()
step = st.sidebar.radio("分析の流れ", list(STEPS))
mode = st.sidebar.radio("分析の種類", STEPS[step])
st.sidebar.divider()

stop_names = engine.get_stop_names()

if mode in ORIGIN_MODES:
    start_stop_name = st.sidebar.selectbox(
        "出発地点", stop_names,
        index=stop_names.index("室蘭駅前") if "室蘭駅前" in stop_names else 0
    )
    start_hour = st.sidebar.slider("出発時刻（時）", 5, 22, 8)
    start_minute = st.sidebar.slider("出発時刻（分）", 0, 55, 0, step=5)
else:
    # 起点を使わないモードでも、内部の共通処理が参照するので既定値を置く
    start_stop_name = "室蘭駅前" if "室蘭駅前" in stop_names else stop_names[0]
    start_hour, start_minute = 8, 0

start_stop_ids = engine.get_stop_ids_by_name(start_stop_name)
start_stop_id = start_stop_ids[0]
# 地図は出発地を中心に開く（以前は室蘭の座標に固定で、他地域のGTFSでは地図の外になった）
origin_center = engine.stop_coords.loc[start_stop_id, ["stop_lat", "stop_lon"]].astype(float).tolist()
start_time_sec = start_hour * 3600 + start_minute * 60

max_time_min = st.sidebar.select_slider("制限時間（分）", options=[15, 30, 45, 60, 90], value=60)
max_time_sec = max_time_min * 60
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
        # 以前は N を選ぶ欄が無く、常に「半分にする」と同じ結果で「0本に1本」と表示されていた
        reduce_keep_every = st.sidebar.slider("何本に1本残すか（N）", 2, 6, 3)

    elif reduce_mode == "全路線一律で削減":
        reduce_pct = st.sidebar.slider("削減率（%）", 10, 80, 50, step=10)
        reduce_ratio = reduce_pct / 100
    st.sidebar.caption("方向・経路（停留所の並び）ごとに便を時刻順に並べ、偏りなく間引きます。")

# 「何分悪化したら悪化と数えるか」は前後比較をする劣化モードでのみ意味がある
if mode in DEGRADE_MODES:
    threshold_min = st.sidebar.slider("悪化閾値（分）", 1, 15, 1)
else:
    threshold_min = 1

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

elif mode in ("時間空白診断（3D）", "改善策"):
    # 空白の定義（拠点・徒歩条件）は診断と処方で揃える必要があるので共通化する
    st.sidebar.markdown("**生活拠点**")
    st.sidebar.caption("到着時刻の「制限時間」分前以降に家を出て、到着時刻までにここで選んだバス停へ"
                       "着けない地域を「空白」とします。早く着いて待つ時間も制限時間に含みます。")
    blank_dest_names = st.sidebar.multiselect(
        "拠点にするバス停（複数可）", stop_names,
        default=[n for n in ["東室蘭駅東口"] if n in stop_names]
    )
    blank_dest_ids = []
    for n in blank_dest_names:
        blank_dest_ids.extend(engine.get_stop_ids_by_name(n))

    blank_walk_m = st.sidebar.slider("バス停まで歩ける距離（m）", 200, 1000, 500, step=100)
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
        blank_elev_scale = st.sidebar.slider("柱の高さの表示倍率", 1, 20, 6,
                                             help="見やすさの調整だけで、計算結果は変わりません。")
    else:
        st.sidebar.markdown("**対象とする時間帯**")
        presc_hour = st.sidebar.slider("この時間帯に拠点へ着けるか（時台）", 5, 22, 7)
        st.sidebar.caption("到着時刻を5分刻みにして平均します。休日の朝など、空白が大きい時間帯を選ぶと施策の差が出やすい。")

        st.sidebar.markdown("**増便**")
        # 足す便の時間帯は対象の時間帯に合わせる。以前は5〜9時に固定で、1便だと7:00発になり、
        # 7時台に拠点へ着く便にならなかった。対象を変えたら既定値ごと作り直すため、
        # キーに対象の時間帯を含める（同じキーのまま既定値だけ変えても反映されない）。
        inc_window = st.sidebar.slider("足す便が拠点を通る時間帯", 5, 23,
                                       (presc_hour, presc_hour + 1), key=f"inc_window_{presc_hour}")
        inc_trips = st.sidebar.slider("パターンごとの追加便数", 1, 4, 1)
        st.sidebar.caption("拠点を通る系統は拠点を通る時刻、通らない系統は始発時刻がこの時間帯に入るよう、"
                           "等間隔に置きます。方向別に足すため、往復2パターンなら実際の追加は2倍になります。")

        st.sidebar.markdown("**ダイヤシフト（増便せず時間帯を移す）**")
        shift_from = st.sidebar.slider("便を減らす時間帯（拠点を通る時刻）", 5, 23, (10, 15))
        st.sidebar.caption("減らした便は、上の「足す便が拠点を通る時間帯」へ移します。")
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
# この実行ブロックが結果を作れるのは下の5モードだけ。他のモードは自前の
# 実行ボタンを持っている。全モードでこのボタンを出していたため、対応しない
# モードで押すと result_after が未定義のまま参照されてアプリが落ちていた。
SIM_MODES = ("到達圏のみ", "路線廃止", "バス停削除", "減便", "代替路線追加")

if mode in SIM_MODES and st.sidebar.button("シミュレーション実行", type="primary"):
    with st.spinner("計算中..."):
        result_before, prev_before = engine.calc_isochrone(
            start_stop_id, start_time_sec, max_time_sec, track_path=True
        )

        m = folium.Map(
            location=origin_center, zoom_start=13,
            tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
            attr="&copy; OpenStreetMap contributors &copy; CARTO"
        )
        # 路線ルートを描画。読み込んだGTFSの shapes.txt を使う（以前は室蘭の既定データを
        # 常に読んでおり、他地域のGTFSでは路線の線が出ないか、室蘭の線が出ていた）
        shapes_path = os.path.join(engine.gtfs_dir, "shapes.txt")
        if os.path.exists(shapes_path) and "shape_id" in engine.trips.columns:
            shapes = pd.read_csv(shapes_path)
            trip_shape = engine.trips[["route_id", "shape_id"]].drop_duplicates()
        else:
            trip_shape = engine.trips.iloc[:0][["route_id"]].assign(shape_id=None)

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
                    reduced = engine.edges_after_frequency_reduction(
                        "half", target_route_id=reduce_target_route_id)
                    sim_label = f"減便（半減）: {reduce_target_route_name}"
                elif reduce_mode == "特定路線の便を間引く（N本に1本残す）":
                    reduced = engine.edges_after_frequency_reduction(
                        "interval", target_route_id=reduce_target_route_id,
                        keep_every=reduce_keep_every)
                    sim_label = f"減便（{reduce_keep_every}本に1本）: {reduce_target_route_name}"
                else:
                    reduced = engine.edges_after_frequency_reduction("all", reduce_ratio=reduce_ratio)
                    sim_label = f"全路線一律 {int(reduce_ratio*100)}%削減"
                with engine.scenario(*reduced):
                    result_after, prev_after = engine.calc_isochrone(
                        start_stop_id, start_time_sec, max_time_sec, track_path=True)
                # 指定した率と実際に減る便は一致しない（方向ごとの便数で四捨五入する）ので、実数を示す
                n_before = engine.bus_edges["trip_id"].nunique()
                n_removed = n_before - reduced[0]["trip_id"].nunique()
                sim_label += f"（実際に減った便 {n_removed}/{n_before}便・{100 * n_removed / n_before:.1f}%）"

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
                "start_time_sec": start_time_sec,
                "max_time_sec": max_time_sec,
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
                    sim_rb, sim_st, facilities, engine.stop_coords, max_time_sec=max_time_sec
                )
                acc_after = facility_data.calc_facility_access(
                    sim_ra, sim_st, facilities, engine.stop_coords, max_time_sec=max_time_sec
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
                        f"降りるバス停（変更前）: {ab['nearest_stop']}<br>"
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
        st.session_state.result_mode = mode

# ===== 時間帯別到達圏モード =====
# 出発時刻を5分刻みで動かし、時間帯ごとに「何通りの出発時刻で行けるか」を見る。
# 毎時00分の1点だけだと、5:00発（始発前）と5:55発で到達数が10と93になるように、
# 数分の違いが時間帯の差として表に出てしまう。
if mode == "時間帯別到達圏":
    if "animation_results" not in st.session_state:
        st.session_state.animation_results = None

    if st.sidebar.button("全時間帯を一括計算", type="primary"):
        with st.spinner("5〜22時台を5分刻みで計算中..."):
            times = [int(t) for t in arrival_times()]
            st.session_state.animation_results = {
                "times": times,
                "reach": {t: engine.calc_isochrone(start_stop_id, t, max_time_sec) for t in times},
                "origin": start_stop_name,
                "max_time_min": max_time_min,
                "day_type": day_type,
            }

    anim = st.session_state.animation_results
    if anim is not None:
        if (anim["origin"], anim["max_time_min"], anim["day_type"]) != (start_stop_name, max_time_min, day_type):
            st.warning(f"表示中の結果は「{anim['origin']}・{anim['max_time_min']}分・{anim['day_type']}ダイヤ」で"
                       "計算したものです。現在の設定で見るには「全時間帯を一括計算」を押し直してください。")
        times, reach = anim["times"], anim["reach"]
        limit = anim["max_time_min"]
        selected_hour = st.slider("出発時刻（時台）", 5, 22, 8)
        hour_times = [t for t in times if t // 3600 == selected_hour]
        n = len(hour_times)

        # 停留所ごとに、その時間帯の何通りの出発時刻から制限時間内に着けるか
        hit, fastest = {}, {}
        for t in hour_times:
            for stop_id, arrival in reach[t].items():
                hit[stop_id] = hit.get(stop_id, 0) + 1
                fastest[stop_id] = min(fastest.get(stop_id, float("inf")), (arrival - t) / 60)

        anim_origin = engine.get_stop_ids_by_name(anim["origin"])[0]
        m = folium.Map(
            location=engine.stop_coords.loc[anim_origin, ["stop_lat", "stop_lon"]].astype(float).tolist(),
            zoom_start=13,
            tiles="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
            attr="&copy; OpenStreetMap contributors &copy; CARTO"
        )
        for stop_id, k in hit.items():
            if stop_id not in engine.stop_coords.index:
                continue
            row = engine.stop_coords.loc[stop_id]
            color = "green" if k == n else "orange" if k * 2 >= n else "red"
            folium.CircleMarker(
                location=[row["stop_lat"], row["stop_lon"]],
                radius=6, color=color, fill=True, fill_opacity=0.8,
                popup=(f"{row['stop_name']}<br>{n}通りの出発時刻のうち{k}通りで{limit}分以内"
                       f"<br>最短 {fastest[stop_id]:.0f}分")
            ).add_to(m)

        start_row = engine.stop_coords.loc[anim_origin]
        folium.Marker(
            location=[start_row["stop_lat"], start_row["stop_lon"]],
            popup=f"出発: {start_row['stop_name']}",
            icon=folium.Icon(color="blue", icon="star")
        ).add_to(m)

        counts = [len(reach[t]) for t in hour_times]
        st.subheader(f"{selected_hour}時台に出発 — 到達できるバス停 平均{np.mean(counts):.0f}箇所")
        st.caption(f"{selected_hour}:00〜{selected_hour}:55発を5分刻みの{n}通りで計算。"
                   f"出発時刻によって {min(counts)}〜{max(counts)}箇所 の幅があります。")
        st_folium(m, width=1200, height=700, returned_objects=[])

        st.markdown("**出発時刻ごとの到達可能数（5分刻み）**")
        all_counts = np.array([len(reach[t]) for t in times], dtype=float)
        per = 60 // STEP_MIN
        chart_data = pd.DataFrame({
            "出発時刻（時）": [t / 3600 for t in times],
            "5分ごと": all_counts,
            "時間帯の平均": np.repeat(all_counts.reshape(-1, per).mean(axis=1), per),
        }).set_index("出発時刻（時）")
        st.line_chart(chart_data)
        st.caption("同じ時間帯でも、出発が数分違うだけで乗れる便が変わり、到達できる範囲は大きく上下します。"
                   "時間帯どうしを比べるときは平均を使います。")

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**凡例**")
            st.markdown(f"🟢 この時間帯ならいつ出ても{limit}分以内に着く　"
                        f"🟠 半分以上の出発時刻で着く　🔴 出発時刻しだい（半分未満）")

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
                    st.session_state.result_mode = mode
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
            # 施設まで歩く時間（歩行速度で変わる）も含めて制限時間と比べる
            access = facility_data.calc_facility_access(
                result, start_time_sec, facilities, engine.stop_coords,
                walk_speed=walk_speed, max_time_sec=max_time_sec
            )

            m = folium.Map(
                location=origin_center, zoom_start=13,
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
                        f"降りるバス停: {fac['nearest_stop']}<br>"
                        f"バス停から徒歩: {fac['walk_time_min']}分（{fac['walk_distance_m']}m）"
                    )
                else:
                    color = "red"
                    icon = "remove-sign"
                    if fac["total_time_min"] is None:
                        reason = "（徒歩圏のどのバス停にも制限時間内に着けない）"
                    else:
                        reason = f"（施設まで最短{fac['total_time_min']}分で、制限時間{max_time_min}分を超える）"
                    popup_text = (
                        f"<b>{fac['facility_name']}</b><br>"
                        f"❌ アクセス不可<br>"
                        f"最も早く着けるバス停: {fac['nearest_stop']}<br>"
                        f"バス停から徒歩: {fac['walk_time_min']}分（{fac['walk_distance_m']}m）<br>"
                        f"{reason}"
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
    st.caption(f"{blank_mesh_level}m四方ごとに、選んだ時間帯に拠点へ着けない人の数を柱の高さで表しています。"
               "到着時刻を5分刻みにして判定し、時間帯の中で平均しています。")

    if pop_data is None:
        st.warning("メッシュ人口データが読み込まれていないため、この分析は実行できません。")
    elif not blank_dest_ids:
        st.info("サイドバーで生活拠点のバス停を1つ以上選んでください。")
    else:
        analyzer = get_blank_analyzer(engine, pop_data, blank_walk_m, blank_walk_speed)
        warn_if_regions_disjoint(analyzer)

        def run_blank_diagnosis():
            st.session_state.blank_result = {
                "day": analyzer.diagnose_day(blank_dest_ids, max_time_sec),
                "dest_names": list(blank_dest_names),
                "dest_ids": list(blank_dest_ids),
                "max_time_min": max_time_min,
                "walk_m": blank_walk_m,
                "walk_speed": blank_walk_speed,
                "day_type": day_type,
            }

        # 起動直後に核心の結果が出ている状態にする。空のフォームで待たせると、
        # 短時間しか触らない相手には何のツールか伝わらない。5分刻みの216通りで約1秒。
        if "day" not in st.session_state.get("blank_result", {}):
            with st.spinner("5〜22時台の空白人口を5分刻みで計算中..."):
                run_blank_diagnosis()

        if st.sidebar.button("この条件で再計算", type="primary"):
            with st.spinner("5〜22時台の空白人口を5分刻みで計算中..."):
                run_blank_diagnosis()

        result = st.session_state.get("blank_result")
        if result is None:
            st.info("サイドバーの「この条件で再計算」を押してください。")
        else:
            day = result["day"]
            st.caption(
                f"{result.get('day_type', '?')}ダイヤ ／ 拠点: {'、'.join(result['dest_names'])} ／ "
                f"制限 {result['max_time_min']}分 ／ "
                f"徒歩 {result['walk_m']}m・{result['walk_speed']}m/分"
            )
            # ダイヤ以外の条件を変えても警告が出ず、画面の数値と設定が食い違っていた
            shown = (result["day_type"], result["dest_names"], result["max_time_min"],
                     result["walk_m"], result["walk_speed"])
            if shown != (day_type, list(blank_dest_names), max_time_min, blank_walk_m, blank_walk_speed):
                st.warning("表示中の結果は上の条件で計算したものです。"
                           "サイドバーの設定で見るには「この条件で再計算」を押してください。")
            sel_hour = st.slider(
                "到着時刻（時台）", day.hours[0], day.hours[-1], 9 if 9 in day.hours else day.hours[0],
                help="その時間帯の到着時刻（例 9:00〜9:55）を5分刻みで判定し、平均した人数を表示します。")
            s = day.hour_summary(sel_hour)
            df_sel = day.hour_mesh(sel_hour)
            # 静的空白（徒歩圏にバス停なし）は終日空白になるのが当たり前なので、
            # 深刻度（1日のうち着けない時間）は静的でないメッシュだけで数える。
            df_sel["blank_hours"] = np.where(df_sel["static_blank"], 0.0, day.blank_hours())
            df_sel["static_pop"] = np.where(df_sel["static_blank"], df_sel["pop"], 0.0)

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
            # 時間帯の平均なので端数が出る。表示が0人になる細い柱は立てない
            time_cells = cells[(cells["blank_pop"] - cells["static_pop"]) >= 0.5].copy()  # 暖色の柱
            time_cells[["r", "g", "b"]] = [sev_rgb(h) for h in time_cells["blank_hours"]]
            time_cells["blank_pop_disp"] = time_cells["blank_pop"].round().astype(int)
            time_cells["blank_elderly_disp"] = time_cells["blank_elderly"].round().astype(int)
            time_cells["time_disp"] = (time_cells["blank_pop"] - time_cells["static_pop"]).round().astype(int)
            span = f"{day.hours[0]}〜{day.hours[-1] + 1}時"
            longest = "（セル内で最も長い地点）" if blank_mesh_level == 500 else ""
            time_cells["tip"] = [
                f"空白人口 {p:,}人（{sel_hour}時台の平均）\nうち時間空白 {t:,}人\nうち高齢者 {e:,}人\n"
                f"{span}のうち着けない時間{longest} 約{h:.1f}時間"
                for p, t, e, h in zip(time_cells["blank_pop_disp"], time_cells["time_disp"],
                                      time_cells["blank_elderly_disp"], time_cells["blank_hours"])
            ]
            base["tip"] = [f"静的空白 {v:,}人（歩ける範囲にバス停がない）" for v in base["static_disp"]]

            # 生活拠点の停留所（計算に使ったもの）。同名の複数ポールはそれぞれ打つ
            hub_ids = [sid for sid in result.get("dest_ids", []) if sid in engine.stop_coords.index]
            hub = engine.stop_coords.loc[hub_ids, ["stop_lat", "stop_lon", "stop_name"]]
            hub = hub.rename(columns={"stop_lat": "lat", "stop_lon": "lon"}).reset_index(drop=True)
            hub["tip"] = "生活拠点: " + hub["stop_name"].astype(str)

            c1, c2, c3, c4 = st.columns(4)
            c1.metric(f"空白人口（{sel_hour}時台の平均）", f"{s['blank_pop']:,}人",
                      f"{100 * s['blank_pop'] / max(s['total_pop'], 1):.1f}%")
            c2.metric("うち高齢者", f"{s['blank_elderly']:,}人")
            c3.metric("静的空白（徒歩圏にバス停なし）", f"{s['static_blank_pop']:,}人")
            c4.metric("時間空白（バス停はあるが間に合わない）",
                      f"{s['blank_pop'] - s['static_blank_pop']:,}人")
            # 平均だけを出すと、数分の違いで大きく変わること（時間空白そのもの）が見えない
            st.caption(f"同じ{sel_hour}時台でも、到着時刻によって {s['blank_pop_min']:,}人"
                       f"（{clock(s['time_min'])}着）〜 {s['blank_pop_max']:,}人"
                       f"（{clock(s['time_max'])}着）の幅があります。")

            view = pdk.ViewState(
                latitude=float(df_sel["lat"].mean()),
                longitude=float(df_sel["lon"].mean()),
                zoom=11, pitch=50, bearing=0,
            )
            # 灰色の土台（静的空白）の上に、時間空白の柱（暖色・深刻度で色分け）を重ねる
            static_layer = pdk.Layer(
                "ColumnLayer",
                data=base[["lon", "lat", "static_pop", "tip"]],
                get_position=["lon", "lat"], get_elevation="static_pop",
                elevation_scale=blank_elev_scale, radius=radius,
                get_fill_color=[174, 184, 194, 200], pickable=True, auto_highlight=True,
            )
            time_layer = pdk.Layer(
                "ColumnLayer",
                data=time_cells[["lon", "lat", "blank_pop", "tip", "r", "g", "b"]],
                get_position=["lon", "lat"], get_elevation="blank_pop",
                elevation_scale=blank_elev_scale, radius=inner,
                get_fill_color=["r", "g", "b", 220], pickable=True, auto_highlight=True,
            )
            # 拠点は柱と見分けがつくよう、青い点を地面に打つ（拠点の周りは
            # 全員が間に合うので柱が立たず、隠れない）
            hub_layer = pdk.Layer(
                "ScatterplotLayer",
                data=hub[["lon", "lat", "tip"]],
                get_position=["lon", "lat"], get_radius=160, radius_min_pixels=6,
                get_fill_color=[37, 99, 235, 255], stroked=True,
                get_line_color=[255, 255, 255, 255], line_width_min_pixels=2,
                pickable=True,
            )
            st.pydeck_chart(pdk.Deck(
                layers=[static_layer, time_layer, hub_layer],
                initial_view_state=view,
                map_style="light",
                tooltip={"text": "{tip}"},
            ))

            st.caption("灰色の土台：静的空白（歩ける範囲にバス停がない） ／ "
                       "暖色の柱：時間空白（色が濃いほど、1日のうち着けない時間が長い） ／ "
                       f"柱の高さ：そこに住んでいて拠点に着けない人数（{sel_hour}時台の平均） ／ "
                       "青い点：生活拠点")

            st.markdown("**到着時刻ごとの空白人口（5分刻み）**")
            hourly_mean, _ = day.hourly_blank()
            series = day.series()
            chart = pd.DataFrame({
                "到着時刻（時）": series["time_sec"] / 3600,
                "5分ごと": series["blank_pop"].round(),
                "時間帯の平均": np.repeat(hourly_mean, day.per_hour).round(),
            }).set_index("到着時刻（時）")
            st.line_chart(chart)
            st.caption("到着時刻が数分違うだけで便に間に合うかが変わり、空白人口は大きく上下します。"
                       "時間帯の値は、この上下を平均したものです。")

# ===== 改善策モード =====
if mode == "改善策":
    st.subheader("どの改善策が空白を減らすか")
    st.caption("診断で見つけた時間空白に対し、増便とダイヤシフトの効果を同じ指標（空白人口）で比較します。")

    if pop_data is None:
        st.warning("メッシュ人口データが読み込まれていないため、この分析は実行できません。")
    elif not blank_dest_ids:
        st.info("サイドバーで生活拠点のバス停を1つ以上選んでください。")
    else:
        analyzer = get_blank_analyzer(engine, pop_data, blank_walk_m, blank_walk_speed)
        warn_if_regions_disjoint(analyzer)
        # 対象の時間帯だけを5分刻みで診断する（12通りで0.1秒程度）。
        # 空白地域（候補路線を選ぶ範囲）は、その時間帯に一度でも着けない時刻があるメッシュ。
        hour_diag = analyzer.diagnose_day(blank_dest_ids, max_time_sec, hours=[presc_hour])
        diag = hour_diag.hour_mesh(presc_hour)
        summary = hour_diag.hour_summary(presc_hour)
        # 比較表がどの条件で作られたかを照合する鍵（条件を変えたあとの古い表で文書を作らない）
        presc_conditions = (day_type, tuple(blank_dest_names), presc_hour, max_time_min,
                            blank_walk_m, blank_walk_speed)

        c1, c2, c3 = st.columns(3)
        c1.metric(f"{presc_hour}時台に着けない人口（平均）", f"{summary['blank_pop']:,}人",
                  f"{100*summary['blank_pop']/max(summary['total_pop'],1):.1f}%")
        c2.metric("うち高齢者", f"{summary['blank_elderly']:,}人")
        c3.metric("うち時間空白（施策で減らせる分）",
                  f"{summary['blank_pop'] - summary['static_blank_pop']:,}人")
        st.caption(f"{day_type}ダイヤ ／ 拠点: {'、'.join(blank_dest_names)} ／ 制限 {max_time_min}分 ／ "
                   f"徒歩 {blank_walk_m}m・{blank_walk_speed}m/分 ／ "
                   f"到着時刻により {summary['blank_pop_min']:,}〜{summary['blank_pop_max']:,}人")

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
                hub = tuple(blank_dest_ids)
                plans = []
                for rid in picked:
                    label = str(names.loc[rid])[:18]
                    plans.append(Plan(
                        f"増便 {label}（拠点{inc_window[0]}-{inc_window[1]}時 +{inc_trips}便/パターン）",
                        lambda e, x=rid: e.edges_after_frequency_increase(
                            x, inc_window[0], inc_window[1], inc_trips, via_stops=hub)))
                    plans.append(Plan(
                        f"シフト {label}（{shift_from[0]}-{shift_from[1]}時→{inc_window[0]}-{inc_window[1]}時 {shift_trips}便）",
                        lambda e, x=rid: e.edges_after_timetable_shift(
                            x, shift_from, inc_window, shift_trips, via_stops=hub)))
                if len(picked) > 1:
                    def _multi(e, ids=tuple(picked)):
                        bus, walk = e.bus_edges, e.walk_edges
                        for x in ids:
                            with e.scenario(bus, walk):
                                bus, walk = e.edges_after_frequency_increase(
                                    x, inc_window[0], inc_window[1], inc_trips, via_stops=hub)
                        return bus, walk
                    plans.append(Plan(f"増便 選んだ{len(picked)}路線すべて", _multi))

                with st.spinner(f"{len(plans)}件の施策を、5〜22時台の到着時刻（5分刻み）で計算中..."):
                    st.session_state.presc_table = compare(
                        engine, analyzer, blank_dest_ids, plans,
                        arrival_hour=presc_hour, max_time_sec=max_time_sec)
                # 比較表の列名は対象の時間帯を含む（例「7時台の削減」）。あとから条件を
                # 変えた状態の表を政策文書に使うと条件と数値が食い違うので、
                # どの条件で作った表かを覚えておく。
                st.session_state.presc_hour_used = presc_hour
                st.session_state.presc_conditions = presc_conditions
                st.session_state.pop("presc_doc_refined", None)

            table = st.session_state.get("presc_table")
            if table is not None:
                st.markdown("**施策の比較（全日の削減が大きい順）**")
                stale = st.session_state.get("presc_conditions") != presc_conditions
                if stale:
                    st.warning("この表は現在とは別の条件（時間帯・拠点・制限時間・徒歩・ダイヤ）で計算したものです。"
                               "現在の設定で見るには「施策を比較する」を押し直してください。")
                st.dataframe(table, hide_index=True, width="stretch")
                st.caption(
                    f"「{st.session_state.get('presc_hour_used')}時台の空白・削減」はその時間帯の到着時刻を"
                    "5分刻みにした平均（人）。「全日の削減」は5〜22時台の空白人口の減少を時間で積み上げた値"
                    "（人・時間。100人が1時間着けなかった状態を解消すると100人・時間）。"
                    "ダイヤシフトは便を移すだけなので狙った時間帯は必ず良くなりますが、便を抜いた時間帯は"
                    "悪化します。狙った時間帯だけで見ると『費用ゼロで改善』に見えてしまうため全日で評価しています。"
                    "「運行キロ増」は費用の代理値（追加便数×路線長）で、実際の単価を掛ければ金額になります。"
                )
                worsen = table[table["全日の削減"] < 0]
                if not worsen.empty:
                    st.warning("全日で見ると逆効果になる施策があります: "
                               + "、".join(worsen["施策"].tolist()))

                # ===== 政策文書の下書き =====
                st.markdown("---")
                st.subheader("政策文書の下書き")
                if stale:
                    st.info("表は現在とは別の条件で作成されています。"
                            "現在の設定で「施策を比較する」を押し直すと文書を作成できます。")
                else:
                    st.caption("上の数値をそのまま文章にしたものです。生成AIは使っていないため、"
                               "記載の数値はすべてこの画面の計算結果と一致します。")
                    doc = policy_report.build_document(policy_report.ReportContext(
                        region=policy_report.region_from_population_path(
                            getattr(pop_data, "source_path", None)),
                        day_type=day_type,
                        dest_names=blank_dest_names,
                        arrival_hour=presc_hour,
                        max_time_min=max_time_min,
                        walk_m=blank_walk_m,
                        walk_speed=blank_walk_speed,
                        summary=summary,
                        table=table,
                        candidates=candidates,
                        sources=policy_report.sources_for(
                            engine.feed_info(), getattr(pop_data, "source_path", None)),
                    ))
                    st.download_button("下書きをダウンロード（Markdown）", doc,
                                       file_name=f"政策文書_{presc_hour}時.md",
                                       mime="text/markdown")
                    with st.expander("下書きを読む", expanded=False):
                        st.markdown(doc)

                    with st.expander("生成AIで文章を整える（任意・自分のAPIキーが必要）"):
                        st.caption(
                            "数値の計算にAIは関与しません。上の下書きの文章だけを整えます。"
                            "整えたあとに「下書きに無い数値が増えていないか」を機械的に照合するので、"
                            "AIが数字を作っていないことを確認できます。"
                        )
                        api_key = st.text_input(
                            "Anthropic APIキー", type="password",
                            help="このアプリは入力されたキーを保存も送信先の変更もしません。"
                                 "利用料は入力した方の契約に課金されます。")
                        ai_model = st.selectbox(
                            "モデル", list(policy_report.MODEL_CHOICES),
                            format_func=lambda m: policy_report.MODEL_CHOICES[m])
                        ai_note = st.text_area(
                            "追加の指示（任意）", placeholder="例: 議会説明用に、結論を先に書いてください。",
                            height=80)
                        if st.button("文章を整える"):
                            if not api_key.strip():
                                st.warning("APIキーを入力してください。")
                            else:
                                try:
                                    with st.spinner("生成中..."):
                                        refined = policy_report.refine(
                                            doc, api_key, model=ai_model,
                                            extra_instruction=ai_note)
                                    st.session_state.presc_doc_refined = refined
                                except Exception as e:
                                    st.error(f"整形できませんでした: {e}")

                        refined = st.session_state.get("presc_doc_refined")
                        if refined:
                            extra = policy_report.unverified_numbers(doc, refined)
                            if extra:
                                st.warning("下書きに無い数値が現れました。"
                                           "そのまま配布せず、該当箇所を確認してください: "
                                           + "、".join(extra[:20]))
                            else:
                                st.success("数値は下書きと一致しています（新たな数値はありません）。")
                            st.download_button("整えた文書をダウンロード（Markdown）", refined,
                                               file_name=f"政策文書_{presc_hour}時_整形済み.md",
                                               mime="text/markdown")
                            st.markdown(refined)

# ===== 結果表示 =====
# 結果は、それを作ったモードを開いているときだけ出す。以前はモードを切り替えても
# 前の結果（例: 減便の地図と影響分析）が施設アクセスなど別のモードの下に出続けていた。
if (st.session_state.result_map is not None
        and st.session_state.get("result_mode") == mode):
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
                sim_limit = stats.get("max_time_sec")
                for ftype in facility_data.facility_types:
                    facilities = facility_data.get_facilities_by_type(ftype)
                    access_before = facility_data.calc_facility_access(
                        sim_result_before, sim_start_time, facilities, engine.stop_coords,
                        max_time_sec=sim_limit
                    )
                    access_after = facility_data.calc_facility_access(
                        sim_result_after, sim_start_time, facilities, engine.stop_coords,
                        max_time_sec=sim_limit
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
                        sim_result_before, sim_start_time, facilities, engine.stop_coords,
                        max_time_sec=sim_limit
                    )
                    access_after = facility_data.calc_facility_access(
                        sim_result_after, sim_start_time, facilities, engine.stop_coords,
                        max_time_sec=sim_limit
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
