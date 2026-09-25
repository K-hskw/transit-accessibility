"""処方の比較結果を自治体向けの政策文書ドラフトにする

数値はすべてこのモジュールが決定的に組み立てる。生成AIは任意で、出来上がった
文書を自然な日本語に整えるためだけに使う（APIキーは利用者が入力し、保存しない）。
キーが無くても文書は必ず出るので、審査や説明の場でAPIの不調に左右されない。
計画書の「数値は検証可能、AIは通訳」という役割分担をそのまま実装にしている。

AIに通した場合も unverified_numbers() で「下書きに無い数値が増えていないか」を
機械的に照合できる。これが空なら、文章は変わっても数値は下書きのままだと言える。
"""
import os
import re
from dataclasses import dataclass, field

import pandas as pd

DEFAULT_MODEL = "claude-opus-5"

MODEL_CHOICES = {
    "claude-opus-5": "Claude Opus 5（既定・最も丁寧）",
    "claude-sonnet-5": "Claude Sonnet 5（速い・安い）",
    "claude-haiku-4-5": "Claude Haiku 4.5（最も安い）",
}

SYSTEM_PROMPT = """あなたは自治体の交通政策担当者に向けた文書を整える編集者です。
分析ツールが出力した下書きを受け取り、そのまま配布できる日本語に整えてください。

厳守すること:
1. 数値・単位・固有名詞を変更、追加、削除しない。下書きに無い数値を書かない。
   割合や合計を自分で計算し直さない。概数への言い換え（例: 1,234人→約1,200人）もしない。
2. 事実を足さない。原因の推測、他自治体の事例、法令・補助制度・予算額への言及を
   勝手に加えない。下書きに書かれていないことは書かない。
3. 見出しの構成と順序は維持する。表はそのまま残す。
4. 編集の範囲は、箇条書きを読みやすい文章にする、語調と敬体を統一する、
   冗長な表現を削る、専門用語に短い補足を添える、までとする。
5. 出力はMarkdown本文のみ。前置き・後書き・作業の説明は書かない。"""

# ===== 地域名 =====

def region_from_population_path(path, default="対象地域"):
    """メッシュ人口CSVのファイル名から自治体名を取り出す

    e-Stat由来の "100m_mesh_pop2020_01205室蘭市.csv" のように、団体コードの
    直後に自治体名が入る命名を想定する。合わなければ default を返すだけで、
    文書生成そのものは止めない。
    """
    if not path:
        return default
    name = os.path.splitext(os.path.basename(str(path)))[0]
    m = re.search(r"\d{5}([^\d_]+)$", name)
    if m:
        return m.group(1)
    m = re.search(r"([^\W\d_]+?[市町村区])", name)
    return m.group(1) if m else default


# ===== 文書の材料 =====

@dataclass
class ReportContext:
    """政策文書に必要な条件と結果をまとめたもの"""
    region: str
    day_type: str
    dest_names: list
    arrival_hour: int
    max_time_min: int
    walk_m: int
    walk_speed: int
    summary: dict                      # BlankAreaAnalyzer.summarize() の戻り値
    table: pd.DataFrame                # prescription.compare() の戻り値
    candidates: pd.DataFrame = None    # rank_routes_by_blank_coverage() の戻り値
    hours: tuple = tuple(range(5, 23))
    sources: list = field(default_factory=list)


DEFAULT_SOURCES = [
    "バス時刻表: GTFS-JP（公共交通オープンデータセンター／事業者公開フィード）",
    "人口: 令和2年国勢調査 100mメッシュ別人口（総務省統計局・e-Stat）",
]


# ===== 表の描画 =====

def _fmt(v):
    # 空欄はASCIIのハイフンにする。ダッシュ類（U+2014 等）はcp932に無く、
    # Windowsの日本語環境でExcelやメモ帳に貼ると文字化けするため。
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "-"
    if isinstance(v, float):
        # 小数は3桁まで出して末尾の0を落とす。桁数を固定すると路線長が
        # 「9.900」「10.8」と不揃いになり、精度を偽って見せることになる。
        s = f"{v:,.3f}".rstrip("0").rstrip(".")
        return s if s else "0"
    if isinstance(v, (int,)) and not isinstance(v, bool):
        return f"{v:,}"
    return str(v)


def markdown_table(df):
    """pandas を tabulate 無しで Markdown 表にする"""
    cols = list(df.columns)
    lines = ["| " + " | ".join(str(c) for c in cols) + " |",
             "|" + "|".join("---" for _ in cols) + "|"]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(_fmt(row[c]) for c in cols) + " |")
    return "\n".join(lines)


# ===== 推奨の決め方 =====

