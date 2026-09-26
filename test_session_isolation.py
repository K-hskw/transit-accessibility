"""公開環境で訪問者どうしが干渉しないことのテスト

Streamlit Cloud は1つのプロセスを全訪問者で共有する。以前は cache_resource の
エンジンを全員で直接使っていたため、訪問者Aの処方計算中に訪問者Bが診断を開くと、
室蘭の平日9時の空白人口が 19,867人ではなく 77,690人と表示された（再現済み）。
"""
import hashlib
import io
import os
import tempfile
import threading
import zipfile

import session_data
from transit_engine import TransitEngine
from population import PopulationData
from blank_area import BlankAreaAnalyzer

errors = []
tests_run = 0
tests_passed = 0


def test(name, condition, detail=""):
    global tests_run, tests_passed
    tests_run += 1
    if condition:
        tests_passed += 1
    else:
        errors.append(f"FAIL: {name} - {detail}")
        print(f"  FAIL: {name} - {detail}")


def md5(path):
    return hashlib.md5(open(path, "rb").read()).hexdigest()


print("=" * 70)
print("訪問者どうしの分離テスト")
print("=" * 70)

base = TransitEngine()            # cache_resource で共有されるもの
pop = PopulationData("100m_mesh_pop2020_01205室蘭市.csv")
dest = base.get_stop_ids_by_name("東室蘭駅東口")

# ===== 1. ダイヤ種別の分離 =====
print("\n--- 1. ダイヤ種別 ---")
a, b = base.session_copy(), base.session_copy()
a.set_day_type("日祝")
test("Aが日祝にしてもBは平日のまま", b.day_type == "平日", b.day_type)
test("Aが日祝にしても共有元は平日のまま", base.day_type == "平日", base.day_type)
test("Aの便数は日祝のもの",
     a.bus_edges["trip_id"].nunique() < base.bus_edges["trip_id"].nunique(),
     f"{a.bus_edges['trip_id'].nunique()} vs {base.bus_edges['trip_id'].nunique()}")

# ===== 2. 読み込みデータは共有している（メモリを倍にしない） =====
print("\n--- 2. 共有と複製 ---")
test("全便エッジは共有", a.all_bus_edges is base.all_bus_edges)
test("停留所座標は共有", a.stop_coords is base.stop_coords)
test("路線長キャッシュは別物", a._route_length_cache is not base._route_length_cache)
rid = base.bus_edges["route_id"].dropna().iloc[0]
before = dict(base._route_length_cache)
b.route_length_km(rid)
test("Bが路線長を計算しても共有元のキャッシュは変わらない",
     base._route_length_cache == before, str(len(base._route_length_cache)))

# ===== 3. 処方計算中の干渉（再現していた不具合） =====
print("\n--- 3. 処方計算中に別の訪問者が診断する ---")
a, b = base.session_copy(), base.session_copy()
an_a = BlankAreaAnalyzer(a, pop, max_walk_m=500, walk_speed_m_min=67)
an_b = BlankAreaAnalyzer(b, pop, max_walk_m=500, walk_speed_m_min=67)
f_b = lambda: an_b.summarize(an_b.diagnose(dest, 9 * 3600, 3600))["blank_pop"]
truth = f_b()

inside, done = threading.Event(), threading.Event()
seen = {}


def visitor_a():
    with a.scenario(a.bus_edges.iloc[:0], None):      # バス全廃のシナリオを計算中
        seen["a"] = an_a.summarize(an_a.diagnose(dest, 9 * 3600, 3600))["blank_pop"]
        inside.set()
        done.wait(30)


def visitor_b():
    inside.wait(30)
    seen["b"] = f_b()
    done.set()


ta, tb = threading.Thread(target=visitor_a), threading.Thread(target=visitor_b)
ta.start(); tb.start(); ta.join(); tb.join()
print(f"  Bが単独で見る値 {truth:,} / Aの計算中にBが見た値 {seen['b']:,} / Aのシナリオ値 {seen['a']:,}")
test("Aのシナリオは実際に結果を変えている（テストが空振りしていない）",
     seen["a"] > truth, f"{seen['a']} vs {truth}")
test("Aの計算中でもBの結果は変わらない", seen["b"] == truth, f"{seen['b']} vs {truth}")
test("Aのシナリオ後、Aは元の状態に戻る",
     an_a.summarize(an_a.diagnose(dest, 9 * 3600, 3600))["blank_pop"] == truth)

