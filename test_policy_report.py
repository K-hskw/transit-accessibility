"""政策文書生成（決定的な下書き＋任意のAI整形）のテスト

AIを呼ぶ経路はテストしない（課金と外部依存が入るため）。テストするのは
「キーが無くても文書が出ること」と「AIに数値の責任を負わせない仕組み」
＝ unverified_numbers の照合が働くことの2点。
"""
import pandas as pd

import policy_report as pr
from transit_engine import TransitEngine
from population import PopulationData
from blank_area import BlankAreaAnalyzer
from prescription import Plan, compare, rank_routes_by_blank_coverage

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


print("=" * 70)
print("政策文書生成テスト")
print("=" * 70)

# ===== 1. 自治体名の取り出し =====
print("\n--- 1. 自治体名 ---")
test("e-Stat形式のファイル名から市名を取れる",
     pr.region_from_population_path("100m_mesh_pop2020_01205室蘭市.csv") == "室蘭市",
     pr.region_from_population_path("100m_mesh_pop2020_01205室蘭市.csv"))
test("フルパスでも取れる",
     pr.region_from_population_path(r"C:\data\500m_mesh_pop2020_01204旭川市.csv") == "旭川市",
     pr.region_from_population_path(r"C:\data\500m_mesh_pop2020_01204旭川市.csv"))
test("町村でも取れる",
     pr.region_from_population_path("100m_mesh_pop2020_01543白老町.csv") == "白老町",
     pr.region_from_population_path("100m_mesh_pop2020_01543白老町.csv"))
test("読めない名前は既定値に落ちる",
     pr.region_from_population_path("mesh.csv") == "対象地域",
     pr.region_from_population_path("mesh.csv"))
test("None でも落ちない", pr.region_from_population_path(None) == "対象地域")

# ===== 2. 数値の照合 =====
print("\n--- 2. 数値の照合 ---")
test("桁区切りを外して拾う", "1234" in pr.extract_numbers("空白人口は1,234人です"))
test("小数も拾う", "12.5" in pr.extract_numbers("割合は12.5%"))
test("同じ数値なら差分は空",
     pr.unverified_numbers("空白は1,234人、46.1%です", "空白人口は 1234 人（46.1%）に達する。") == [],
     str(pr.unverified_numbers("空白は1,234人、46.1%です", "空白人口は1234人（46.1%）")))
test("AIが数値を足すと検出する",
     pr.unverified_numbers("空白は1,234人です", "空白は1,234人で、前年比8%増です") == ["8"],
     str(pr.unverified_numbers("空白は1,234人です", "空白は1,234人で、前年比8%増です")))
test("AIが概数に書き換えると検出する",
     "1200" in pr.unverified_numbers("空白は1,234人です", "空白は約1,200人です"))
test("数値を減らすのは検出対象外（削除は照合しない）",
     pr.unverified_numbers("空白は1,234人、46.1%です", "空白は1,234人です") == [])

# ===== 3. 推奨の選び方（合成データ） =====
print("\n--- 3. 推奨の選び方 ---")
synth = pd.DataFrame([
    {"施策": "現状（何もしない）", "7時の空白": 1000, "7時の削減": 0, "全日の削減": 0,
     "全日うち高齢者": 0, "悪化した時間帯": 0, "最大悪化": 0, "追加便数": 0,
     "運行キロ増": 0.0, "1人あたり運行キロ": None},
    {"施策": "増便A", "7時の空白": 600, "7時の削減": 400, "全日の削減": 3000,
     "全日うち高齢者": 900, "悪化した時間帯": 0, "最大悪化": 0, "追加便数": 12,
     "運行キロ増": 120.0, "1人あたり運行キロ": 0.04},
    {"施策": "増便B", "7時の空白": 800, "7時の削減": 200, "全日の削減": 1000,
     "全日うち高齢者": 300, "悪化した時間帯": 0, "最大悪化": 0, "追加便数": 3,
     "運行キロ増": 20.0, "1人あたり運行キロ": 0.02},
    {"施策": "シフトC", "7時の空白": 700, "7時の削減": 300, "全日の削減": -500,
     "全日うち高齢者": -100, "悪化した時間帯": 5, "最大悪化": 400, "追加便数": 0,
     "運行キロ増": 0.0, "1人あたり運行キロ": None},
])
rec = pr.recommend(synth, 7)
test("全日の削減が最大の施策を推奨する", rec["best"]["施策"] == "増便A", str(rec["best"]["施策"]))
test("費用効率が良い別案も拾う", rec["cheapest"]["施策"] == "増便B", str(rec["cheapest"]))
test("全日で悪化する施策を注意として挙げる",
     rec["harmful"] is not None and list(rec["harmful"]["施策"]) == ["シフトC"])