def recommend(table, arrival_hour):
    """比較表から推奨・注意事項を決定的に選ぶ

    「全日の削減」を第一の基準にする。狙った時刻だけを見るとダイヤシフトが
    常に有利に見えるが、便を抜いた時間帯の悪化が写らないため。
    """
    rows = table[table["施策"] != "現状（何もしない）"].copy()
    out = {"best": None, "cheapest": None, "free": None, "harmful": None,
           "side_effect": None, "n_plans": len(rows), "tied": 0}
    if rows.empty:
        return out

    positive = rows[rows["全日の削減"] > 0]
    harmful = rows[rows["全日の削減"] < 0]
    out["harmful"] = harmful if not harmful.empty else None
    if positive.empty:
        return out

    # 効果が同点の施策は珍しくない（別路線の増便がどちらも同じ空白地域を
    # 埋める場合など）。表の並び順で決めると、効果が同じでも運行キロが
    # 10倍近い施策を推奨しかねないので、同点は費用の小さいほうを採る。
    tied = positive[positive["全日の削減"] == positive["全日の削減"].max()]
    out["tied"] = len(tied)
    out["best"] = tied.sort_values(["運行キロ増", "追加便数"]).iloc[0]

    eff = positive[positive["1人あたり運行キロ"].notna()]
    if not eff.empty:
        cheapest = eff.loc[eff["1人あたり運行キロ"].idxmin()]
        if cheapest["施策"] != out["best"]["施策"]:
            out["cheapest"] = cheapest

    free = positive[positive["運行キロ増"] <= 0]
    out["free"] = free if not free.empty else None

    side = positive[positive["悪化した時間帯"] > 0]
    out["side_effect"] = side if not side.empty else None
    return out


# ===== 本体 =====