# ===== 4. エッジの読み込み元 =====
print("\n--- 4. edges_dir ---")
explicit = TransitEngine(gtfs_dir="gtfs_data", edges_dir=".")
test("edges_dir='.' は既定と同じエッジを読む",
     len(explicit.all_bus_edges) == len(base.all_bus_edges)
     and len(explicit.walk_edges) == len(base.walk_edges))

# ===== 5. アップロードは訪問者の作業ディレクトリに閉じる =====
print("\n--- 5. GTFSアップロード ---")
shared = {f: md5(f) for f in ("bus_edges.csv", "walk_edges.csv")}
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
    for fname in os.listdir("gtfs_data"):
        z.write(os.path.join("gtfs_data", fname), arcname=f"muroran/{fname}")
buf.seek(0)

work = session_data.new_work_dir()
up = session_data.build_uploaded_engine(buf, work)
test("共有の bus_edges.csv を書き換えない", md5("bus_edges.csv") == shared["bus_edges.csv"])
test("共有の walk_edges.csv を書き換えない", md5("walk_edges.csv") == shared["walk_edges.csv"])
test("共有ディレクトリ gtfs_data_custom を作らない", not os.path.exists("gtfs_data_custom"))
test("エッジは作業ディレクトリに作られる",
     os.path.exists(os.path.join(work, "bus_edges.csv")))
test("サブフォルダ入りのZIPでも見つける", up.stops is not None and len(up.stops) == len(base.stops))
test("同じGTFSなら既定と同じ便数になる",
     up.all_bus_edges["trip_id"].nunique() == base.all_bus_edges["trip_id"].nunique(),
     f"{up.all_bus_edges['trip_id'].nunique()} vs {base.all_bus_edges['trip_id'].nunique()}")
an_up = BlankAreaAnalyzer(up, pop, max_walk_m=500, walk_speed_m_min=67)
test("同じGTFSなら空白人口も既定と一致する",
     an_up.summarize(an_up.diagnose(dest, 9 * 3600, 3600))["blank_pop"] == truth)
session_data.remove_work_dir(work)
test("作業ディレクトリを消せる", not os.path.exists(work))

# ===== 6. 不正なZIP =====
print("\n--- 6. 不正なZIP ---")


def expect_error(name, make_zip, match=""):
    work = session_data.new_work_dir()
    try:
        session_data.extract_gtfs(make_zip(), work)
        test(name, False, "例外が出なかった")
    except ValueError as e:
        test(name, match in str(e), str(e))
    finally:
        session_data.remove_work_dir(work)


def zip_of(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, content in files.items():
            z.writestr(n, content)
    b.seek(0)
    return b


expect_error("stops.txt が無いZIPを拒否する", lambda: zip_of({"readme.txt": "x"}), "見つかりません")
expect_error("calendar.txt 等が欠けたZIPを拒否する",
             lambda: zip_of({"stops.txt": "x", "stop_times.txt": "x"}), "必要なファイル")

saved = session_data.MAX_UNZIPPED_BYTES
session_data.MAX_UNZIPPED_BYTES = 100
expect_error("展開後サイズの上限を超えるZIPを拒否する",
             lambda: zip_of({"stops.txt": "x" * 1000}), "大きすぎ")
session_data.MAX_UNZIPPED_BYTES = saved

# 展開先の外へのファイル書き込み（../ を含む名前）
outside = os.path.join(tempfile.gettempdir(), "kuhaku_escape_test.txt")
if os.path.exists(outside):
    os.remove(outside)
work = session_data.new_work_dir()
try:
    session_data.extract_gtfs(zip_of({"../../../kuhaku_escape_test.txt": "x", "a.txt": "x"}), work)
except ValueError:
    pass
test("../ を含む名前でも展開先の外に書かない", not os.path.exists(outside))
session_data.remove_work_dir(work)

test("ファイル名からディレクトリ部分を落とす",
     session_data.safe_filename("../../evil/100m_mesh_pop2020_01204旭川市.csv")
     == "100m_mesh_pop2020_01204旭川市.csv")
test("空や .. のファイル名は既定値にする",
     session_data.safe_filename("..", "d.csv") == "d.csv"
     and session_data.safe_filename("", "d.csv") == "d.csv")

print("\n" + "=" * 70)
print(f"テスト結果: {tests_passed}/{tests_run} パス")
if errors:
    print(f"\nエラー: {len(errors)}件")
    for e in errors:
        print(f"  {e}")
else:
    print("全テスト合格！問題なし。")
print("=" * 70)
