#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""keiba_sim.py — 競馬モンテカルロ・シミュレーター（既定1万回）

出馬表（JRA公式HTML）と、全10場の成績（JRA公式の成績PDFから作った data/results。
2021年〜の全着順・ハロンタイム）から、各馬の全走の指数・脚質・道悪適性を作り、
天候→馬場状態の確率と合わせて N 回レースを走らせ、勝率・連対率・複勝率・展開・想定時計を出す。
どちらも GitHub Actions が otakuan-pixel/jra2 に保存している。

第4版（2026-11-17）：通し検証で残っていた偏りを、騎手の勝率・馬体重・前走の着順と上り下り・クラスの上げ下げ・
間隔・コース適性・枠・脚質・キャリアの浅い馬の調教師、の情報で補正した。

オッズ込みの期待値（検証中）：sim に --odds "馬番:単勝,…" を渡すと、オッズにSIMの能力値などを足して勝率を作り直し、期待値を出す。

サブコマンド
  builddb  --repo <jra2のclone> --out <DB>         出馬表の全履歴＋成績データ → DB（runs.tsv / races.json.gz）
  list     --db <DB> [--date YYYYMMDD] [--q 名前]   レースを探す
  sim      --db <DB> --race YYYYMMDD:場:R [--going 良:0.7,稍重:0.3] [--n 10000] [--odds "1:4.4,2:12.3,…" --odds-asof "11/22 14:50"] --out <DIR>   良馬場・重馬場に固定した指数も同時に出す
  backtest --db <DB> [--n 300]                      出馬表のあるレースで精度を測る（前日オッズとの比較つき）
  validate --db <DB> [--since YYYYMMDD] [--until YYYYMMDD] [--n 1000]
                                                    成績データの全レースを「その週までのデータ」だけで予想して精度を測る（通し検証）