test("現状は施策として数えない", rec["n_plans"] == 3, str(rec["n_plans"]))

# 全施策が悪化する場合は推奨を出してはいけない
bad = synth[synth["施策"].isin(["現状（何もしない）", "シフトC"])]
rec_bad = pr.recommend(bad, 7)
test("純改善が無ければ推奨を出さない", rec_bad["best"] is None)
test("純改善が無くても注意は出す", rec_bad["harmful"] is not None)

# 運行キロを増やさずに改善する施策は別枠で拾う
free_tbl = synth.copy()
free_tbl.loc[free_tbl["施策"] == "シフトC", ["全日の削減", "全日うち高齢者"]] = [200, 50]
rec_free = pr.recommend(free_tbl, 7)
test("増キロ0で改善する施策を拾う",
     rec_free["free"] is not None and "シフトC" in list(rec_free["free"]["施策"]))
test("副作用のある施策を拾う",
     rec_free["side_effect"] is not None and "シフトC" in list(rec_free["side_effect"]["施策"]))

# 効果が同点なら費用の小さいほうを推奨する（表の並び順で決めてはいけない）
tie_tbl = synth.copy()
tie_tbl.loc[tie_tbl["施策"] == "増便B", "全日の削減"] = 3000
rec_tie = pr.recommend(tie_tbl, 7)
test("同点なら運行キロが小さいほうを推奨する",
     rec_tie["best"]["施策"] == "増便B", str(rec_tie["best"]["施策"]))
test("同点の件数を数える", rec_tie["tied"] == 2, str(rec_tie["tied"]))
tie_doc = pr.build_document(pr.ReportContext(
    region="X市", day_type="平日", dest_names=["A"], arrival_hour=7, max_time_min=60,
    walk_m=500, walk_speed=67,
    summary={"blank_pop": 1000, "blank_elderly": 300, "blank_mesh": 10,
             "total_pop": 5000, "static_blank_pop": 200},
    table=tie_tbl))
test("同点があることを文書に書く", "同じ削減効果となる施策が2件あります" in tie_doc)

# 狙った時刻が改善しない施策を「改善」と書かない
flat = synth[synth["施策"].isin(["現状（何もしない）", "増便A"])].copy()
flat.loc[flat["施策"] == "増便A", "7時の削減"] = 0
flat.loc[flat["施策"] == "増便A", "7時の空白"] = 1000
flat_doc = pr.build_document(pr.ReportContext(
    region="X市", day_type="平日", dest_names=["A"], arrival_hour=7, max_time_min=60,
    walk_m=500, walk_speed=67,
    summary={"blank_pop": 1000, "blank_elderly": 300, "blank_mesh": 10,
             "total_pop": 5000, "static_blank_pop": 200},
    table=flat))
test("狙った時刻が変わらない施策を『減る』と書かない",
     "0人減り" not in flat_doc and "変わりません" in flat_doc,
     [l for l in flat_doc.split("\n") if "0人減り" in l])

# ===== 4. 実データで文書を組み立てる =====
print("\n--- 4. 実データでの文書生成 ---")
engine = TransitEngine()
pop_data = PopulationData("100m_mesh_pop2020_01205室蘭市.csv")
analyzer = BlankAreaAnalyzer(engine, pop_data, max_walk_m=500, walk_speed_m_min=67)
dest = engine.get_stop_ids_by_name("東室蘭駅東口")
LIM = 3600
HOUR = 7
engine.set_day_type("日祝")

test("人口データが読み込み元のパスを覚えている",
     getattr(pop_data, "source_path", None) == "100m_mesh_pop2020_01205室蘭市.csv",
     str(getattr(pop_data, "source_path", None)))

diag = analyzer.diagnose(dest, HOUR * 3600, LIM)
summary = analyzer.summarize(diag)
candidates = rank_routes_by_blank_coverage(engine, analyzer, diag)
picked = list(candidates.head(2)["route_id"])
plans = []
for rid in picked:
    label = str(candidates.set_index("route_id").loc[rid, "route_name"])[:18]
    plans.append(Plan(f"増便 {label}",
                      lambda e, x=rid: e.edges_after_frequency_increase(x, 5, 9, 1)))
    plans.append(Plan(f"シフト {label}",
                      lambda e, x=rid: e.edges_after_timetable_shift(x, (10, 15), (5, 9), 2)))
table = compare(engine, analyzer, dest, plans, arrival_hour=HOUR, max_time_sec=LIM)