def build_document(ctx):
    """条件と比較結果から政策文書のドラフト（Markdown）を組み立てる"""
    s = ctx.summary
    total = max(s["total_pop"], 1)
    blank = s["blank_pop"]
    static = s["static_blank_pop"]
    time_blank = blank - static
    pct = 100.0 * blank / total
    dest = "、".join(ctx.dest_names) if ctx.dest_names else "（未設定）"
    h = ctx.arrival_hour
    rec = recommend(ctx.table, h)
    hours_label = f"{min(ctx.hours)}〜{max(ctx.hours)}時"

    p = [f"# {ctx.region}　公共交通の時間空白と改善施策の比較", ""]
    p.append("本資料は、バス時刻表（GTFS-JP）と国勢調査メッシュ人口から、"
             "「いつ・どこが・何人、生活拠点に到達できないか」を算出し、"
             "改善施策の効果を同じ指標で比較したものです。"
             "記載の数値はすべて同一の条件で機械的に算出しており、再計算して検証できます。")
    p.append("")
    p.append("**算定条件**")
    p.append("")
    p.append(f"- ダイヤ: {ctx.day_type}")
    p.append(f"- 生活拠点: {dest}")
    p.append(f"- 判定: {h}時までに拠点へ到着でき、所要時間が{ctx.max_time_min}分以内であること")
    p.append(f"- 乗車条件: 居住メッシュから徒歩{ctx.walk_m}m以内の停留所を利用（徒歩分速{ctx.walk_speed}m）")
    p.append("")

    # --- 1. 要旨 ---
    p.append("## 1. 要旨")
    p.append("")
    p.append(f"- {h}時までに{dest}へ到達できない人口は **{blank:,}人**で、"
             f"対象人口{total:,}人の{pct:.1f}%にあたります。うち65歳以上は{s['blank_elderly']:,}人です。")
    p.append(f"- このうち{static:,}人は徒歩圏に停留所が無い地域（静的空白）で、"
             f"ダイヤの見直しでは解消できません。残る **{time_blank:,}人**は"
             "停留所はあるが便が無い、または間に合わない「時間空白」であり、"
             "増便やダイヤの組み替えで削減できる範囲です。")
    if rec["best"] is not None:
        b = rec["best"]
        p.append(f"- 比較した{rec['n_plans']}件の施策のうち、{hours_label}の延べ空白人口を"
                 f"最も減らすのは「{b['施策']}」で、**{int(b['全日の削減']):,}人・時間帯**の削減となります。")
    else:
        p.append(f"- 比較した{rec['n_plans']}件の施策には、{hours_label}を通算して"
                 "空白人口を減らすものがありませんでした。対象路線または対象時間帯の"
                 "選び直しが必要です。")
    p.append("")

    # --- 2. 現状 ---
    p.append("## 2. 現状の診断")
    p.append("")
    diag_df = pd.DataFrame([
        {"区分": "対象人口", "人数": total, "説明": "メッシュ人口の合計"},
        {"区分": f"{h}時に到達できない人口", "人数": blank,
         "説明": f"うち65歳以上 {s['blank_elderly']:,}人（{pct:.1f}%）"},
        {"区分": "└ 静的空白", "人数": static, "説明": f"徒歩{ctx.walk_m}m以内に停留所が無い"},
        {"区分": "└ 時間空白", "人数": time_blank, "説明": "停留所はあるが、その時刻に間に合わない"},
    ])
    p.append(markdown_table(diag_df))
    p.append("")
    p.append("時間空白は、距離を基準にした従来の「交通空白地域」の定義では現れません。"
             "停留所が徒歩圏にあっても、必要な時刻に便が無ければ移動はできないためです。"
             "本資料の施策評価は、この時間空白を対象としています。")
    p.append("")

    if ctx.candidates is not None and not ctx.candidates.empty:
        p.append("**空白地域を通る路線（施策の候補）**")
        p.append("")
        p.append(markdown_table(ctx.candidates.head(10)))
        p.append("")
        p.append("候補は便数ではなく、空白地域の停留所をいくつ通るかで選んでいます。"
                 "便数の多い幹線を増便しても、その路線が空白地域を通っていなければ"
                 "空白人口は減りません。")
        p.append("")

    # --- 3. 比較 ---
    p.append("## 3. 比較した施策")
    p.append("")
    p.append(markdown_table(ctx.table))
    p.append("")
    p.append(f"- 「全日の削減」は{hours_label}の各時刻について空白人口を算出し、"
             "その延べ人数の減少を合計したものです。")
    p.append("- ダイヤシフトは便を移すだけなので、狙った時刻は必ず改善します。"
             "一方で便を抜いた時間帯は悪化するため、単一時刻ではなく全時間帯で評価しています。")
    p.append("- 「運行キロ増」は費用の代理値で、追加便数×路線長（停留所間の直線距離の和）です。"
             "実際のキロ当たり単価を掛ければ概算費用になります。")
    p.append("")

    # --- 4. 推奨 ---
    p.append("## 4. 推奨")
    p.append("")
    if rec["best"] is None:
        p.append("今回比較した範囲では、推奨できる施策はありません。"
                 "全日で見ると効果が無いか、他の時間帯の悪化が上回ります。"
                 "空白地域を通る別の路線を候補に加えるか、"
                 "静的空白に対してデマンド型交通など別の手段を検討してください。")
    else:
        b = rec["best"]
        p.append(f"**「{b['施策']}」を推奨します。**")
        p.append("")
        if int(b[f"{h}時の削減"]) > 0:
            p.append(f"- {h}時の空白人口は{int(b[f'{h}時の削減']):,}人減り、"
                     f"{int(b[f'{h}時の空白']):,}人になります。")
        else:
            # 狙った時刻が変わらない施策を「改善」と書くと誤解を招く。
            # 効果が他の時間帯に出ていることをそのまま書く。
            p.append(f"- {h}時の空白人口は{int(b[f'{h}時の空白']):,}人で変わりません。"
                     f"効果は他の時間帯に現れます。"
                     f"{h}時そのものを改善したい場合は、この時刻に拠点へ到達できる"
                     "便の追加を別途検討する必要があります。")
        p.append(f"- {hours_label}の延べ空白人口は{int(b['全日の削減']):,}人・時間帯減り、"
                 f"うち65歳以上は{int(b['全日うち高齢者']):,}人・時間帯です。")
        p.append(f"- 必要な増便は{int(b['追加便数']):,}便、運行キロの増分は{b['運行キロ増']:,.1f}km（1日あたり）です。")
        if b["1人あたり運行キロ"] is not None and not pd.isna(b["1人あたり運行キロ"]):
            p.append(f"- 空白人口を1人・時間帯減らすのに必要な運行キロは{b['1人あたり運行キロ']:.3f}kmです。")
        if int(b["悪化した時間帯"]) > 0:
            p.append(f"- ただし{int(b['悪化した時間帯'])}の時間帯で空白人口が増え、"
                     f"最大の悪化は{int(b['最大悪化']):,}人です。導入時はこの時間帯の確認が必要です。")
        p.append("")
        if rec["tied"] > 1:
            p.append(f"同じ削減効果となる施策が{rec['tied']}件あります。"
                     "そのうち運行キロの増分が最も小さいものを推奨としています。")
            p.append("")
        if rec["cheapest"] is not None:
            c = rec["cheapest"]
            p.append(f"費用を抑える場合は「{c['施策']}」が効率的です。"
                     f"削減は{int(c['全日の削減']):,}人・時間帯と小さくなりますが、"
                     f"運行キロの増分は{c['運行キロ増']:,.1f}kmにとどまります。")
            p.append("")
        if rec["free"] is not None:
            names = "、".join(f"「{n}」" for n in rec["free"]["施策"])
            p.append(f"運行キロを増やさずに効果が出る施策として{names}があります。"
                     "既存の便を時間帯間で移すもので、増車や増員を伴いません。"
                     "ただし便を抜いた時間帯の利用実態の確認が前提になります。")
            p.append("")

    if rec["harmful"] is not None:
        p.append("**注意を要する施策**")
        p.append("")
        for _, r in rec["harmful"].iterrows():
            if int(r[f"{h}時の削減"]) > 0:
                head = (f"- 「{r['施策']}」は{h}時こそ{int(r[f'{h}時の削減']):,}人改善しますが、")
            else:
                head = f"- 「{r['施策']}」は{h}時の空白を減らさないうえ、"
            p.append(head
                     + f"{hours_label}を通算すると空白人口が{abs(int(r['全日の削減'])):,}人・時間帯増えます。"
                     "採用は推奨しません。")
        p.append("")

    # --- 5. 前提と限界 ---
    p.append("## 5. 前提条件と限界")
    p.append("")
    p.append("- 到達可能性は時刻依存の最短経路探索で判定しています。停留所間の所要時間は"
             "時刻表の値をそのまま用い、乗り換えは時刻の前後関係で判定します。"
             "徒歩どうしの連続移動は乗り換えとして認めていません。")
    p.append("- 道路の混雑や遅延は考慮していません。時刻表どおりに運行された場合の値です。")
    p.append("- 人口はメッシュ中心に集約して扱っています。メッシュ内の分布は考慮していません。")
    p.append("- 鉄道・自家用車・自転車・タクシーは含めていません。バスのみで到達できるかを見ています。")
    p.append("- 運行キロは停留所間の直線距離の和であり、実走行距離より短めに出ます。"
             "施策どうしの相対比較に用いる値です。")
    p.append("")

    # --- 6. 出典 ---
    p.append("## 6. データ出典")
    p.append("")
    for src in (ctx.sources or DEFAULT_SOURCES):
        p.append(f"- {src}")
    p.append("")
    p.append("算出: クウハクスコープ（GTFS-JPとメッシュ人口から時間空白を診断するツール）")

    return "\n".join(p)