依存: beautifulsoup4, lxml, numpy（画像出力は matplotlib）
"""
import argparse, collections, csv, datetime as dt, gzip, hashlib, json, math, os, re, statistics as st, subprocess, sys
from pathlib import Path

JO = {"01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
      "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉"}
JO_R = {v: k for k, v in JO.items()}
WET = {"良": 0.0, "稍重": 0.45, "重": 0.8, "不良": 1.0}
RUN_FIELDS = ["horse", "ymd", "rc", "jo", "kai", "nichi", "rno", "race", "grade", "surf", "dist", "cond",
              "time", "place", "heads", "gate", "pop", "jockey", "wt", "hw", "corners", "f3", "vs", "diff", "rating",
              "l3", "sex", "age", "odds", "trainer", "owner", "breeder", "blinker"]

# ───────────────────────── パラメータ（backtest で調整した値） ─────────────────────────
P = {
    # ── 走りの評価（指数） ──
    "w_resid": 0.3,      # レース全体の時計の速さを指数にどれだけ反映するか（0=相手関係と着差だけ, 1=時計そのまま）
    "k_pace": 0.35,      # スローで時計が遅くなった分の割り戻し
    "k_wt": 0.05,        # 斤量1kgあたり（秒/1000m）
    "k_level": 4.0,      # レースレベル推定をクラス平均へ寄せる強さ
    "k_pop": 0.7,        # 過去走の人気（当時の評価）の反映
    "k_style_run": 0.03, # 過去走の展開不利（速い流れを前で、遅い流れを後ろで）の割り戻し ※回帰で推定
    # ── 今回への当てはめ ──
    "top_blend": 0.2,    # 平均と「良い時の走り」の配合
    "recency_days": 180, # 新しい走りほど重く（半減の目安）
    "prior_n": 0.3,      # 出走数が少ない馬をクラス平均へ寄せる強さ
    "prior_gap": 0.55,   # そのクラスの平均的な出走馬と勝ち馬の差
    "k_jockey": 1.0,     # 騎手効果の反映率（通し検証5,706レースで 0.5 では不足）
    "k_layoff": 0.1,     # 休み明けの割引（同検証で 0.3 は引きすぎ。241日以上の馬が予想の1.55倍勝っていた）
    "k_wt_now": 0.03,    # 今回の斤量1kgあたり（過去走の評価に使う k_wt より小さい）
    # 年齢・性別・初出走の補正（2025年で推定→2026年で確認。逆向きでもほぼ同じ値）
    "age_adj": {3: 0.36, 4: 0.15, 6: -0.10, 7: -0.20},   # 年齢の違う馬が走るレースだけ。5歳が基準、7は7歳以上
    "fem_adj": -0.04,    # 牡馬・せん馬と走る牝馬
    "n0_gap": 0.5,       # 評価できる過去走が無い馬を、クラス平均からどれだけ下に置くか（従来 0.1）
    # ── 第4版で足した補正（第3版の残りの偏りから。2025年で推定→2026年で確認、逆向きでも同じ符号。単位は秒/1000m） ──
    "k_hw": 0.057,       # 馬体重（過去6走の平均）30kgあたり。470kgが基準。大型馬ほど勝ち、420kg以下は見込みの0.69倍しか勝っていなかった
    "k_jk_wr": 0.115,    # 騎手の直近2年の勝率（7%が基準、対数）
    "k_jk_vm": 0.065,    # 騎手が市場の見立て（単勝オッズ）より勝っている度合い
    "last_adj": {2: 0.106, 3: 0.052, 6: -0.072, 10: -0.082},   # 前走の着順：2着、3着、6〜9着、10着以下
    "k_trend": 0.088,    # 前走の指数−これまでの平均（上り調子・下り調子）
    "drop_adj": 0.141,   # 前走より下のクラスに出る馬
    "quick_adj": -0.127, # 連闘・中1週（前走から15日以内）
    "gate_d": 0.112,     # ダート12頭以上：外枠ほど有利（内→外で±0.056）
    "gate_t": -0.065,    # 芝12頭以上：内枠ほど有利
    "k_front": 0.141,    # 前に行く脚質
    "k_maxcls": 0.223,   # いまより上のクラスを走った経験
    "k_course": 0.459,   # その競馬場（同じ芝ダ）での指数が、ほかの場より高い／低い
    "n2_adj": 0.082,     # 評価できる過去走がちょうど2走の馬（見込みの1.15倍勝っていた）
    "k_winrate": 0.475,  # 通算の勝率（3走以上。10%が基準）
    "short_adj": 0.082,  # 前走から400m以上の距離短縮（同じ芝ダ）
    "k_draw": 0.173,     # コースごとの枠の有利不利（過去の内枠・外枠の着順の差から）
    "k_tr_young": 0.23,  # 評価できる過去走が2走以下の馬だけ：調教師の直近2年の勝率（7%が基準、対数）
    "k_tr_debut": 0.125, # 同：その調教師の馬がデビュー1〜2戦目に出した指数の水準
    "k_going": 1.0,      # 道悪適性の反映率
    "k_dist": 0.10,      # 距離未経験の割引
    "k_rec": 0.5,        # 通算の3着内率の反映
    "k_west": 0.03,      # 栗東所属
    # ── レース中のゆらぎ ──
    "sigma0": 0.80,      # 1走ごとのばらつき（秒/1000m）。全馬共通・裾の長い分布（ロジスティック分布）
    "sigma_scale": 1.0,
    "b_pace": 0.03,      # ペース×位置取り（過去走9,958件の回帰では0.047±0.008。位置取りは結果にも左右されるので控えめに採用）
    "g_bias": 0.03,      # コース・馬場の前残り傾向×位置取り（同 0.066±0.010。同上）
}


TIME_BIAS = 1.36   # 想定時計の補正（秒/1000m）。通し検証（5,735レース）で想定が速すぎた分
# オッズ込みの期待値（検証中）：単勝オッズから見た勝率に、SIMの能力値と「市場が見誤りやすい向き」を足して勝率を作り直す。
# 係数は2025年1月〜2026年11月15日の5,735レースで推定。四半期ごとの入れ替え検証では、期待値0.9以上（50倍未満）で単勝回収率97%（誤差の幅86〜108%）、
# 1.0以上（50倍未満）で102%（72〜137%）。確定オッズでの検証なので、発走前のオッズで買う実戦ではこれより下がりうる。
MK = {"lpm": 0.9816,     # 単勝オッズから見た勝率（対数）
      "mu": 0.2493,      # SIMの能力値（秒/1000m）
      "l10": 0.2281,     # 前走10着以下（市場が嫌いすぎる）
      "l6_9": 0.1209,    # 前走6〜9着
      "jk_wr": -0.0992,  # 騎手の勝率（対数。上位騎手は買われすぎ）
      "closer": -0.1866, # 追込脚質（買われすぎ）
      "drop": -0.1794,   # 前走より下のクラス（買われすぎ）
      "n1": -0.1237,     # 評価できる過去走が1走だけ（買われすぎ）
      "a3": 0.1187,      # 古馬と走る3歳（低く見られがち）
      "quick": -0.0957,  # 連闘・中1週（買われすぎ）
      "tr_wr": -0.0517}  # 調教師の勝率（対数。上位厩舎は買われすぎ）
TIME_BAD = -0.38   # 不良馬場のときの追加補正（不良だけ想定が遅すぎた。137レース）
TIME_BAND = 0.75   # 想定時計の「8割の幅」の縮め方（そのままだと実際の92%が入る広さだったため）

# ───────────────────────── 共通の小道具 ─────────────────────────
def T(el):
    return el.get_text(" ", strip=True) if el else ""


def num(s):
    m = re.search(r"-?\d+(?:\.\d+)?", s or "")
    return m.group(0) if m else ""


def sec(t):
    m = re.match(r"^(?:(\d+):)?(\d+\.\d)$", (t or "").strip())
    return int(m.group(1) or 0) * 60 + float(m.group(2)) if m else None


def fmt_time(s):
    if s is None:
        return "—"
    return "%d:%04.1f" % (s // 60, s % 60) if s >= 60 else "%.1f" % s


def fl(x, d=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def cls_of(grade, race):
    g, n = grade or "", race or ""
    for k, v in (("GⅠ", "G1"), ("GⅡ", "G2"), ("GⅢ", "G3"), ("JpnⅠ", "G1"), ("JpnⅡ", "G2"), ("JpnⅢ", "G3"),
                 ("リステッド", "OP"), ("OP", "OP"), ("オープン", "OP"), ("3勝", "3勝"), ("2勝", "2勝"), ("1勝", "1勝"),
                 ("1600万", "3勝"), ("1000万", "2勝"), ("500万", "1勝")):
        if k in g or k in n:
            return v
    if "新馬" in n or "新馬" in g:
        return "新馬"
    if "未勝利" in n or "未勝利" in g:
        return "未勝利"
    return "他"


CLS_ORDER = ["新馬", "未勝利", "1勝", "2勝", "3勝", "OP", "G3", "G2", "G1"]


# ───────────────────────── 出馬表HTMLの解析 ─────────────────────────
def parse_past(td):
    if td is None or not td.select_one(".date"):
        return None
    a = td.select_one(".race_line .name a")
    corners = [li.get_text(strip=True) for li in td.select(".corner_list li")]
    fin = td.select_one("p.fin")
    fin_time = T(fin.select_one(".time")) if fin else ""
    fin_name = T(fin).replace(fin_time, "").strip() if fin else ""
    gi = td.select_one(".r_class img")
    grade = gi.get("alt", "") if gi else T(td.select_one(".r_class"))
    dist_txt = T(td.select_one(".dist"))
    md = re.match(r"(\d+)\s*(芝|ダ|障)?", dist_txt)
    dm = re.match(r"(\d{4})年(\d+)月(\d+)日", T(td.select_one(".date")))
    p = {
        "ymd": "%04d%02d%02d" % tuple(int(x) for x in dm.groups()) if dm else "",
        "rc": T(td.select_one(".rc")),
        "race": T(a) if a else T(td.select_one(".race_line .name")),
        "grade": grade,
        "place": T(td.select_one(".place_line .place")).replace("着", "").strip(),
        "heads": num(T(td.select_one(".num .max"))),
        "gate": num(T(td.select_one(".num .gate"))),
        "pop": num(T(td.select_one(".num .pop"))),
        "jockey": T(td.select_one(".info_line1 .jockey")).replace(" ", ""),
        "wt": num(T(td.select_one(".info_line1 .weight"))),
        "surf": (md.group(2) or "") if md else "",
        "dist": md.group(1) if md else "",
        "time": T(td.select_one("p.time")),
        "cond": T(td.select_one(".condition")),
        "hw": num(T(td.select_one(".h_weight"))),
        "rating": num(T(td.select_one("p.rating"))),
        "corners": "-".join(corners),
        "f3": T(td.select_one(".f3")).replace("3F", "").strip(),
        "vs": fin_name,
        "diff": fin_time.strip("()"),
        "jo": "", "kai": "", "nichi": "", "rno": "",
    }
    href = a.get("href", "") if a else ""
    m = re.search(r"CNAME=pw01sde10(\d\d)(\d{4})(\d\d)(\d\d)(\d\d)(\d{8})", href)
    if m:
        p.update(jo=m.group(1), kai=str(int(m.group(3))), nichi=str(int(m.group(4))), rno=str(int(m.group(5))), ymd=m.group(6))
    return p


def parse_entry(html_bytes, ymd, jo, r):
    from bs4 import BeautifulSoup
    s = BeautifulSoup(html_bytes.decode("cp932", "replace"), "lxml")
    hd = s.select_one("div.race_header")
    if hd is None:
        return None
    gi = hd.select_one(".race_name .grade_icon img")
    grade = gi.get("alt", "") if gi else ""
    name = T(hd.select_one(".race_name")).replace(grade, "").strip()
    course = T(hd.select_one(".cell.course")).replace("コース：", "").strip()
    md = re.search(r"([\d,]+)", course)
    dist = int(md.group(1).replace(",", "")) if md else 0
    surf = "障" if "障害" in T(hd) or "→" in course else ("ダ" if "ダート" in course else "芝")
    day = T(hd.select_one(".cell.date"))
    mk = re.search(r"(\d+)回\S+?(\d+)日", day)
    cat, klass = T(hd.select_one(".cell.category")), T(hd.select_one(".cell.class"))
    race = {
        "date": ymd, "jo": jo, "venue": JO.get(jo, jo), "r": int(r), "name": name, "grade": grade,
        "day": day, "kai": int(mk.group(1)) if mk else 0, "nichi": int(mk.group(2)) if mk else 0,
        "post": T(hd.select_one(".cell.time")).replace("発走時刻：", "").strip(),
        "cat": cat, "cls_txt": klass, "cls": cls_of(grade, klass + " " + name),
        "rule": T(hd.select_one(".cell.rule")) + " " + T(hd.select_one(".cell.weight")),
        "course": course, "dist": dist, "surf": surf,
        "inout": "外" if "外" in course else ("内" if "内" in course else ""),
        "turn": "左" if "左" in course else ("右" if "右" in course else "直"),
        "hdr": T(hd)[:200], "horses": [],
    }
    table = s.select_one("table.basic.narrow-xy.mt20") or s.select_one("table")
    if table is None:
        return None
    for tr in table.select("tbody tr"):
        na = tr.select_one("td.horse .name a") or tr.select_one("td.horse .name")
        if not na:
            continue
        waku_img = tr.select_one("td.waku img")
        htxt = T(tr.select_one("td.horse"))
        age = T(tr.select_one("td.jockey p.age"))
        ma = re.match(r"(牡|牝|セ|せん)\s*(\d+)(?:/(\S+))?", age)
        rec = re.search(r"\((\d+)\.(\d+)\.(\d+)\.(\d+)\)", T(tr.select_one(".cell.result")))
        prize = fl(T(tr.select_one(".cell.win")).replace(",", "").replace("万円", ""), 0.0)
        runs = [p for p in (parse_past(tr.select_one("td.past.p%d" % k)) for k in (1, 2, 3, 4)) if p]
        race["horses"].append({
            "waku": num(waku_img.get("alt", "")) if waku_img else "", "num": T(tr.select_one("td.num")),
            "name": T(na), "odds": fl(T(tr.select_one(".odds_line .num"))), "pop": fl(num(T(tr.select_one(".pop_rank")))),
            "sex": ma.group(1) if ma else "", "age": int(ma.group(2)) if ma else 0,
            "wt": fl(num(T(tr.select_one("td.jockey p.weight")))),
            "jockey": T(tr.select_one("td.jockey p.jockey")).replace(" ", ""),
            "trainer": T(tr.select_one("p.trainer a")).replace(" ", ""), "base": T(tr.select_one("p.trainer .division")).strip("()"),
            "sire": T(tr.select_one("li.sire")).replace("父：", "").strip(),
            "bms": T(tr.select_one(".bloodmare")).replace("母の父：", "").strip("() "),
            "rec": [int(x) for x in rec.groups()] if rec else None, "prize": prize,
            "cancel": ("取消" in htxt) or ("除外" in htxt) or not T(tr.select_one("td.num")).isdigit(), "runs": runs,
        })
    return race


# ───────────────────────── builddb：git履歴の全出馬表 → DB ─────────────────────────
def git(repo, *a, binary=False):
    out = subprocess.run(["git", "-C", str(repo)] + list(a), capture_output=True, check=True).stdout
    return out if binary else out.decode("utf-8", "replace")


def _parse_job(job):
    repo, commit, path, ct, ymd, jo, r = job
    race = parse_entry(git(repo, "show", "%s:%s" % (commit, path), binary=True), ymd, jo, r)
    if race:
        race["snap"] = ct
    return race


def load_results(repo):
    """data/results/<年>/<場>_{races,runs}.csv.gz（JRA公式の成績PDFから作った全着順）を runs の形にそろえて返す"""
    out = []
    base = Path(repo) / "data" / "results"
    for rp in sorted(base.glob("*/*_races.csv.gz")):
        up = rp.with_name(rp.name.replace("_races", "_runs"))
        if not up.exists():
            continue
        info = {}
        for r in csv.DictReader(gzip.open(rp, "rt", encoding="utf-8")):
            info[(r["ymd"], r["jo"], r["rno"])] = r
        byrace = collections.defaultdict(list)
        for u in csv.DictReader(gzip.open(up, "rt", encoding="utf-8")):
            byrace[(u["ymd"], u["jo"], u["rno"])].append(u)
        for k, us in byrace.items():
            ri = info.get(k)
            if not ri:
                continue
            fin = sorted((u for u in us if u["place"].isdigit() and sec(u["time"])), key=lambda u: int(u["place"]))
            if not fin or fin[0]["place"] != "1":
                continue
            starters = sum(1 for u in us if u["status"] not in ("取消", "除外"))
            t1 = sec(fin[0]["time"])
            second = next((u for u in fin if u["place"] != "1"), None)
            g = ri["grade"]
            g = "リステッド" if g == "L" else g
            for u in fin:
                t = sec(u["time"])
                if u["place"] == "1":
                    vs, diff = (second["horse"], round(sec(second["time"]) - t1, 1)) if second else ("", 0.0)
                else:
                    vs, diff = fin[0]["horse"], round(t - t1, 1)
                out.append({
                    "horse": u["horse"], "ymd": u["ymd"], "rc": JO.get(u["jo"], ""), "jo": u["jo"], "kai": u["kai"],
                    "nichi": u["nichi"], "rno": u["rno"], "race": ri["name"] or ri["cls_txt"], "grade": g or ri["cls_txt"],
                    "surf": ri["surf"], "dist": ri["dist"], "cond": ri["cond"], "time": u["time"], "place": u["place"],
                    "heads": str(starters), "gate": u["num"], "pop": u["pop"], "jockey": u["jockey"], "wt": u["wt"],
                    "hw": u["hw"], "corners": u["corners"], "f3": "", "vs": vs, "diff": "%.1f" % diff, "rating": "",
                    "l3": ri["last3"], "sex": u["sex"] if u["sex"] in ("牡", "牝") else "セ", "age": u["age"], "odds": u["odds"],
                    "trainer": u.get("trainer", ""), "owner": u.get("owner", ""), "breeder": u.get("breeder", ""), "blinker": u.get("blinker", "")})
    return out


def merge_runs(entry_runs, result_runs):
    """成績データ（全着順・全馬）を正とし、出馬表にしか無い情報（1頭ごとの上がり3F・レーティング、地方・海外の走り）を足す"""
    if not result_runs:
        return entry_runs
    ent = {(p["horse"], p["ymd"]): p for p in entry_runs}
    days = {(p["ymd"], p["jo"]) for p in result_runs}
    out = []
    for p in result_runs:
        e = ent.get((p["horse"], p["ymd"]))
        if e:
            p = dict(p, f3=e.get("f3", ""), rating=e.get("rating", ""))
        out.append(p)
    have = {(p["horse"], p["ymd"]) for p in result_runs}
    for p in entry_runs:
        if (p["horse"], p["ymd"]) in have:
            continue
        if (p["ymd"], p.get("jo")) in days:
            continue   # その開催日の成績がある＝出馬表側の表記ゆれ（馬名違い等）。成績を正とする
        out.append(p)
    return out


def cmd_builddb(a):
    repo, out = Path(a.repo), Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    log = git(repo, "-c", "core.quotepath=off", "log", "--no-renames", "--format=@%H %ct", "--name-status", "--", "data/entries")
    latest, commit, ct = {}, None, 0
    for line in log.splitlines():
        if line.startswith("@"):
            commit, ct = line[1:].split()
            continue
        m = re.match(r"^[AM]\s+(data/entries/(\d{8})/(\d\d)_(\d\d)\.html)$", line)
        if m and m.group(1) not in latest:   # log は新しい順 → 最初に出たものが最新版
            latest[m.group(1)] = (commit, int(ct), m.group(2), m.group(3), m.group(4))
    races, runs, seen = [], [], set()
    cache = out / "races.json.gz"
    old = {}
    if cache.exists():
        for r in json.load(gzip.open(cache, "rt", encoding="utf-8")):
            old[(r["date"], r["jo"], r["r"])] = r
    todo, keep = [], []
    for path, (commit, ct, ymd, jo, r) in sorted(latest.items()):
        key = (ymd, jo, int(r))
        if key in old and old[key].get("snap") == ct:
            keep.append(old[key])
        else:
            todo.append((str(repo), commit, path, ct, ymd, jo, r))
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as ex:
        parsed = [x for x in ex.map(_parse_job, todo, chunksize=8) if x]
    n_new = len(parsed)
    races = sorted(keep + parsed, key=lambda x: (x["date"], x["jo"], x["r"]))
    for race in races:
        for h in race["horses"]:
            for p in h["runs"]:
                k = (h["name"], p["ymd"], p["rc"], p["race"])
                if k in seen:
                    continue
                seen.add(k)
                runs.append(dict(p, horse=h["name"]))
    n_entry = len(runs)
    res_runs = load_results(repo)
    runs = merge_runs(runs, res_runs)
    with gzip.open(cache, "wt", encoding="utf-8") as f:
        json.dump(races, f, ensure_ascii=False, separators=(",", ":"))
    with open(out / "runs.tsv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(RUN_FIELDS)
        for p in sorted(runs, key=lambda p: (p["ymd"], p["jo"], p["rno"], p["horse"])):
            w.writerow([p.get(k, "") for k in RUN_FIELDS])
    ds = sorted({r["date"] for r in races})
    print("出馬表 %d レース（新規解析 %d）/ %s〜%s / 過去走 %d 件 / 馬 %d 頭" % (
        len(races), n_new, ds[0], ds[-1], len(runs), len({p["horse"] for p in runs})))
    if res_runs:
        dd = sorted({(p["ymd"], p["jo"]) for p in res_runs})
        print("  うち成績データ（全着順）%d 件：%d 開催日・%s〜%s・%s ／ 出馬表の近4走のみ %d 件" % (
            len(res_runs), len(dd), dd[0][0], dd[-1][0],
            "・".join(JO[j] for j in sorted({j for _, j in dd})), len(runs) - len(res_runs)))
    else:
        print("  成績データ（data/results）なし：出馬表の近4走 %d 件だけで作成" % n_entry)


def load_db(db):
    db = Path(db)
    races = json.load(gzip.open(db / "races.json.gz", "rt", encoding="utf-8"))
    runs = list(csv.DictReader(open(db / "runs.tsv", encoding="utf-8"), delimiter="\t"))
    return races, runs



# ───────────────────────── モデル：基準タイム・ペース・各走の指数 ─────────────────────────
def _i(x, d=None):
    try:
        return int(x)
    except (TypeError, ValueError):
        return d


def band(dist):
    return "短" if dist <= 1400 else ("中" if dist <= 2000 else "長")


class Model:
    """runs（過去走の全件）から、コース基準タイム・クラス差・馬場差・ペース・騎手効果を作る"""

    def __init__(self, runs, params=None, asof=None):
        self.P = dict(P, **(params or {}))
        self.asof = asof
        self.runs = [self._norm(r) for r in runs if not asof or (r.get("ymd") or "") < asof]
        self.runs = [r for r in self.runs if r]
        self._races()
        self._baselines()
        self._figures()
        self.by_horse = collections.defaultdict(list)
        for r in self.runs:
            self.by_horse[r["horse"]].append(r)
        for v in self.by_horse.values():
            v.sort(key=lambda r: r["ymd"], reverse=True)
        self._bias()
        self._jockeys_cache = {}

    @staticmethod
    def _norm(r):
        r = dict(r)
        r["t"] = sec(r.get("time"))
        r["d"] = _i(r.get("dist"), 0)
        r["pl"] = _i(r.get("place"))
        r["hd"] = _i(r.get("heads"), 0)
        r["f3v"] = fl(r.get("f3"))
        r["wtv"] = fl(r.get("wt"))
        r["dv"] = fl(r.get("diff"))
        cs = [_i(c) for c in (r.get("corners") or "").split("-") if _i(c)]
        r["c1"], r["c4"] = (cs[0], cs[-1]) if cs else (None, None)
        r["cls"] = cls_of(r.get("grade"), r.get("race"))
        r["jra"] = bool(r.get("jo")) and r.get("surf") in ("芝", "ダ", "障")
        if not r.get("ymd"):
            return None
        return r

    def _races(self):
        g = collections.defaultdict(list)
        for r in self.runs:
            if r["jra"] and r["t"] and r["d"] and "/" not in (r.get("cond") or ""):
                g[(r["ymd"], r["jo"], r["rno"])].append(r)
        self.R = {}
        for k, rs in g.items():
            wts = []
            for r in rs:
                if r["pl"] == 1:
                    wts.append(r["t"])
                elif r["pl"] and r["dv"] is not None:
                    wts.append(round(r["t"] - r["dv"], 1))
            if not wts:
                continue
            r0 = rs[0]
            wt = st.median(wts)
            d = {"ymd": k[0], "jo": k[1], "rno": k[2], "surf": r0["surf"], "dist": r0["d"], "cond": r0["cond"] or "良",
                 "cls": r0["cls"], "name": r0["race"], "win": wt, "spk": wt / r0["d"] * 1000, "heads": max(r["hd"] for r in rs),
                 "kai": _i(r0.get("kai"), 0), "nichi": _i(r0.get("nichi"), 0)}
            # 先頭の「残り600m通過」を推定：4角で最も前にいた馬の（走破−上がり3F）から
            fr = [(r["c4"], r["t"] - r["f3v"]) for r in rs if r["c4"] and r["f3v"] and r["d"] > 600]
            l3 = next((fl(r.get("l3")) for r in rs if fl(r.get("l3"))), None)
            if l3 and r0["d"] > 600 and r0["surf"] != "障":   # 成績データ：レースの上がり3Fは実測
                lead = wt - l3
                d["lead"], d["last3"], d["exact"] = lead, l3, True
                d["pidx"] = l3 / 600 * 1000 - lead / (r0["d"] - 600) * 1000 if r0["d"] > 800 else None
            elif fr:
                c4, own = min(fr)
                gap = 0.12 * min(c4 - 1, 4) + 0.08 * max(c4 - 5, 0)
                lead = own - min(gap, 1.5)
                d["lead"] = lead
                d["last3"] = wt - lead
                d["exact"] = c4 == 1
                d["pidx"] = (wt - lead) / 600 * 1000 - lead / (r0["d"] - 600) * 1000 if r0["d"] > 800 else None
            self.R[k] = d
        for r in self.runs:
            r["R"] = self.R.get((r["ymd"], r["jo"], r["rno"])) if r["jra"] else None

    def _baselines(self):
        R = list(self.R.values())
        by_c, by_sd = collections.defaultdict(list), collections.defaultdict(list)
        for d in R:
            by_c[(d["jo"], d["surf"], d["dist"])].append(d)
            by_sd[(d["surf"], d["dist"])].append(d)
        cadj = collections.defaultdict(float)
        gadj = collections.defaultdict(float)
        var = collections.defaultdict(float)
        base = {}

        def key_v(d):
            return (d["ymd"], d["jo"], d["surf"])

        for _ in range(5):
            adj = lambda d: d["spk"] - cadj[d["cls"]] - gadj[(d["surf"], d["cond"])] - var[key_v(d)]
            sd_med = {k: st.median(adj(d) for d in v) for k, v in by_sd.items()}
            for k, v in by_c.items():
                if len(v) >= 3:
                    base[k] = st.median(adj(d) for d in v)
                else:   # 少ないコースは全場の同距離で補う
                    base[k] = (sum(adj(d) for d in v) + 2 * sd_med[(k[1], k[2])]) / (len(v) + 2)
            for nm, keyf, store in (("c", lambda d: d["cls"], cadj), ("g", lambda d: (d["surf"], d["cond"]), gadj)):
                acc = collections.defaultdict(list)
                for d in R:
                    b = base[(d["jo"], d["surf"], d["dist"])]
                    other = (gadj[(d["surf"], d["cond"])] if nm == "c" else cadj[d["cls"]]) + var[key_v(d)]
                    acc[keyf(d)].append(d["spk"] - b - other)
                for k, v in acc.items():
                    store[k] = sum(v) / (len(v) + 3)
            z = cadj.get("1勝", 0.0)
            for k in list(cadj):
                cadj[k] -= z
            for s in ("芝", "ダ", "障"):
                z = gadj.get((s, "良"), 0.0)
                for k in list(gadj):
                    if k[0] == s:
                        gadj[k] -= z
            acc = collections.defaultdict(list)
            for d in R:
                acc[key_v(d)].append(d["spk"] - base[(d["jo"], d["surf"], d["dist"])] - cadj[d["cls"]] - gadj[(d["surf"], d["cond"])])
            for k, v in acc.items():
                var[k] = sum(v) / (len(v) + 1.5)
        self.base, self.cadj, self.gadj, self.var, self.sd_med = base, dict(cadj), dict(gadj), dict(var), sd_med
        for d in R:
            d["exp"] = base[(d["jo"], d["surf"], d["dist"])] + cadj[d["cls"]] + gadj[(d["surf"], d["cond"])] + var[key_v(d)]
            d["res"] = d["spk"] - d["exp"]            # ＋＝そのクラスとしては遅い決着
        self.res_sd = st.pstdev([d["res"] + var[key_v(d)] for d in R])   # 馬場差＋レースごとのぶれ
        # ペース指数（上がりの遅さ − 前半の遅さ。＋＝前傾＝ハイペース）を 芝ダ×距離帯 で標準化
        grp = collections.defaultdict(list)
        for d in R:
            if d.get("pidx") is not None:
                grp[(d["surf"], band(d["dist"]))].append(d["pidx"])
        self.pstat = {}
        for k, v in grp.items():
            m = st.median(v)
            mad = st.median(abs(x - m) for x in v) * 1.4826 or 1.0
            self.pstat[k] = (m, mad)
        xs, ys = [], []
        for d in R:
            if d.get("pidx") is not None:
                m, s = self.pstat[(d["surf"], band(d["dist"]))]
                d["pz"] = max(-3, min(3, (d["pidx"] - m) / s))
                xs.append(d["pz"]); ys.append(d["res"])
            else:
                d["pz"] = None
        # スロー（pz<0）ほど決着時計が遅くなる度合い
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        self.pace_slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
        # 残り600m通過と上がり3Fの標準（コース別）
        self.split = {}
        acc = collections.defaultdict(list)
        for d in R:
            if d.get("lead") and d["cond"] == "良":
                acc[(d["jo"], d["surf"], d["dist"])].append(d["last3"] - cadj[d["cls"]] * 0.6 * 0.6)
        for k, v in acc.items():
            if len(v) >= 3:
                self.split[k] = st.median(v)

    def _figures(self):
        Pm = self.P
        elig = []
        for r in self.runs:
            r["F"] = None
            d = r["R"]
            if d is None or r["pl"] is None or not r["t"]:
                continue
            if r["pl"] == 1:
                r["mg"] = -0.5 * min((r["dv"] or 0.0) / r["d"] * 1000, 0.5)
            else:
                r["mg"] = min(max(r["t"] - d["win"], 0.0) / r["d"] * 1000, 2.0)
            r["key"] = (r["ymd"], r["jo"], r["rno"])
            elig.append(r)
        # レースレベル：同じレースを走った馬たちの「他のレースでの走り」から、そのレースの相手の強さを推定
        clv = {c: -v for c, v in self.cadj.items()}
        L = {k: clv.get(d["cls"], 0.0) for k, d in self.R.items()}
        for _ in range(6):
            acc = collections.defaultdict(list)
            for r in elig:
                acc[(r["horse"], r["surf"])].append((r["key"], L[r["key"]] - r["mg"], r["mg"]))
            vote = collections.defaultdict(list)
            for lst in acc.values():
                n = len(lst)
                if n < 2:
                    continue
                tot = sum(v for _, v, _ in lst)
                for key, v, mg in lst:
                    vote[key].append((tot - v) / (n - 1) + mg)
            raw = collections.defaultdict(list)
            for k, d in self.R.items():
                xs = vote.get(k, [])
                xs = [max(min(x, clv.get(d["cls"], 0.0) + 1.5), clv.get(d["cls"], 0.0) - 1.5) for x in xs]
                L[k] = (sum(xs) + Pm["k_level"] * clv.get(d["cls"], 0.0)) / (len(xs) + Pm["k_level"])
                if xs:
                    raw[d["cls"]].append(sum(xs) / len(xs))
            for c, v in raw.items():
                if len(v) >= 8:
                    clv[c] = 0.5 * clv.get(c, 0.0) + 0.5 * (sum(v) / len(v))
            z = clv.get("1勝", 0.0)
            clv = {c: v - z for c, v in clv.items()}
            L = {k: v - z for k, v in L.items()}
        self.clv, self.L = clv, L
        # 指数→時計の対応（クラス別：相手関係の水準 clv と 勝ち時計の水準 cadj の直線）
        cnt = collections.Counter(d["cls"] for d in self.R.values())
        pts = [(clv[c], self.cadj[c], cnt[c]) for c in clv if c in self.cadj and cnt[c] >= 15]
        sw_ = sum(w for _, _, w in pts) or 1.0
        mx, my = sum(x * w for x, _, w in pts) / sw_, sum(y * w for _, y, w in pts) / sw_
        vx = sum(w * (x - mx) ** 2 for x, _, w in pts) or 1.0
        s_ = -sum(w * (x - mx) * (y - my) for x, y, w in pts) / vx
        self.f2t = (s_, my + s_ * mx)
        for r in elig:
            d = r["R"]
            res = d["res"]
            if d.get("pz") is not None:
                res -= Pm["k_pace"] * self.pace_slope * d["pz"]
            F = L[r["key"]] - r["mg"] - Pm["w_resid"] * res
            if r["wtv"]:
                F += Pm["k_wt"] * (r["wtv"] - 55.0)
            rp = (r["c4"] - 1) / max(r["hd"] - 1, 1) if r["c4"] and r["hd"] > 1 else None
            if rp is not None and d.get("pz") is not None:
                F += Pm["k_style_run"] * d["pz"] * (0.5 - rp) * 2   # 速い流れを前で／遅い流れを後ろで＝展開不利を割り戻す
            pp = _i(r.get("pop"))
            if pp and r["hd"] > 1:
                F += Pm["k_pop"] * (0.5 - (pp - 1) / (r["hd"] - 1))  # 当時の人気＝調教など成績に出ない評価の代わり
            r["F"], r["Fq"] = F, 1.0
            r["rp1"] = (r["c1"] - 1) / max(r["hd"] - 1, 1) if r["c1"] and r["hd"] > 1 else None
            r["rp4"] = rp
        for r in self.runs:   # 地方・海外の重賞だけはクラスと着差で評価
            if r["F"] is None and not r["jra"] and r["pl"] and r["cls"] in ("G1", "G2", "G3") and r["dv"] is not None and r["d"]:
                mg = (0 if r["pl"] == 1 else r["dv"]) / r["d"] * 1000
                r["F"], r["Fq"] = clv.get(r["cls"], 0.8) - 0.15 - min(mg, 2.0), 0.5

    def _bias(self):
        """前有利度：3着内の馬のうち4角3番手以内の割合（場×芝ダ×距離帯、芝ダ×馬場状態）"""
        a, b, tot = collections.defaultdict(lambda: [0, 0]), collections.defaultdict(lambda: [0, 0]), [0, 0]
        for r in self.runs:
            if r["jra"] and r["pl"] and r["pl"] <= 3 and r["c4"] and r["surf"] in ("芝", "ダ"):
                f = 1 if r["c4"] <= 3 else 0
                for store, k in ((a, (r["jo"], r["surf"], band(r["d"]))), (b, (r["surf"], r["cond"]))):
                    store[k][0] += f; store[k][1] += 1
                tot[0] += f; tot[1] += 1
        self.front_all = tot[0] / max(tot[1], 1)
        sh = lambda v: (v[0] + 30 * self.front_all) / (v[1] + 30)
        self.front_course = {k: sh(v) for k, v in a.items()}
        # 枠の有利不利：コース（場×芝ダ×距離）ごとに、内3分の1と外3分の1の平均着順（頭数で割った位置）の差。＋なら内が有利
        dr = collections.defaultdict(lambda: [0, 0.0, 0, 0.0])
        for r in self.runs:
            g = _i(r.get("gate"))
            if r["jra"] and r["pl"] and r["surf"] in ("芝", "ダ") and r["hd"] >= 10 and g:
                gr, pc = (g - 1) / (r["hd"] - 1), (r["pl"] - 1) / (r["hd"] - 1)
                v = dr[(r["jo"], r["surf"], r["d"])]
                if gr < 1 / 3:
                    v[0] += 1; v[1] += pc
                elif gr > 2 / 3:
                    v[2] += 1; v[3] += pc
        self.draw = {k: (v[3] / v[2] - v[1] / v[0]) * min(v[0], v[2]) / (min(v[0], v[2]) + 150.0) for k, v in dr.items() if v[0] and v[2]}
        self.front_cond = {k: sh(v) for k, v in b.items()}

    def front_index(self, jo, surf, dist, cond):
        """全体平均=0、±1 がおおむね最も極端なコース"""
        c = self.front_course.get((jo, surf, band(dist)), self.front_all) - self.front_all
        g = self.front_cond.get((surf, cond), self.front_all) - self.front_cond.get((surf, "良"), self.front_all)
        return (c + g) / 0.10

    def jockeys(self, asof):
        """騎手効果：その騎手が乗った時の指数 − その馬の他の走りの平均（縮小推定）"""
        if asof in self._jockeys_cache:
            return self._jockeys_cache[asof]
        acc = collections.defaultdict(list)
        for h, rs in self.by_horse.items():
            fs = [(r["F"], r["jockey"]) for r in rs if r["F"] is not None and r["jra"] and r["ymd"] < asof and r["surf"] != "障"]
            if len(fs) < 3:
                continue
            tot = sum(f for f, _ in fs)
            for f, j in fs:
                acc[j].append(f - (tot - f) / (len(fs) - 1))
        out = {j: sum(v) / (len(v) + 40) for j, v in acc.items()}
        self._jockeys_cache[asof] = out
        return out

    def jockey_stats(self, asof):
        """騎手ごとの直近2年の（騎乗数, 勝ち数, 単勝オッズから見た勝ち数の見込み）。平地・JRA・オッズのある走りだけ"""
        if not hasattr(self, "_jstat_cache"):
            self._jstat_cache = {}
        if asof in self._jstat_cache:
            return self._jstat_cache[asof]
        cut = "%04d%s" % (int(asof[:4]) - 2, asof[4:])
        acc = collections.defaultdict(lambda: [0, 0, 0.0])
        for r in self.runs:
            if r["jra"] and r["pl"] is not None and r["surf"] != "障" and cut <= r["ymd"] < asof:
                od = fl(r.get("odds"))
                if od:
                    a = acc[r["jockey"]]
                    a[0] += 1; a[1] += r["pl"] == 1; a[2] += 0.8 / od
        self._jstat_cache[asof] = acc
        return acc

    def trainer_stats(self, asof):
        """調教師ごとの直近2年の（出走数, 勝ち数）と、管理馬のデビュー1〜2戦目の指数（クラス平均との差）の一覧"""
        if not hasattr(self, "_tstat_cache"):
            self._tstat_cache = {}
        if asof in self._tstat_cache:
            return self._tstat_cache[asof]
        cut = "%04d%s" % (int(asof[:4]) - 2, asof[4:])
        wr, young = collections.defaultdict(lambda: [0, 0]), collections.defaultdict(list)
        for rs in self.by_horse.values():
            past = [r for r in rs if r["ymd"] < asof]
            m = len(past)
            for i, r in enumerate(past):
                tr = r.get("trainer")
                if not tr or not r["jra"] or r["pl"] is None or r["surf"] == "障" or r["ymd"] < cut:
                    continue
                if fl(r.get("odds")):
                    wr[tr][0] += 1; wr[tr][1] += r["pl"] == 1
                if (m - i) <= 2 and r["F"] is not None:
                    young[tr].append(r["F"] - self.clv.get(r["cls"], 0.0))
        out = (dict(wr), {k: sum(v) / (len(v) + 30.0) for k, v in young.items()})
        self._tstat_cache[asof] = out
        return out

    @staticmethod
    def _name_key(name, table):
        """成績PDFでは外字が「〓」になる。出馬表の名前（例：髙野）と突き合わせる"""
        if not name or name in table:
            return name
        for k in table:
            if "〓" in k and len(k) == len(name) and all(a == b or a == "〓" for a, b in zip(k, name)):
                return k
        return name

    # ── 1頭の評価 ──
    def rate(self, h, race, jk):
        Pm = self.P
        date, surf, dist = race["date"], race["surf"], race["dist"]
        d0 = dt.date(int(date[:4]), int(date[4:6]), int(date[6:]))
        seen, rs = set(), []
        for r in list(self.by_horse.get(h["name"], [])) + [self._adhoc(p, h["name"]) for p in h.get("runs", [])]:
            if r is None or r["ymd"] >= date or r["ymd"] in seen:
                continue
            seen.add(r["ymd"])
            rs.append(r)
        rs.sort(key=lambda r: r["ymd"], reverse=True)
        prior = self.clv.get(race["cls"], 0.0) - Pm["prior_gap"]
        ws, fs, jw, e_w, e_v, wet, dry = [], [], [], [], [], [], []
        same_surf = near_dist = 0
        for i, r in enumerate(rs[:12]):
            if r["F"] is None or (r["surf"] == "障") != (surf == "障"):
                continue
            days = (d0 - dt.date(int(r["ymd"][:4]), int(r["ymd"][4:6]), int(r["ymd"][6:]))).days
            w = 0.5 ** (days / Pm["recency_days"]) * (0.92 ** i) * r.get("Fq", 1.0)
            if r["surf"] == surf:
                same_surf += 1
                if abs(r["d"] - dist) <= 300:
                    near_dist += 1
            else:
                w *= 0.2
            w *= math.exp(-abs(r["d"] - dist) / 900.0)
            ws.append(w); fs.append(r["F"]); jw.append(jk.get(r["jockey"], 0.0))
            if r.get("rp1") is not None:
                e_w.append(w + 0.05); e_v.append(0.65 * r["rp1"] + 0.35 * r["rp4"])
            if r["surf"] == surf and r["jra"]:
                (wet if WET.get(r["cond"], 0) >= 0.45 else dry).append(r["F"])
        sw = sum(ws)
        if sw > 0:
            avg = sum(w * f for w, f in zip(ws, fs)) / sw
            good = sorted((f for w, f in zip(ws, fs) if w >= 0.2 * max(ws)), reverse=True)[:2]
            top = sum(good) / len(good)
            mu = (1 - Pm["top_blend"]) * avg + Pm["top_blend"] * top
            mu = (sw * mu + Pm["prior_n"] * prior) / (sw + Pm["prior_n"])
            jpast = sum(w * j for w, j in zip(ws, jw)) / sw
        else:
            mu, jpast = prior - Pm["n0_gap"], 0.0
        notes = []
        if h.get("rec") and sum(h["rec"]) >= 3:
            n_st = sum(h["rec"])
            mu += Pm["k_rec"] * ((h["rec"][0] + h["rec"][1] + h["rec"][2] + 1.0) / (n_st + 3.3) - 0.3)
        if h.get("base") == "栗東":
            mu += Pm["k_west"]
        mu -= Pm["k_wt_now"] * ((h.get("wt") or 55.0) - 55.0)
        field = [x for x in race.get("horses", []) if not x.get("cancel")]
        if len({x.get("age") for x in field if x.get("age")}) > 1 and h.get("age"):
            mu += Pm["age_adj"].get(min(int(h["age"]), 7), 0.0)
        if h.get("sex") == "牝" and any(x.get("sex") and x.get("sex") != "牝" for x in field):
            mu += Pm["fem_adj"]
        jn = re.sub(r"^[▲△☆★◇◆]", "", h.get("jockey") or "")   # 出馬表の騎手名には減量の印が付く。成績側の名前に合わせる
        jd = Pm["k_jockey"] * (jk.get(jn, 0.0) - jpast)
        mu += jd
        if rs:
            last = rs[0]["ymd"]
            lay = (d0 - dt.date(int(last[:4]), int(last[4:6]), int(last[6:]))).days
            if lay > 120:
                mu -= Pm["k_layoff"] * min(1.0, (lay - 120) / 180.0); notes.append("休み明け%d日" % lay)
        else:
            lay = None; notes.append("初出走")
        mkf = {}
        if surf != "障":   # 第4版の補正（平地のみ）
            fp = [r for r in rs if r["F"] is not None and r["surf"] != "障"]
            js = self.jockey_stats(date).get(jn, (0, 0, 0.0))
            # オッズ込みの期待値（MK）で使う項目
            mkf["jk_wr"] = math.log(((js[1] + 3.5) / (js[0] + 50.0)) / 0.07)
            mkf["closer"] = 1.0 if len(e_w) >= 2 and sum(w * v for w, v in zip(e_w, e_v)) / sum(e_w) >= 0.8 else 0.0
            mkf["n1"] = 1.0 if len(fs) == 1 else 0.0
            mkf["a3"] = 1.0 if _i(h.get("age"), 0) == 3 and len({x.get("age") for x in field if x.get("age")}) > 1 else 0.0
            if h.get("trainer"):
                t_wr0 = self.trainer_stats(date)[0]
                tk0 = self._name_key(h["trainer"], t_wr0)
                if tk0 in t_wr0 and t_wr0[tk0][0] >= 1:
                    mkf["tr_wr"] = math.log(((t_wr0[tk0][1] + 3.5) / (t_wr0[tk0][0] + 50.0)) / 0.07)
            if fp:
                lp0 = fp[0]["pl"] or 0
                mkf["l10"] = 1.0 if lp0 >= 10 else 0.0
                mkf["l6_9"] = 1.0 if 6 <= lp0 <= 9 else 0.0
                mkf["drop"] = 1.0 if self.clv.get(race["cls"], 0.0) < self.clv.get(fp[0]["cls"], 0.0) - 0.05 else 0.0
                mkf["quick"] = 1.0 if (d0 - dt.date(int(fp[0]["ymd"][:4]), int(fp[0]["ymd"][4:6]), int(fp[0]["ymd"][6:]))).days <= 15 else 0.0
            mu += Pm["k_jk_wr"] * math.log(((js[1] + 3.5) / (js[0] + 50.0)) / 0.07) + Pm["k_jk_vm"] * 50.0 * (js[1] - js[2]) / (js[0] + 100.0)
            m_f = len(field)
            g_rel = (_i(h.get("num"), 0) - 1) / (m_f - 1) if m_f >= 12 and _i(h.get("num"), 0) else None
            if g_rel is not None and surf in ("芝", "ダ"):
                mu += (Pm["gate_d"] if surf == "ダ" else Pm["gate_t"]) * (g_rel - 0.5)
            if m_f >= 2 and _i(h.get("num"), 0):
                mu += Pm["k_draw"] * (0.5 - (_i(h["num"]) - 1) / (m_f - 1)) * self.draw.get((race.get("jo"), surf, dist), 0.0) * 10.0
            if len(fs) == 2:
                mu += Pm["n2_adj"]
            if len(fs) <= 2 and h.get("trainer"):
                t_wr, t_y = self.trainer_stats(date)
                tk = self._name_key(h["trainer"], t_wr)
                if tk in t_wr:
                    mu += Pm["k_tr_young"] * math.log(((t_wr[tk][1] + 3.5) / (t_wr[tk][0] + 50.0)) / 0.07)
                mu += Pm["k_tr_debut"] * t_y.get(self._name_key(h["trainer"], t_y), 0.0)
            if h.get("rec") and sum(h["rec"]) >= 3:
                mu += Pm["k_winrate"] * max(-0.1, min(0.4, (h["rec"][0] + 0.3) / (sum(h["rec"]) + 3.0) - 0.1))
            if len(e_w) >= 2:
                mu += Pm["k_front"] * (0.5 - sum(w * v for w, v in zip(e_w, e_v)) / sum(e_w))
            if fp:
                l = fp[0]
                lp = l["pl"] or 0
                mu += Pm["last_adj"].get(2 if lp == 2 else 3 if lp == 3 else 6 if 6 <= lp <= 9 else 10 if lp >= 10 else 0, 0.0)
                if len(fs) >= 3 and sw > 0:
                    mu += Pm["k_trend"] * max(-1.0, min(1.0, l["F"] - sum(w * f for w, f in zip(ws, fs)) / sw))
                c_now = self.clv.get(race["cls"], 0.0)
                if c_now < self.clv.get(l["cls"], 0.0) - 0.05:
                    mu += Pm["drop_adj"]; notes.append("前走より下のクラス")
                mu += Pm["k_maxcls"] * max(0.0, min(1.0, max(self.clv.get(r["cls"], 0.0) for r in fp) - c_now))
                if (d0 - dt.date(int(l["ymd"][:4]), int(l["ymd"][4:6]), int(l["ymd"][6:]))).days <= 15:
                    mu += Pm["quick_adj"]
                if l["surf"] == surf and dist - l["d"] <= -400:
                    mu += Pm["short_adj"]
                hws = [_i(r.get("hw")) for r in fp[:6] if _i(r.get("hw"))]
                if hws:
                    mu += Pm["k_hw"] * max(-2.5, min(2.5, (sum(hws) / len(hws) - 470.0) / 30.0))
                here = [r["F"] for r in fp[:12] if r["jo"] == race.get("jo") and r["surf"] == surf]
                if here:
                    f_all = sum(r["F"] for r in fp[:12]) / len(fp[:12])
                    mu += Pm["k_course"] * max(-0.5, min(0.5, (sum(here) / len(here) - f_all) * len(here) / (len(here) + 3.0)))
        if sw > 0 and same_surf == 0:
            mu -= 0.25; notes.append("初" + ("ダート" if surf == "ダ" else "芝"))
        elif sw > 0 and near_dist == 0:
            mu -= Pm["k_dist"]; notes.append("距離は未経験の幅")
        apt = 0.0
        if wet and dry:
            apt = len(wet) / (len(wet) + 2.5) * (sum(wet) / len(wet) - sum(dry) / len(dry))
            apt = max(-0.5, min(0.5, apt))
        sig = Pm["sigma0"] * Pm["sigma_scale"]   # 走数の少ない馬ほど大きくする方式は、通し検証で当たり具合を悪くしていたのでやめた
        e = sum(w * v for w, v in zip(e_w, e_v)) / sum(e_w) if e_w else 0.5
        return {"mu": mu, "sig": sig, "apt": apt, "e": e, "e_n": len(e_w), "n": len(fs), "sw": sw, "lay": lay,
                "jd": jd, "notes": notes, "n_wet": len(wet),
                "best": max(fs) if fs else None, "avg": (sum(w * f for w, f in zip(ws, fs)) / sw) if sw else None,
                "last_f3": next((r["f3v"] for r in rs if r["f3v"]), None), "mkf": mkf}

    def _adhoc(self, p, horse):
        """DBに無い過去走（当日読み込んだ出馬表の分）をその場で指数化"""
        k = (p.get("ymd"), p.get("jo"), p.get("rno"))
        for r in self.by_horse.get(horse, []):
            if r["ymd"] == p.get("ymd"):
                return r
        r = self._norm(dict(p, horse=horse))
        if not r:
            return None
        r["R"] = self.R.get(k) if r["jra"] else None
        r["F"] = None
        if r["R"] is None and r["jra"] and r["t"] and r["pl"] and r["d"]:
            # レース自体がDBに無い → この馬の時計と着差だけで仮のレースを立てる
            win = r["t"] if r["pl"] == 1 else (r["t"] - r["dv"] if r["dv"] is not None else None)
            ck = (r["jo"], r["surf"], r["d"])
            b = self.base.get(ck) or self.sd_med.get((r["surf"], r["d"]))
            if win and b:
                exp = b + self.cadj.get(r["cls"], 0.0) + self.gadj.get((r["surf"], r["cond"]), 0.0) + self.var.get((r["ymd"], r["jo"], r["surf"]), 0.0)
                r["R"] = {"win": win, "cls": r["cls"], "res": win / r["d"] * 1000 - exp, "pz": None}
        if r["R"] is not None and r["pl"]:
            d = r["R"]
            mg = -0.5 * min((r["dv"] or 0.0) / r["d"] * 1000, 0.5) if r["pl"] == 1 else min((r["t"] - d["win"]) / r["d"] * 1000, 2.0)
            lv = self.L.get(k, self.clv.get(d["cls"], 0.0))
            F = lv - mg - self.P["w_resid"] * d["res"]
            if r["wtv"]:
                F += self.P["k_wt"] * (r["wtv"] - 55.0)
            pp = _i(r.get("pop"))
            if pp and r["hd"] > 1:
                F += self.P["k_pop"] * (0.5 - (pp - 1) / (r["hd"] - 1))
            r["F"], r["Fq"] = F, 0.8
            r["rp1"] = (r["c1"] - 1) / max(r["hd"] - 1, 1) if r["c1"] and r["hd"] > 1 else None
            r["rp4"] = (r["c4"] - 1) / max(r["hd"] - 1, 1) if r["c4"] and r["hd"] > 1 else None
        return r


# ───────────────────────── シミュレーション本体 ─────────────────────────
def parse_going(s, default="良"):
    if not s:
        return {default: 1.0}
    out = {}
    for part in s.split(","):
        k, _, v = part.partition(":")
        out[k.strip()] = float(v) if v else 1.0
    z = sum(out.values())
    return {k: v / z for k, v in out.items() if k in WET}


def simulate(model, race, going=None, n=300, seed=None, front_bias=0.0, detail=0, raw=False):
    """race（出馬表1レース）を n 回走らせる。戻り値は集計済みの dict"""
    import numpy as np
    Pm = model.P
    jk = model.jockeys(race["date"])
    H = [h for h in race["horses"] if not h.get("cancel") and str(h.get("num", "")).isdigit()]
    rt = [model.rate(h, race, jk) for h in H]
    m = len(H)
    going = going or {"良": 1.0}
    if seed is None:
        seed = int(hashlib.md5(("%s%s%s" % (race["date"], race["jo"], race["r"])).encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    mu = np.array([r["mu"] for r in rt]); sig = np.array([r["sig"] for r in rt])
    apt = np.array([r["apt"] for r in rt]); e = np.array([r["e"] for r in rt])
    e_sd = np.array([0.13 if r["e_n"] >= 2 else 0.22 for r in rt])
    gk = list(going); gp = np.array([going[k] for k in gk])
    gi = rng.choice(len(gk), size=n, p=gp)
    wetv = np.array([WET[k] for k in gk])[gi]
    fidx = np.array([model.front_index(race["jo"], race["surf"], race["dist"], k) for k in gk])[gi] + front_bias
    # 位置取り：各馬のテンの位置（0=先頭,1=最後方）をゆらして並べる
    pos = e[None, :] + rng.normal(0, 1, (n, m)) * e_sd[None, :]
    order = pos.argsort(axis=1).argsort(axis=1)
    rel = order / max(m - 1, 1)
    # ペース：前に行く馬の割合と頭数から（結果の分かる201レースで回帰。読める部分は小さく、残りは乱数）
    nfront = (pos < 0.12).sum(axis=1)
    pz = -1.09 + 1.67 * nfront / m + 0.97 * (m / 16.0) + rng.normal(0, 0.92, n)
    perf = (mu[None, :] + Pm["k_going"] * apt[None, :] * wetv[:, None]
            + Pm["b_pace"] * pz[:, None] * (rel - 0.5) * 2          # 速い流れ＝後ろ有利／遅い流れ＝前有利
            + Pm["g_bias"] * fidx[:, None] * (0.5 - rel) * 2          # コース・馬場の前残り傾向
            + rng.logistic(0, 1, (n, m)) / 1.8138 * sig[None, :])   # 標準偏差1にそろえたロジスティック分布（正規分布より大駆け・凡走が多い）
    rank = (-perf).argsort(axis=1).argsort(axis=1)        # 0=1着
    win = (rank == 0).mean(axis=0); top2 = (rank <= 1).mean(axis=0); top3 = (rank <= 2).mean(axis=0)
    # 想定時計：指数（相手関係の水準）を、クラス別の勝ち時計水準との対応で時計に直す
    ck = (race["jo"], race["surf"], race["dist"])
    b = model.base.get(ck) or model.sd_med.get((race["surf"], race["dist"]))
    times = None
    if b:
        gadj = np.array([model.gadj.get((race["surf"], k), 0.0) + (TIME_BAD if k == "不良" else 0.0) for k in gk])[gi]
        s, c0 = model.f2t
        # その日の馬場差・レースごとのぶれ（全馬共通なので着順には影響しない）と、検証で出た偏り（TIME_BIAS）を加える
        common = rng.normal(0, model.res_sd, n)
        spk = b + gadj[:, None] + c0 - s * perf + (model.pace_slope * pz)[:, None] + common[:, None] + TIME_BIAS
        times = spk * race["dist"] / 1000.0
    out = {"n": n, "seed": seed, "going": going, "m": m,
           "pace": {"ハイ": float((pz > 0.6).mean()), "ミドル": float(((pz <= 0.6) & (pz >= -0.6)).mean()), "スロー": float((pz < -0.6).mean())},
           "horses": []}
    lead = (order == 0).mean(axis=0)
    for i, h in enumerate(H):
        d = {"num": h["num"], "waku": h["waku"], "name": h["name"], "jockey": h["jockey"], "wt": h["wt"], "sexage": "%s%d" % (h["sex"], h["age"]),
             "odds": h.get("odds"), "pop": h.get("pop"), "win": float(win[i]), "top2": float(top2[i]), "top3": float(top3[i]),
             "avg_rank": float(rank[:, i].mean() + 1), "lead": float(lead[i]), "style": float(e[i]),
             "mu": float(mu[i]), "sig": float(sig[i]), "apt": float(apt[i]), "runs": rt[i]["n"], "notes": rt[i]["notes"],
             "jd": rt[i]["jd"], "n_wet": rt[i]["n_wet"], "perf": float(perf[:, i].mean())}
        for lab, mask in (("slow", pz < -0.6), ("high", pz > 0.6), ("dry", wetv < 0.3), ("wet", wetv >= 0.3)):
            d["win_" + lab] = float((rank[mask, i] == 0).mean()) if mask.sum() >= 400 else None
            d["top3_" + lab] = float((rank[mask, i] <= 2).mean()) if mask.sum() >= 400 else None
        out["horses"].append(d)
    # 組み合わせ（馬連・3連複・3連単の出現回数）
    o3 = (-perf).argsort(axis=1)[:, :3]
    nums = [h["num"] for h in H]
    c_ex, c_tr, c_tf = collections.Counter(), collections.Counter(), collections.Counter()
    for a, b2, c in o3:
        c_ex[tuple(sorted((nums[a], nums[b2]), key=int))] += 1
        c_tr[tuple(sorted((nums[a], nums[b2], nums[c]), key=int))] += 1
        c_tf[(nums[a], nums[b2], nums[c])] += 1
    out["umaren"] = [("-".join(k), v / n) for k, v in c_ex.most_common(8)]
    out["sanrenpuku"] = [("-".join(k), v / n) for k, v in c_tr.most_common(8)]
    out["sanrentan"] = [("→".join(k), v / n) for k, v in c_tf.most_common(5)]
    if times is not None:
        wt_ = times.min(axis=1)
        q10, q50, q90 = (float(np.percentile(wt_, q)) for q in (10, 50, 90))
        out["time"] = {"p10": q50 - TIME_BAND * (q50 - q10), "p50": q50, "p90": q50 + TIME_BAND * (q90 - q50)}
        if model.split.get(ck):
            out["course_last3"] = float(model.split[ck])      # このコース・良馬場の標準的なレース上がり3F
    out["front_index"] = float(fidx.mean())
    if detail and n < detail:
        # 展開別・馬場別の内訳は、条件ごとの回数が少ないと誤差が大きいので、同じ設定で試行を増やして集計する
        big = simulate(model, race, going=going, n=detail, seed=seed + 1, front_bias=front_bias, detail=0)
        for h, hb in zip(out["horses"], big["horses"]):
            for k in ("win_slow", "win_high", "win_dry", "win_wet", "top3_slow", "top3_high", "top3_dry", "top3_wet"):
                h[k] = hb[k]
        out["detail_n"] = detail
        out["pace"] = big["pace"]
        if big.get("time"):
            out["time"] = big["time"]
    if raw:   # 動画など、1回ごとの中身を使う用途向け（JSONには書き出さない）
        out["_raw"] = {"perf": perf, "rel": rel, "pz": pz, "times": times, "rank": rank, "H": H}
    return out



# ───────────────────────── 検証（結果が分かっている過去レース） ─────────────────────────
def results_from_runs(runs):
    """後の出馬表に載った「前走」から、各レースの勝ち馬と分かっている着順を集める"""
    res = {}
    for r in runs:
        if not r.get("jo") or not (r.get("place") or "").isdigit():
            continue
        k = (r["ymd"], r["jo"], int(r["rno"]))
        d = res.setdefault(k, {"place": {}, "winner": None, "cond": r.get("cond")})
        pl = int(r["place"])
        d["place"][r["horse"]] = pl
        if pl == 1:
            d["winner"] = r["horse"]
        elif r.get("vs") and d["winner"] is None:
            d["winner"] = r["vs"]
    return res


def backtest(races, runs, params=None, n=300, dates=None, verbose=False, models=None):
    import numpy as np
    res = results_from_runs(runs)
    models = models if models is not None else {}
    rows = []
    for race in sorted(races, key=lambda r: (r["date"], r["jo"], r["r"])):
        if dates and race["date"] not in dates:
            continue
        k = (race["date"], race["jo"], race["r"])
        rr = res.get(k)
        if not rr or not rr["winner"] or race["surf"] == "障" or race["cls"] == "新馬":
            continue
        H = [h for h in race["horses"] if not h.get("cancel") and str(h.get("num", "")).isdigit()]
        names = [h["name"] for h in H]
        if rr["winner"] not in names or len(H) < 5:
            continue
        if race["date"] not in models:
            models.clear()   # 全成績を入れてからは1日ぶんのモデルが約1.2GBあるので、前の日のものは捨てる
            models[race["date"]] = Model(runs, params, asof=race["date"])
        M = models[race["date"]]
        o = simulate(M, race, going={rr["cond"] if rr["cond"] in WET else "良": 1.0}, n=n)
        hs = o["horses"]
        wi = names.index(rr["winner"])
        p = np.array([h["win"] for h in hs]); p = (p * n + 0.5) / (n + 0.5 * len(hs))
        od = np.array([h["odds"] or np.nan for h in hs])
        # 出馬表のオッズが「前日オッズ」と言えるのは、レース当日の朝9時より前に取得したものだけ（当日に取り直した分は確定オッズに近い）
        lim = dt.datetime(int(race["date"][:4]), int(race["date"][4:6]), int(race["date"][6:]), 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=9))).timestamp()
        pre = bool(race.get("snap")) and race["snap"] < lim
        pm = (1 / od) / np.nansum(1 / od) if pre and np.isfinite(od).all() else None
        top = int(np.argmax(p))
        row = {"key": k, "m": len(hs), "ll": -math.log(p[wi]), "llu": math.log(len(hs)), "hit": int(top == wi), "pwin": float(p[wi]),
               "top_p": float(p[top]), "top_odds": hs[top]["odds"], "top_pl": rr["place"].get(names[top]),
               "llm": -math.log(pm[wi]) if pm is not None else None,
               "fav_hit": int(int(np.nanargmin(od)) == wi) if pm is not None else None,
               "win_odds": hs[wi]["odds"], "p": p, "pm": pm, "wi": wi, "cls": race["cls"], "surf": race["surf"],
               "t3": [(hs[i]["top3"], 1 if rr["place"][nm] <= 3 else 0) for i, nm in enumerate(names) if nm in rr["place"]],
               "t_pred": o["time"]["p50"] if o.get("time") else None, "t_lo": o["time"]["p10"] if o.get("time") else None,
               "t_hi": o["time"]["p90"] if o.get("time") else None, "dist": race["dist"]}
        rows.append(row)
    return rows


def summarize_bt(rows):
    n = len(rows)
    if not n:
        return {}
    m = [r for r in rows if r["llm"] is not None]
    t3 = [x for r in rows for x in r["t3"]]
    eps = 1e-3
    out = {"races": n, "ll": sum(r["ll"] for r in rows) / n, "ll_uniform": sum(r["llu"] for r in rows) / n,
           "hit": sum(r["hit"] for r in rows) / n,
           "t3ll": -sum(math.log(max(eps, min(1 - eps, p))) if y else math.log(max(eps, min(1 - eps, 1 - p))) for p, y in t3) / max(len(t3), 1),
           "races_odds": len(m)}
    if m:
        out.update(ll_m=sum(r["ll"] for r in m) / len(m), llm=sum(r["llm"] for r in m) / len(m),
                   hit_m=sum(r["hit"] for r in m) / len(m), fav_hit=sum(r["fav_hit"] for r in m) / len(m),
                   roi_top=sum((r["top_odds"] or 0) * r["hit"] for r in m) / len(m),
                   roi_fav=sum(r["win_odds"] * r["fav_hit"] for r in m) / len(m))
    return out


def cmd_backtest(a):
    races, runs = load_db(a.db)
    rows = backtest(races, runs, n=a.n)
    ds = sorted({r["key"][0] for r in rows})
    half = ds[len(ds) // 2]
    for lab, rs in (("全体", rows), ("前半(〜%s)" % ds[len(ds) // 2 - 1], [r for r in rows if r["key"][0] < half]), ("後半(%s〜)" % half, [r for r in rows if r["key"][0] >= half])):
        s = summarize_bt(rs)
        print("【%s】%d レース（%s〜%s）" % (lab, s["races"], min(r["key"][0] for r in rs), max(r["key"][0] for r in rs)))
        print("  SIM本命(◎)の勝率 %.1f%%　／　勝ち馬に付けていた勝率の対数損失 %.3f（全馬同率なら %.3f）" % (s["hit"] * 100, s["ll"], s["ll_uniform"]))
        if s.get("races_odds"):
            print("  うち前日オッズのある %d レース：SIM本命 %.1f%%・対数損失 %.3f ／ 前日1番人気 %.1f%%・対数損失 %.3f" % (
                s["races_odds"], s["hit_m"] * 100, s["ll_m"], s["fav_hit"] * 100, s["llm"]))
            print("  参考：SIM本命の単勝を前日オッズで買った場合 %.0f%%、前日1番人気 %.0f%%（確定オッズではない・件数が少なく誤差大）" % (s["roi_top"] * 100, s["roi_fav"] * 100))
    known = [r for r in rows if r["top_pl"] is not None]
    print("◎の着順が分かっている %d レース：3着内 %.1f%%" % (len(known), 100 * sum(1 for r in known if r["top_pl"] <= 3) / max(len(known), 1)))
    import numpy as np
    P_, Y_ = [], []
    for r in rows:
        for i, p in enumerate(r["p"]):
            P_.append(p); Y_.append(1 if i == r["wi"] else 0)
    P_, Y_ = np.array(P_), np.array(Y_)
    print("勝率の当たり具合（SIMが付けた勝率の帯ごとに、実際に勝った割合）")
    for lo, hi in ((0, .05), (.05, .1), (.1, .2), (.2, .3), (.3, 1.01)):
        m = (P_ >= lo) & (P_ < hi)
        if m.sum():
            print("  %2d〜%3d%%：%4d頭　SIM平均 %4.1f%%　実際 %4.1f%%" % (lo * 100, min(hi, 1) * 100, m.sum(), P_[m].mean() * 100, Y_[m].mean() * 100))


# ───────────────────────── オッズ込みの期待値（検証中） ─────────────────────────
def market_ev(model, race, odds=None):
    """単勝オッズ（odds={馬番: オッズ}。無ければ出馬表のオッズ）から、作り直した勝率と期待値を返す。全馬のオッズがそろわなければ None"""
    if race["surf"] == "障":
        return None
    H = [h for h in race["horses"] if not h.get("cancel") and str(h.get("num", "")).isdigit()]
    od = []
    for h in H:
        v = (odds or {}).get(int(h["num"])) if odds else h.get("odds")
        if not v or v <= 0:
            return None
        od.append(float(v))
    if len(H) < 2:
        return None
    jk = model.jockeys(race["date"])
    rt = [model.rate(h, race, jk) for h in H]
    inv = [1.0 / v for v in od]
    pm = [x / sum(inv) for x in inv]
    sc = [MK["lpm"] * math.log(q) + MK["mu"] * x["mu"] + sum(MK[k] * v for k, v in x["mkf"].items() if k in MK) for q, x in zip(pm, rt)]
    mx = max(sc)
    ex = [math.exp(v - mx) for v in sc]
    p = [v / sum(ex) for v in ex]
    return [{"num": h["num"], "name": h["name"], "odds": o_, "pm": q, "p": pp, "ev": pp * o_} for h, o_, q, pp in zip(H, od, pm, p)]


def parse_odds(s):
    """ "1:4.4,2:12.3" または "1=4.4 2=12.3" → {1: 4.4, 2: 12.3} """
    out = {}
    for tok in re.split(r"[,\s]+", (s or "").strip()):
        if tok:
            k, v = re.split(r"[:=]", tok)
            out[int(k)] = float(v)
    return out


# ───────────────────────── 通し検証（成績データの全レースを、その週までのデータだけで予想） ─────────────────────────
_WF = {}


def _wf_init(db, since, until, n):
    _, runs = load_db(db)
    byrace = collections.defaultdict(list)
    for r in runs:
        if r.get("odds") and since <= r["ymd"] <= until:
            byrace[(r["ymd"], r["jo"], int(r["rno"]))].append(r)
    _WF.update(runs=runs, byrace=byrace, n=n)


def _wf_work(group):
    asof, dates = group
    M = Model(_WF["runs"], asof=asof)
    out = []
    for k in sorted(_WF["byrace"]):
        if k[0] not in dates:
            continue
        rs = _WF["byrace"][k]
        r0 = rs[0]
        cls = cls_of(r0["grade"], r0["race"])
        if r0["surf"] == "障" or cls == "新馬" or len(rs) < 5 or not r0["dist"] or not any(r["place"] == "1" for r in rs):
            continue
        race = {"date": k[0], "jo": k[1], "r": k[2], "surf": r0["surf"], "dist": int(r0["dist"]), "cls": cls, "name": r0["race"], "horses": []}
        for r in sorted(rs, key=lambda r: int(r["gate"] or 0)):
            past = [p for p in M.by_horse.get(r["horse"], []) if p["ymd"] < k[0] and p["pl"]]
            rec = [sum(1 for p in past if p["pl"] == i) for i in (1, 2, 3)] + [sum(1 for p in past if p["pl"] > 3)]
            race["horses"].append({"name": r["horse"], "num": r["gate"], "waku": "", "odds": fl(r["odds"]), "pop": _i(r["pop"]),
                                   "sex": r["sex"], "age": _i(r["age"], 0), "wt": fl(r["wt"]), "jockey": r["jockey"],
                                   "rec": rec if sum(rec) else None, "base": "", "runs": [], "place": int(r["place"]), "trainer": r.get("trainer") or ""})
        cond = r0["cond"] if r0["cond"] in WET else "良"
        o = simulate(M, race, going={cond: 1.0}, n=_WF["n"])
        H = race["horses"]
        m = len(H)
        p = [(h["win"] * _WF["n"] + 0.5) / (_WF["n"] + 0.5 * m) for h in o["horses"]]
        inv = [1.0 / h["odds"] if h["odds"] else 0.0 for h in H]
        pm = [x / sum(inv) for x in inv] if all(inv) else None
        wi = next(i for i, h in enumerate(H) if h["place"] == 1)
        top = max(range(m), key=lambda i: p[i])
        fav = max(range(m), key=lambda i: pm[i]) if pm else None
        t_win = min(sec(r["time"]) for r in rs if r["place"] == "1")
        out.append({"key": k, "cls": cls, "hit": int(H[top]["place"] == 1), "top3": int(H[top]["place"] <= 3), "ll": -math.log(p[wi]),
                    "llu": math.log(m), "llm": -math.log(pm[wi]) if pm else None,
                    "fav_hit": int(H[fav]["place"] == 1) if pm else None, "fav_top3": int(H[fav]["place"] <= 3) if pm else None,
                    "roi": (H[top]["odds"] or 0.0) * int(H[top]["place"] == 1), "roi_fav": (H[fav]["odds"] or 0.0) * int(H[fav]["place"] == 1) if pm else None,
                    "t_in": int(o["time"]["p10"] <= t_win <= o["time"]["p90"]) if o.get("time") else None,
                    "t_err": (o["time"]["p50"] - t_win) / race["dist"] * 1000 if o.get("time") else None,
                    "mk": [(x["ev"], x["odds"], int(hh["place"] == 1)) for x, hh in zip(market_ev(M, race) or [], H)],
                    "cal": [(min(int(p[i] * 20), 8), p[i], int(H[i]["place"] == 1)) for i in range(m)]})
    print("  %s の週 %d レース" % (asof, len(out)), flush=True)
    return out


def cmd_validate(a):
    from multiprocessing import Pool
    _wf_init(a.db, a.since, a.until, a.n)
    days = sorted({k[0] for k in _WF["byrace"]})
    if not days:
        sys.exit("成績データ（オッズ付きの走り）がDBにありません。builddb を data/results のあるリポジトリで作り直してください")
    groups = []
    for d in days:   # 土日（と連続する祝日）を1つの週にまとめ、その初日より前のデータだけでモデルを作る
        dd = dt.date(int(d[:4]), int(d[4:6]), int(d[6:]))
        if groups and (dd - groups[-1][2]).days <= 2:
            groups[-1][1].add(d); groups[-1][2] = dd
        else:
            groups.append([d, {d}, dd])
    print("%s〜%s：%d 開催日・%d 週（1週あたり10〜20秒）" % (days[0], days[-1], len(days), len(groups)), flush=True)
    rows = []
    with Pool(min(2, os.cpu_count() or 1), initializer=_wf_init, initargs=(a.db, a.since, a.until, a.n)) as p:
        for out in p.imap(_wf_work, [(g[0], g[1]) for g in groups]):
            rows += out

    def show(lab, rs):
        n = len(rs)
        if not n:
            return
        m = [r for r in rs if r["llm"] is not None]
        t = [r["t_in"] for r in rs if r["t_in"] is not None]
        print("【%s】%d レース：SIM本命(◎) 勝率 %.1f%%・3着内 %.1f%%・単勝回収 %.0f%%・対数損失 %.3f（全馬同率なら %.3f）" % (
            lab, n, 100 * sum(r["hit"] for r in rs) / n, 100 * sum(r["top3"] for r in rs) / n, 100 * sum(r["roi"] for r in rs) / n,
            sum(r["ll"] for r in rs) / n, sum(r["llu"] for r in rs) / n))
        if m:
            print("      1番人気（確定オッズ） 勝率 %.1f%%・3着内 %.1f%%・単勝回収 %.0f%%・対数損失 %.3f ／ 想定時計の8割幅に入った割合 %.0f%%" % (
                100 * sum(r["fav_hit"] for r in m) / len(m), 100 * sum(r["fav_top3"] for r in m) / len(m), 100 * sum(r["roi_fav"] for r in m) / len(m),
                sum(r["llm"] for r in m) / len(m), 100 * sum(t) / max(len(t), 1)))

    show("全体", rows)
    for y in sorted({r["key"][0][:4] for r in rows}):
        show(y + "年", [r for r in rows if r["key"][0][:4] == y])
    for c in CLS_ORDER:
        show(c, [r for r in rows if r["cls"] == c])
    for j in sorted(JO):
        show(JO[j], [r for r in rows if r["key"][1] == j])
    acc = collections.defaultdict(lambda: [0, 0.0, 0])
    for r in rows:
        for b, p_, y in r["cal"]:
            acc[b][0] += 1; acc[b][1] += p_; acc[b][2] += y
    te = [r["t_err"] for r in rows if r.get("t_err") is not None]
    if te:
        print("想定勝ち時計−実際（秒/1000m）：平均 %+.3f" % (sum(te) / len(te)))
    mk = [x for r in rows for x in r.get("mk", [])]
    if mk:
        print("オッズ込みの期待値で単勝を買った場合（確定オッズ。係数は2025年1月〜2026年11月15日で推定したので、その期間は参考値）")
        for lab, f in (("期待値1.0以上", lambda x: x[0] >= 1.0), ("期待値1.0以上・50倍未満", lambda x: x[0] >= 1.0 and x[1] < 50),
                       ("期待値0.9以上・50倍未満", lambda x: x[0] >= 0.9 and x[1] < 50), ("期待値0.8未満・10倍未満（割高）", lambda x: x[0] < 0.8 and x[1] < 10)):
            bb = [x for x in mk if f(x)]
            if bb:
                print("  %s：%d点　的中 %.1f%%　回収 %.0f%%" % (lab, len(bb), 100 * sum(x[2] for x in bb) / len(bb), 100 * sum(x[1] * x[2] for x in bb) / len(bb)))
    print("勝率の当たり具合（SIMが付けた勝率の帯ごとに、実際に勝った割合）")
    for b in sorted(acc):
        v = acc[b]
        print("  %2d%%〜：%6d頭　SIM平均 %4.1f%%　実際 %4.1f%%" % (b * 5, v[0], 100 * v[1] / v[0], 100 * v[2] / v[0]))


# ───────────────────────── 出力（JSON・note用テキスト・画像） ─────────────────────────
VENUE_LL = {"01": (43.0767, 141.3264), "02": (41.7836, 140.7778), "03": (37.7650, 140.4810), "04": (37.9469, 139.1867),
            "05": (35.6627, 139.4843), "06": (35.7255, 139.9606), "07": (35.0667, 136.9903), "08": (34.9056, 135.7265),
            "09": (34.7789, 135.3622), "10": (33.8433, 130.8717)}
WAKU_COL = {"1": ("#ffffff", "#111"), "2": ("#222222", "#fff"), "3": ("#d8352a", "#fff"), "4": ("#1f63b5", "#fff"),
            "5": ("#f2cd1f", "#111"), "6": ("#2d8a4e", "#fff"), "7": ("#ef8a1c", "#111"), "8": ("#f19cb8", "#111")}


def pct(x):
    return "%d%%" % round(x * 100)


def n_txt(n):
    return "%d万回" % (n // 10000) if n % 10000 == 0 else "%s回" % format(n, ",")


def err_txt(n):
    return "±2〜3ポイント" if n < 1000 else ("±1〜1.5ポイント" if n < 5000 else "±1ポイント以内")


def style_name(e):
    return "逃げ・先行" if e < 0.2 else ("先行" if e < 0.38 else ("中団" if e < 0.62 else "後方"))


def assign_marks(hs):
    """印は機械的に付ける：◎○▲=勝率順、△=残りの複勝率上位2頭、☆=6番人気以下で複勝率が最も高い馬"""
    order = sorted(hs, key=lambda h: (-h["win"], -h["top3"]))
    marks = {}
    for h, m in zip(order[:3], "◎○▲"):
        marks[h["num"]] = m
    rest = sorted((h for h in hs if h["num"] not in marks), key=lambda h: -h["top3"])
    hole = next((h for h in rest if (h.get("pop") or 0) >= 6), None)
    if hole:
        marks[hole["num"]] = "☆"
    for h in [h for h in rest if h["num"] not in marks][:2]:
        marks[h["num"]] = "△"
    return marks


def note_text(race, o, meta):
    hs = sorted(o["horses"], key=lambda h: (-h["win"], -h["top3"]))
    marks = assign_marks(o["horses"])
    d = race["date"]
    wd = "月火水木金土日"[dt.date(int(d[:4]), int(d[4:6]), int(d[6:])).weekday()]
    surf = {"芝": "芝", "ダ": "ダート", "障": "障害"}[race["surf"]]
    going = "・".join("%s%s" % (k, pct(v)) for k, v in sorted(o["going"].items(), key=lambda kv: -kv[1]))
    L = []
    L.append("【タイトル案】")
    L.append("【%s】%sシミュレーションで出た勝率・複勝率と、良馬場・重馬場の指数" % (race["name"], n_txt(o["n"])))
    L.append("")
    L.append("【本文】")
    L.append("%d月%d日（%s）%s%dR %s（%s%dm%s）を、出走全馬の過去走データから%sシミュレーションしました。" % (
        int(d[4:6]), int(d[6:]), wd, race["venue"], race["r"], race["name"], surf, race["dist"], race["inout"], n_txt(o["n"])))
    L.append("想定した馬場は %s。%s" % (going, meta.get("weather_note", "")))
    L.append("")
    L.append("## シミュレーションの結果")
    L.append("（ここに結果の画像を入れる）")
    L.append("")
    for h in hs[:5]:
        m = marks.get(h["num"], "　")
        L.append("%s %s番 %s　勝率%s／連対率%s／複勝率%s" % (m, h["num"], h["name"], pct(h["win"]), pct(h["top2"]), pct(h["top3"])))
    L.append("")
    L.append("## 展開の見立て")
    p = o["pace"]
    L.append("ハイペース%s・ミドル%s・スロー%s。逃げ・先行馬の数と頭数からの見立てで、ペースは事前に読み切れない分を幅として持たせています。" % (pct(p["ハイ"]), pct(p["ミドル"]), pct(p["スロー"])))
    lead = sorted(o["horses"], key=lambda h: -h["lead"])[:3]
    L.append("ハナを切る回数が多かったのは " + "、".join("%s番%s（%s）" % (h["num"], h["name"], pct(h["lead"])) for h in lead if h["lead"] > 0.05) + "。")
    if o.get("time"):
        t = o["time"]
        L.append("想定の勝ちタイムは %s 前後（%s〜%s）。" % (fmt_time(t["p50"]), fmt_time(t["p10"]), fmt_time(t["p90"])))
    fi = o.get("front_index", 0)
    L.append("このコース・馬場の前残り傾向は「%s」。" % ("前が残りやすい" if fi > 0.4 else ("差しが届きやすい" if fi < -0.4 else "平均的")))
    L.append("")

    def movers(a, b, la, lb):
        xs = [(h[b] - h[a], h) for h in o["horses"] if h.get(a) is not None and h.get(b) is not None]
        xs.sort(key=lambda x: -x[0])
        return ["%s番%s（%s %s → %s %s）" % (h["num"], h["name"], la, pct(h[a]), lb, pct(h[b])) for dlt, h in xs[:2] if dlt >= 0.03]
    sl, hi = movers("win_high", "win_slow", "ハイ", "スロー"), movers("win_slow", "win_high", "スロー", "ハイ")
    if sl or hi:
        L.append("## 展開が変わると浮上する馬")
        if o.get("detail_n"):
            L.append("（展開別・馬場別の内訳は誤差を抑えるため、同じ設定で%s回に増やして集計しています）" % format(o["detail_n"], ","))
        if sl:
            L.append("スローになった回で勝率が上がる：" + "、".join(sl))
        if hi:
            L.append("ハイペースになった回で勝率が上がる：" + "、".join(hi))
        L.append("")
    wet = movers("win_dry", "win_wet", "良", "道悪")
    if len(o["going"]) > 1 and wet:
        L.append("## 馬場が渋った場合")
        L.append("道悪になった回で勝率が上がる：" + "、".join(wet))
        L.append("")
    du = o.get("dual")
    if du:
        L.append("## 良馬場・重馬場の指数")
        L.append("（ここに馬場別指数の画像を入れる）")
        L.append("馬場を良に固定した場合と重に固定した場合を、それぞれ%sずつ走らせました。指数はこのレースの出走馬の中での偏差値（50が平均）です。" % n_txt(o["n"]))
        for h in sorted(o["horses"], key=lambda h: -du[h["num"]]["g_idx"])[:6]:
            x = du[h["num"]]
            L.append("%s番 %s　良 %d（勝率%s）／重 %d（勝率%s）" % (h["num"], h["name"], round(x["g_idx"]), pct(x["g_win"]), round(x["h_idx"]), pct(x["h_win"])))
        dif = sorted(((du[h["num"]]["h_idx"] - du[h["num"]]["g_idx"], h) for h in o["horses"]), key=lambda t: -t[0])
        ups = ["%s番%s（%+d）" % (h["num"], h["name"], round(v)) for v, h in dif[:3] if v >= 1]
        dns = ["%s番%s（%+d）" % (h["num"], h["name"], round(v)) for v, h in dif[::-1][:3] if v <= -1]
        if ups:
            L.append("重馬場で指数が上がる：" + "、".join(ups))
        if dns:
            L.append("重馬場で指数が下がる：" + "、".join(dns))
        unk = [h["num"] for h in o["horses"] if not h["n_wet"]]
        if unk:
            L.append("道悪（稍重以上）の実績がデータに無い馬：%s番。適性が分からないため、重でも良とほぼ同じ指数になっています。" % "・".join(unk))
        L.append("")
    L.append("―――（有料にする場合は、ここから下を有料部分にする案）―――")
    L.append("")
    L.append("## 印")
    for h in hs:
        if h["num"] in marks:
            extra = "　単勝%.1f倍（%d番人気）" % (h["odds"], h["pop"]) if h.get("odds") and h.get("pop") else ""
            L.append("%s %s番 %s%s" % (marks[h["num"]], h["num"], h["name"], extra))
    L.append("")
    L.append("## 出現回数の多かった組み合わせ")
    L.append("馬連：" + "、".join("%s（%s）" % (k, pct(v)) for k, v in o["umaren"][:5]))
    L.append("3連複：" + "、".join("%s（%s）" % (k, pct(v)) for k, v in o["sanrenpuku"][:5]))
    L.append("")
    L.append("## このシミュレーションについて")
    L.append("・各馬のJRAでの全走（2021年以降）の時計・着差・相手関係・通過順・斤量・騎手・馬体重と、レースごとのラップ、年齢、枠順などから能力と脚質を数値化し、馬場状態と展開をランダムに変えながら%s走らせた結果です。" % n_txt(o["n"]))
    L.append("・%sの試行で、勝率の誤差は%sです。" % (n_txt(o["n"]), err_txt(o["n"])))
    L.append("・調教・パドック・当日の馬体重は入っていません。データは%s時点です。" % meta.get("asof", "前日"))
    L.append("・的中を保証するものではありません。馬券の購入はご自身の判断でお願いします。")
    L.append("")
    L.append("#競馬予想 #%s #中央競馬 #シミュレーション #AI競馬予想 #データ競馬" % re.sub(r"[\s　（）()第回\d]", "", race["name"]))
    return "\n".join(L)


def render_card(race, o, path, meta):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager as fm
    from matplotlib.patches import FancyBboxPatch, Rectangle
    import logging
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
    fonts = fm.findSystemFonts()
    fp = None
    for key in ("notosanscjk-regular", "notosansjp-regular", "notosanscjkjp-regular", "ipaexg", "ipagp", "ipag", "takao", "vl-gothic", "hiragino"):
        fp = next((f for f in fonts if key in os.path.basename(f).lower()), None)
        if fp:
            break
    if fp is None:
        return None
    fm.fontManager.addfont(fp)
    bold = next((f for f in fonts if os.path.basename(f).lower() == os.path.basename(fp).lower().replace("regular", "bold")), None)
    if bold and bold != fp:
        fm.fontManager.addfont(bold)
    plt.rcParams["font.family"] = fm.FontProperties(fname=fp).get_name()
    INK, SUB, LINE, BG = "#0b0b0b", "#52514e", "#dddcd6", "#fcfcfb"
    C3, C2, C1 = "#86b6ef", "#3987e5", "#184f95"       # 複勝率・連対率・勝率（同じ青の濃淡）
    hs = sorted(o["horses"], key=lambda h: (-h["win"], -h["top3"]))
    marks = assign_marks(o["horses"])
    n = len(hs)
    W, row, top, bot = 10.8, 0.46, 1.75, 0.95
    H = top + bot + row * n
    fig = plt.figure(figsize=(W, H), dpi=120, facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, W); ax.set_ylim(0, H); ax.axis("off")
    d = race["date"]
    surf = {"芝": "芝", "ダ": "ダート", "障": "障害"}[race["surf"]]
    ax.text(0.4, H - 0.55, "%s　%sシミュレーション" % (race["name"], n_txt(o["n"])), fontsize=19, fontweight="bold", color=INK, va="center")
    going = "・".join("%s%s" % (k, pct(v)) for k, v in sorted(o["going"].items(), key=lambda kv: -kv[1]))
    p = o["pace"]
    ax.text(0.4, H - 1.0, "%d/%d %s%dR　%s%dm%s　%d頭　｜　想定馬場 %s　｜　展開 ハイ%s・ミドル%s・スロー%s" % (
        int(d[4:6]), int(d[6:]), race["venue"], race["r"], surf, race["dist"], race["inout"], n, going, pct(p["ハイ"]), pct(p["ミドル"]), pct(p["スロー"])),
        fontsize=10.5, color=SUB, va="center")
    x_m, x_n, x_name, x_bar, bar_w, x_v = 0.45, 0.95, 1.35, 4.35, 2.85, 7.35
    hy = H - top + 0.2
    for x, s, ha in ((x_name, "馬名・騎手", "left"), (x_v + 0.35, "勝率", "right"), (x_v + 1.05, "連対率", "right"), (x_v + 1.75, "複勝率", "right"), (W - 0.4, "人気(単勝)", "right")):
        ax.text(x, hy, s, fontsize=9.5, color=SUB, ha=ha, va="center")
    lx = x_bar
    for c, s in ((C1, "勝率"), (C2, "連対率"), (C3, "複勝率")):
        ax.add_patch(Rectangle((lx, hy - 0.07), 0.16, 0.14, color=c, lw=0))
        ax.text(lx + 0.22, hy, s, fontsize=9.5, color=SUB, va="center")
        lx += 0.95
    ax.plot([0.4, W - 0.4], [H - top, H - top], color=LINE, lw=1)
    xmax = max(0.5, max(h["top3"] for h in hs) * 1.02)
    for i, h in enumerate(hs):
        y = H - top - row * (i + 0.5)
        if i:
            ax.plot([0.4, W - 0.4], [y + row / 2, y + row / 2], color=LINE, lw=0.5)
        ax.text(x_m, y, marks.get(h["num"], ""), fontsize=14, color=INK, ha="center", va="center", fontweight="bold")
        bg, fg = WAKU_COL.get(str(h["waku"]), ("#eeeeee", "#111"))
        ax.add_patch(FancyBboxPatch((x_n - 0.17, y - 0.15), 0.34, 0.30, boxstyle="round,pad=0,rounding_size=0.05", fc=bg, ec="#9a9a94", lw=0.7))
        ax.text(x_n, y - 0.005, h["num"], fontsize=10.5, color=fg, ha="center", va="center", fontweight="bold")
        ax.text(x_name, y + 0.07, h["name"], fontsize=11.5, color=INK, va="center", fontweight="bold")
        ax.text(x_name, y - 0.13, "%s %s %gkg　%s" % (h["sexage"], h["jockey"], h["wt"], style_name(h["style"])), fontsize=7.8, color=SUB, va="center")
        for v, c in ((h["top3"], C3), (h["top2"], C2), (h["win"], C1)):
            wv = bar_w * v / xmax
            if wv > 0.01:
                ax.add_patch(FancyBboxPatch((x_bar, y - 0.1), wv, 0.2, boxstyle="round,pad=0,rounding_size=%.3f" % min(0.04, wv / 2), fc=c, ec=BG, lw=1.0))
                ax.add_patch(Rectangle((x_bar, y - 0.1), min(0.04, wv), 0.2, color=c, lw=0))
        for x, v, b in ((x_v + 0.35, h["win"], True), (x_v + 1.05, h["top2"], False), (x_v + 1.75, h["top3"], False)):
            ax.text(x, y, pct(v), fontsize=11, color=INK, ha="right", va="center", fontweight="bold" if b else "normal")
        od = "%d人気 %.1f倍" % (h["pop"], h["odds"]) if h.get("odds") and h.get("pop") else "—"
        ax.text(W - 0.4, y, od, fontsize=9.5, color=SUB, ha="right", va="center")
    ax.plot([x_bar, x_bar], [H - top, bot - 0.05], color="#b9b8b1", lw=0.8)
    fy = bot - 0.3
    ax.text(0.4, fy, "勝率＝1着、連対率＝2着以内、複勝率＝3着以内に入った回数の割合。印は勝率・複勝率の順に機械的に付けています。", fontsize=8.6, color=SUB, va="center")
    ax.text(0.4, fy - 0.26, "%sの試行で、勝率の誤差は%s。オッズは%s時点。的中を保証するものではありません。" % (n_txt(o["n"]), err_txt(o["n"]), meta.get("asof", "前日")),
            fontsize=8.6, color=SUB, va="center")
    fig.savefig(path, dpi=120, facecolor=BG)
    plt.close(fig)
    return path


def _mpl_jp():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager as fm
    import logging
    logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
    fonts = fm.findSystemFonts()
    for key in ("notosanscjk-regular", "notosansjp-regular", "notosanscjkjp-regular", "ipaexg", "ipagp", "ipag", "takao", "vl-gothic", "hiragino"):
        fp = next((f for f in fonts if key in os.path.basename(f).lower()), None)
        if fp:
            fm.fontManager.addfont(fp)
            bold = next((f for f in fonts if os.path.basename(f).lower() == os.path.basename(fp).lower().replace("regular", "bold")), None)
            if bold and bold != fp:
                fm.fontManager.addfont(bold)
            plt.rcParams["font.family"] = fm.FontProperties(fname=fp).get_name()
            return plt
    return None


def render_dual(race, o, path, meta):
    """良馬場・重馬場の指数を1頭1行で並べる（●良 と ●重 を線でつなぐ）"""
    du = o.get("dual")
    plt = _mpl_jp() if du else None
    if plt is None:
        return None
    from matplotlib.patches import FancyBboxPatch
    INK, SUB, LINE, BG = "#0b0b0b", "#52514e", "#dddcd6", "#fcfcfb"
    CG, CH = "#2a78d6", "#eb6834"          # 良＝青、重＝オレンジ
    hs = sorted(o["horses"], key=lambda h: -du[h["num"]]["g_idx"])
    marks = o.get("marks") or assign_marks(o["horses"])
    n = len(hs)
    W, row, top, bot = 10.8, 0.46, 1.75, 0.95
    H = top + bot + row * n
    fig = plt.figure(figsize=(W, H), dpi=120, facecolor=BG)
    ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, W); ax.set_ylim(0, H); ax.axis("off")
    d = race["date"]
    surf = {"芝": "芝", "ダ": "ダート", "障": "障害"}[race["surf"]]
    ax.text(0.4, H - 0.55, "%s　良馬場・重馬場の指数" % race["name"], fontsize=19, fontweight="bold", color=INK, va="center")
    ax.text(0.4, H - 1.0, "%d/%d %s%dR　%s%dm%s　%d頭　｜　馬場を良・重に固定して、それぞれ%sシミュレーション" % (
        int(d[4:6]), int(d[6:]), race["venue"], race["r"], surf, race["dist"], race["inout"], n, n_txt(o["n"])), fontsize=10.5, color=SUB, va="center")
    x_m, x_n, x_name, x0, x1 = 0.45, 0.95, 1.35, 4.25, 6.75
    cols = ((7.55, "良 指数"), (8.25, "勝率"), (9.05, "重 指数"), (9.75, "勝率"), (10.4, "差"))
    hy = H - top + 0.2
    ax.text(x_name, hy, "馬名・騎手", fontsize=9.5, color=SUB, va="center")
    for x, s in cols:
        ax.text(x, hy, s, fontsize=9.5, color=SUB, ha="right", va="center")
    for lx, c, s in ((x0, CG, "良馬場"), (x0 + 1.05, CH, "重馬場")):
        ax.plot([lx + 0.08], [hy], marker="o", ms=8, mfc=c, mec=BG, mew=1.2)
        ax.text(lx + 0.24, hy, s, fontsize=9.5, color=SUB, va="center")
    ax.plot([0.4, W - 0.4], [H - top, H - top], color=LINE, lw=1)
    vals = [v for x in du.values() for v in (x["g_idx"], x["h_idx"])]
    lo, hi = min(vals) - 2, max(vals) + 2
    X = lambda v: x0 + (x1 - x0) * (v - lo) / (hi - lo)
    if lo < 50 < hi:
        ax.plot([X(50), X(50)], [H - top, bot - 0.05], color="#b9b8b1", lw=0.8)
        ax.text(X(50), bot - 0.16, "50（平均）", fontsize=8, color=SUB, ha="center", va="center")
    for i, h in enumerate(hs):
        y = H - top - row * (i + 0.5)
        x = du[h["num"]]
        if i:
            ax.plot([0.4, W - 0.4], [y + row / 2, y + row / 2], color=LINE, lw=0.5)
        ax.text(x_m, y, marks.get(h["num"], ""), fontsize=14, color=INK, ha="center", va="center", fontweight="bold")
        bg, fg = WAKU_COL.get(str(h["waku"]), ("#eeeeee", "#111"))
        ax.add_patch(FancyBboxPatch((x_n - 0.17, y - 0.15), 0.34, 0.30, boxstyle="round,pad=0,rounding_size=0.05", fc=bg, ec="#9a9a94", lw=0.7))
        ax.text(x_n, y - 0.005, h["num"], fontsize=10.5, color=fg, ha="center", va="center", fontweight="bold")
        ax.text(x_name, y + 0.07, h["name"], fontsize=11.5, color=INK, va="center", fontweight="bold")
        ax.text(x_name, y - 0.13, "%s　%s　%s" % (h["jockey"], style_name(h["style"]), "道悪%d走" % h["n_wet"] if h["n_wet"] else "道悪の実績なし"), fontsize=7.8, color=SUB, va="center")
        yg, yh = y + 0.055, y - 0.055
        ax.plot([X(x["g_idx"]), X(x["h_idx"])], [yg, yh], color="#9a9a94", lw=1.6, solid_capstyle="round", zorder=2)
        ax.plot([X(x["h_idx"])], [yh], marker="o", ms=8.5, mfc=CH, mec=BG, mew=1.4, zorder=3)
        ax.plot([X(x["g_idx"])], [yg], marker="o", ms=8.5, mfc=CG, mec=BG, mew=1.4, zorder=4)
        dv = round(x["h_idx"]) - round(x["g_idx"])
        for (cx, _), s, b in zip(cols, ("%d" % round(x["g_idx"]), pct(x["g_win"]), "%d" % round(x["h_idx"]), pct(x["h_win"]), "±0" if dv == 0 else "%+d" % dv), (True, False, True, False, False)):
            ax.text(cx, y, s, fontsize=11 if b else 10, color=INK if b else SUB, ha="right", va="center", fontweight="bold" if b else "normal")
    fy = bot - 0.45
    ax.text(0.4, fy, "指数＝このレースの出走馬の中での偏差値（平均50）。良を基準にした同じ物差しなので、重の指数が良より高い馬は道悪で相対的に浮上します。", fontsize=8.6, color=SUB, va="center")
    ax.text(0.4, fy - 0.26, "道悪の実績がデータに無い馬は適性が分からないため、重でも良とほぼ同じ指数になります。的中を保証するものではありません。", fontsize=8.6, color=SUB, va="center")
    fig.savefig(path, dpi=120, facecolor=BG)
    plt.close(fig)
    return path


def find_race(races, spec=None, q=None, date=None):
    if spec:
        d, jo, r = spec.split(":")
        jo = JO_R.get(jo, jo.zfill(2))
        return [x for x in races if x["date"] == d and x["jo"] == jo and x["r"] == int(r)]
    xs = [x for x in races if (not date or x["date"] == date) and (not q or q in x["name"] or q in (x["venue"] + str(x["r"]) + "R"))]
    return sorted(xs, key=lambda x: (x["date"], x["jo"], x["r"]))


def cmd_list(a):
    races, _ = load_db(a.db)
    xs = find_race(races, q=a.q, date=a.date)
    if not a.date and not a.q:
        last = max(x["date"] for x in races)
        xs = [x for x in xs if x["date"] >= last[:6] + "01"]
    for x in xs[-(a.limit):]:
        print("%s:%s:%d\t%s%dR\t%s%d%s\t%s\t%s\t%d頭\t発走%s" % (x["date"], x["jo"], x["r"], x["venue"], x["r"], x["surf"], x["dist"], x["inout"],
                                                               x["grade"] or x["cls"], x["name"], len(x["horses"]), x["post"]))


def cmd_sim(a):
    races, runs = load_db(a.db)
    if a.html:
        d, jo, r = a.race.split(":")
        race = parse_entry(open(a.html, "rb").read(), d, JO_R.get(jo, jo.zfill(2)), r)
        cand = [race] if race else []
    else:
        cand = find_race(races, spec=a.race) if a.race else find_race(races, q=a.q, date=a.date)
        cand = cand[-1:] if a.q and cand else cand
    if len(cand) != 1:
        sys.exit("レースを1つに絞れません（%d件）。list で確認してください" % len(cand))
    race = cand[0]
    model = Model(runs, asof=race["date"])
    going = parse_going(a.going)
    o = simulate(model, race, going=going, n=a.n, seed=a.seed, front_bias=a.front_bias, detail=a.detail)
    # 良馬場・重馬場に固定した版（指数は良の平均・ばらつきを物差しにした偏差値）
    og = simulate(model, race, going={"良": 1.0}, n=a.n, seed=o["seed"] + 11, front_bias=a.front_bias)
    oh = simulate(model, race, going={"重": 1.0}, n=a.n, seed=o["seed"] + 12, front_bias=a.front_bias)
    pg = [h["perf"] for h in og["horses"]]
    mg, sg = st.mean(pg), (st.pstdev(pg) or 1.0)
    o["dual"] = {hg["num"]: {"g_idx": 50 + 10 * (hg["perf"] - mg) / sg, "h_idx": 50 + 10 * (hh["perf"] - mg) / sg,
                             "g_win": hg["win"], "g_top3": hg["top3"], "h_win": hh["win"], "h_top3": hh["top3"]}
                 for hg, hh in zip(og["horses"], oh["horses"])}
    snap =dt.datetime.fromtimestamp(race["snap"], dt.timezone(dt.timedelta(hours=9))).strftime("%-m月%-d日%-H時") if race.get("snap") else "前日"
    meta = {"asof": snap, "weather_note": a.weather_note or ""}
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    tag = "%s_%s_%02d" % (race["date"], race["jo"], race["r"])
    o["race"] = {k: race[k] for k in ("date", "jo", "venue", "r", "name", "grade", "cls", "surf", "dist", "inout", "post", "day")}
    o["marks"] = assign_marks(o["horses"])
    o["asof"] = snap
    odds_in = parse_odds(a.odds) if getattr(a, "odds", None) else None
    mk = market_ev(model, race, odds_in)
    if mk:
        o["market"] = {"asof": (a.odds_asof or "指定のオッズ") if odds_in else snap + "（出馬表のオッズ）", "horses": mk}
    json.dump(o, open(out / ("sim_%s.json" % tag), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    open(out / ("note_%s.txt" % tag), "w", encoding="utf-8").write(note_text(race, o, meta))
    card = render_card(race, o, str(out / ("card_%s.png" % tag)), meta)
    dual = render_dual(race, o, str(out / ("dual_%s.png" % tag)), meta)
    print("■ %s %s%dR %s（%s%dm%s・%s）%d頭　試行%s　馬場 %s　オッズ %s時点" % (
        race["date"], race["venue"], race["r"], race["name"], race["surf"], race["dist"], race["inout"], race["cls"], o["m"], n_txt(o["n"]),
        " ".join("%s%s" % (k, pct(v)) for k, v in o["going"].items()), snap))
    print("展開 ハイ%s ミドル%s スロー%s ／ 前残り指数 %+.1f ／ 想定勝ち時計 %s" % (
        pct(o["pace"]["ハイ"]), pct(o["pace"]["ミドル"]), pct(o["pace"]["スロー"]), o["front_index"], fmt_time(o["time"]["p50"]) if o.get("time") else "—"))
    print("印 馬番 馬名            勝率 連対 複勝  平均着  脚質    走数 人気 単勝   勝率×単勝  良/重指数 メモ")
    for h in sorted(o["horses"], key=lambda h: (-h["win"], -h["top3"])):
        ev = "%.2f" % (h["win"] * h["odds"]) if h.get("odds") else "—"
        print("%s %2s %-10s\t%4s %4s %4s  %4.1f  %-5s %2d  %3s %6s  %5s  %s" % (
            o["marks"].get(h["num"], "　"), h["num"], h["name"], pct(h["win"]), pct(h["top2"]), pct(h["top3"]), h["avg_rank"], style_name(h["style"]),
            h["runs"], "%d" % h["pop"] if h.get("pop") else "—", "%.1f" % h["odds"] if h.get("odds") else "—", ev,
            "%d/%d %s" % (round(o["dual"][h["num"]]["g_idx"]), round(o["dual"][h["num"]]["h_idx"]), " ".join(h["notes"]))))
    if o.get("market"):
        print("■ オッズ込みの期待値（検証中）　オッズ：%s" % o["market"]["asof"])
        print("   馬番 馬名            単勝   市場の見立て  作り直した勝率  期待値")
        for x in sorted(o["market"]["horses"], key=lambda x: -x["ev"]):
            print("%s %2s %-10s\t%6.1f   %5.1f%%       %5.1f%%      %.2f" % (
                "★" if x["ev"] >= 1.0 and x["odds"] < 50 else "☆" if x["ev"] >= 0.9 and x["odds"] < 50 else "　", x["num"], x["name"], x["odds"], 100 * x["pm"], 100 * x["p"], x["ev"]))
        print("   ★＝期待値1.0以上、☆＝0.9以上（どちらも単勝50倍未満だけ。50倍以上はもともと回収率が60%前後と低いので対象外）。")
        print("   過去の検証（確定オッズ）では、★の単勝回収率は102%（誤差の幅72〜137%）、☆以上で97%（86〜108%）。オッズが動けば期待値も変わる。")
    else:
        print("■ オッズ込みの期待値：全馬の単勝オッズがそろっていないので出せません（--odds \"1:4.4,2:12.3,…\" で渡す）")
    print("馬連上位:", " ".join("%s(%s)" % (k, pct(v)) for k, v in o["umaren"][:5]))
    print("3連複上位:", " ".join("%s(%s)" % (k, pct(v)) for k, v in o["sanrenpuku"][:5]))
    print("出力:", out / ("sim_%s.json" % tag), out / ("note_%s.txt" % tag), card or "（画像なし：日本語フォントが見つかりません）", dual or "")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="競馬モンテカルロ・シミュレーター")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("builddb"); b.add_argument("--repo", required=True); b.add_argument("--out", required=True)
    b = sub.add_parser("list"); b.add_argument("--db", required=True); b.add_argument("--date"); b.add_argument("--q"); b.add_argument("--limit", type=int, default=80)
    b = sub.add_parser("sim"); b.add_argument("--db", required=True); b.add_argument("--race"); b.add_argument("--q"); b.add_argument("--date")
    b.add_argument("--html"); b.add_argument("--going"); b.add_argument("--n", type=int, default=10000); b.add_argument("--detail", type=int, default=6000); b.add_argument("--seed", type=int)
    b.add_argument("--front-bias", dest="front_bias", type=float, default=0.0); b.add_argument("--weather-note", dest="weather_note"); b.add_argument("--out", default="out")
    b.add_argument("--odds", help='発走前の単勝オッズ。"1:4.4,2:12.3,…"（全馬ぶん）'); b.add_argument("--odds-asof", dest="odds_asof", help="そのオッズの時刻（例 11/22 14:50）")
    b = sub.add_parser("backtest"); b.add_argument("--db", required=True); b.add_argument("--n", type=int, default=300)
    b = sub.add_parser("validate"); b.add_argument("--db", required=True); b.add_argument("--since", default="20260101"); b.add_argument("--until", default="99999999"); b.add_argument("--n", type=int, default=1000)
    a = ap.parse_args()
    {"builddb": cmd_builddb, "list": cmd_list, "sim": cmd_sim, "backtest": cmd_backtest, "validate": cmd_validate}[a.cmd](a)