ctx = pr.ReportContext(
    region=pr.region_from_population_path(pop_data.source_path),
    day_type="日祝", dest_names=["東室蘭駅東口"], arrival_hour=HOUR,
    max_time_min=60, walk_m=500, walk_speed=67,
    summary=summary, table=table, candidates=candidates,
)
doc = pr.build_document(ctx)
print(f"  文書長: {len(doc):,}文字 / 施策 {len(table)-1}件")

test("APIキー無しで文書が出る", isinstance(doc, str) and len(doc) > 500, str(len(doc)))
test("自治体名が見出しに入る", doc.startswith("# 室蘭市"), doc[:20])
for sec in ["## 1. 要旨", "## 2. 現状の診断", "## 3. 比較した施策",
            "## 4. 推奨", "## 5. 前提条件と限界", "## 6. データ出典"]:
    test(f"節がある: {sec}", sec in doc)

test("空白人口が本文に入る", f"{summary['blank_pop']:,}人" in doc,
     f"{summary['blank_pop']:,}")
test("静的空白と時間空白を分けて書く",
     f"{summary['static_blank_pop']:,}人" in doc
     and f"{summary['blank_pop'] - summary['static_blank_pop']:,}人" in doc)
test("条件（ダイヤ・拠点・徒歩）が明記される",
     "日祝" in doc and "東室蘭駅東口" in doc and "徒歩500m" in doc)
test("出典が書かれている", "GTFS-JP" in doc and "国勢調査" in doc)
test("nan が漏れていない", "nan" not in doc.lower().replace("financial", ""),
     [l for l in doc.split("\n") if "nan" in l.lower()][:2])
test("None が漏れていない", "None" not in doc,
     [l for l in doc.split("\n") if "None" in l][:2])
test("比較表の全施策が文書に載る",
     all(str(n) in doc for n in table["施策"]),
     str([n for n in table["施策"] if str(n) not in doc]))

# 全日で悪化する施策があるなら、必ず注意として書かれていること
worsen = table[table["全日の削減"] < 0]
if not worsen.empty:
    test("全日で悪化する施策は注意として明記される",
         "注意を要する施策" in doc and all(str(n) in doc for n in worsen["施策"]),
         str(list(worsen["施策"])))
    print(f"  全日で悪化する施策 {len(worsen)}件を注意として記載")
else:
    print("  （今回の条件では全日で悪化する施策なし）")

rec_real = pr.recommend(table, HOUR)
if rec_real["best"] is not None:
    test("推奨施策が本文に名指しされる", str(rec_real["best"]["施策"]) in doc)
    print(f"  推奨: {rec_real['best']['施策']}（全日 {int(rec_real['best']['全日の削減']):,}人）")
else:
    test("推奨が無い場合はその旨を書く", "推奨できる施策はありません" in doc)
    print("  推奨できる施策なし（その旨を記載）")

# 文書の数値は下書き自身に含まれるので、自己照合は必ず空になる
test("自己照合は空になる", pr.unverified_numbers(doc, doc) == [])

# 拠点未設定・候補なしでも落ちないこと
ctx2 = pr.ReportContext(
    region="対象地域", day_type="平日", dest_names=[], arrival_hour=HOUR,
    max_time_min=60, walk_m=500, walk_speed=67,
    summary=summary, table=table.iloc[:1], candidates=None,
)
doc2 = pr.build_document(ctx2)
test("施策ゼロ・拠点未設定でも文書が出る", len(doc2) > 300 and "（未設定）" in doc2)
test("施策ゼロなら推奨を出さない", "推奨できる施策はありません" in doc2)

# ===== 5. APIキーの扱い =====
print("\n--- 5. APIキーの扱い ---")
try:
    pr.refine(doc, "")
    test("空のキーは拒否する", False, "例外が出なかった")
except ValueError:
    test("空のキーは拒否する", True)
except Exception as e:
    test("空のキーは拒否する", False, f"想定外の例外: {type(e).__name__}")

test("既定モデルは claude-opus-5", pr.DEFAULT_MODEL == "claude-opus-5", pr.DEFAULT_MODEL)
test("モデル選択肢に既定が含まれる", pr.DEFAULT_MODEL in pr.MODEL_CHOICES)
src = open("policy_report.py", encoding="utf-8").read()
test("キーを保存する処理が無い",
     "open(" not in src.split("def refine")[1] and "environ[" not in src)
test("AIへの指示が数値の改変を禁じている",
     "下書きに無い数値を書かない" in pr.SYSTEM_PROMPT)

print("\n" + "=" * 70)
print(f"テスト結果: {tests_passed}/{tests_run} パス")
if errors:
    print(f"\nエラー: {len(errors)}件")
    for e in errors:
        print(f"  {e}")
else:
    print("全テスト合格！問題なし。")
print("=" * 70)