# ===== 数値の照合 =====

_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def extract_numbers(text):
    """文中の数値を桁区切りを外した文字列の集合にする"""
    out = set()
    for m in _NUM.findall(text or ""):
        v = m.replace(",", "").rstrip(".")
        if v:
            out.add(v.rstrip("0").rstrip(".") if "." in v else v)
    return out


def unverified_numbers(original, refined):
    """整えた文章に、下書きに無い数値が混ざっていないかを返す

    空リストなら、文章は変わっても数値は下書きのままだと機械的に言える。
    生成AIに数値の責任を持たせないための確認で、空でなければ利用者が
    その数値を見て判断する。
    """
    known = extract_numbers(original)
    return sorted(extract_numbers(refined) - known, key=lambda s: (len(s), s))


# ===== 任意: 生成AIで文章を整える =====

def refine(document, api_key, model=DEFAULT_MODEL, extra_instruction=""):
    """下書きの文章だけを整える。数値の責任は呼び出し側（下書き）にある。

    APIキーは呼び出し時に渡すだけで、このモジュールは保存も記録もしない。
    """
    if not (api_key or "").strip():
        raise ValueError("APIキーが空です。")
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError(
            "anthropic パッケージが入っていません。`pip install anthropic` を実行してください。"
        ) from e

    client = anthropic.Anthropic(api_key=api_key.strip())
    user = "次の下書きを、自治体の担当者がそのまま読める文書に整えてください。\n\n" + document
    if (extra_instruction or "").strip():
        user += ("\n\n---\n追加の指示（数値の改変・追加は不可）:\n"
                 + extra_instruction.strip())

    try:
        with client.messages.stream(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": user}],
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise RuntimeError("APIキーが正しくありません。") from e
    except anthropic.RateLimitError as e:
        raise RuntimeError("APIの利用制限に達しました。しばらく待って再実行してください。") from e
    except anthropic.APIConnectionError as e:
        raise RuntimeError("APIに接続できませんでした。ネットワークを確認してください。") from e
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"APIがエラーを返しました（{e.status_code}）: {e.message}") from e

    if msg.stop_reason == "refusal":
        raise RuntimeError("生成が拒否されました。下書きをそのままお使いください。")
    text = "\n".join(b.text for b in msg.content if b.type == "text").strip()
    if not text:
        raise RuntimeError("空の応答が返りました。下書きをそのままお使いください。")
    if msg.stop_reason == "max_tokens":
        text += "\n\n（注記: 出力が上限に達したため、末尾が欠けている可能性があります）"
    return text
