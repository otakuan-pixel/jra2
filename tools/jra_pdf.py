#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jra_pdf.py — JRA公式「成績」PDF（1開催日・1場ぶん）から、レースと全出走馬を取り出す

  python3 jra_pdf.py <PDF> [<PDF> ...] --out-races races.csv --out-runs runs.csv

PDFの特徴（2021〜2026年で確認）
- A4横1ページに2レース（左右2段）。左右を別々に読む
- 数字は埋め込みフォントで (cid:9872)〜(cid:9881) = 0〜9 のことがある
- 騎手名と馬主名は文字列としてはつながるので、文字の x 座標で列を切り分ける
- 出走馬の行は着順の並び。「〃」は前の馬と同タイム
- 1頭ごとの上がり3Fは載っていない（レースのハロンタイムと通過順位はある）
"""
import argparse, collections, csv, os, re, sys

DIG = {9872 + i: str(i) for i in range(10)}
TRACK_JO = {"sapporo": "01", "hakodate": "02", "fukushima": "03", "niigata": "04", "tokyo": "05",
            "nakayama": "06", "chukyo": "07", "kyoto": "08", "hanshin": "09", "kokura": "10"}
JO_NAME = {"01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
           "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉"}
Z2H = str.maketrans("０１２３４５６７８９．，：＋−－―ー（）", "0123456789.,:+--―ー()")
RACE_FIELDS = ["ymd", "jo", "kai", "nichi", "rno", "name", "grade", "cls_txt", "surf", "dist", "inout", "turn",
               "weather", "cond", "heads", "laps", "first3", "last3", "last4", "corner_labels", "src"]
RUN_FIELDS = ["ymd", "jo", "kai", "nichi", "rno", "horse", "place", "status", "waku", "num", "sex", "age", "wt",
              "jockey", "hw", "hwd", "time", "odds", "pop", "c1", "c2", "c3", "c4", "corners"]


def ctext(ch):
    t = ch["text"]
    m = re.match(r"^\(cid:(\d+)\)$", t)
    if m:
        n = int(m.group(1))
        return DIG.get(n, "{%d}" % n)
    return t


def dec(s):
    return re.sub(r"\(cid:(\d+)\)", lambda m: DIG.get(int(m.group(1)), "{%s}" % m.group(1)), s)


def norm(s):
    return (s or "").translate(Z2H)


def zone_text(chs, sep=""):
    return sep.join(ctext(c) for c in sorted(chs, key=lambda c: (c["x0"], c["top"])))


def parse_meta_from_name(path):
    m = re.search(r"(\d{4})-(\d)([a-z]+)(\d{1,2})\.pdf$", os.path.basename(path))
    if not m:
        return None
    y, k, t, n = m.groups()
    return {"year": int(y), "kai": int(k), "track": t, "jo": TRACK_JO.get(t, ""), "nichi": int(n)}


SEP = set(",，-−＝=―")


def corner_region(chars):
    """「通過順位」の欄を文字の座標から読む → {馬番: {コーナー: 順位}}, ラベル一覧
    - カッコ内（横並び）は同じ順位。ただし数字の上に「・」が付いた馬は集団の先頭で、残りはその次の順位
    - 「・」は数字の4〜5pt上に別の行として置かれているので、x 座標で馬番に結び付ける
    - 「)(」は重ねて描かれ、x 順では「()」になる"""
    rows = collections.defaultdict(list)
    for c in chars:
        rows[round(c["top"])].append(c)
    start = stop = None
    for top in sorted(rows):
        s = "".join(c["text"] for c in sorted(rows[top], key=lambda c: c["x0"]))
        if start is None and "通過順位" in s.replace(" ", ""):
            start = top
        elif start is not None and top > start + 4 and re.search(r"勝馬|市場|紹|〔|※|\(注\)", s):
            stop = top - 3.5        # 勝ち馬の名前は「勝馬の」の行より少し上に置かれている
            break
    if start is None:
        return {}, []
    stop = stop if stop is not None else start + 45
    keep, dots = [], []
    for c in chars:
        if not (start - 4.5 <= c["top"] < stop):
            continue
        t = norm(ctext(c))
        if t == "・" or t == "*":
            dots.append(c)
        elif c["top"] >= start - 1.5 and (t.isdigit() or t in "()" or t in SEP or re.fullmatch(r"\{\d+\}", t)):
            keep.append((c, t))    # {n}＝ラベルが丸数字などの字形になっている場合
    lines = []                                    # [top, [(char, text)]]
    for c, t in sorted(keep, key=lambda ct: ct[0]["top"]):
        if lines and abs(c["top"] - lines[-1][0]) <= 2.5:
            lines[-1][1].append((c, t))
        else:
            lines.append([c["top"], [(c, t)]])
    marks = collections.defaultdict(list)          # 行番号 → 「・」の中心 x
    for d in dots:
        below = [(ln[0] - d["top"], i) for i, ln in enumerate(lines) if 1.5 < ln[0] - d["top"] <= 9]
        if below:
            marks[min(below)[1]].append(d["x0"])
    pos, labels, cur = collections.defaultdict(dict), [], None
    state = {}                                      # ラベル → [count, grp(list or None)]
    for li, (top, cs) in enumerate(lines):
        cs.sort(key=lambda ct: ct[0]["x0"])
        # 間隔の広いところで区切る（ラベルと並びの間、左右2列の間）
        runs, run, last = [], [], None
        for c, t in cs:
            if last is not None and c["x0"] - last > 8:      # 並びの中は x0 の差が6以下、ラベルの前後は10以上
                runs.append(run); run = []
            run.append((c, t)); last = c["x0"]
        if run:
            runs.append(run)
        is_lab = [bool(re.fullmatch(r"[1-4]|\{\d+\}", "".join(t for _, t in r))) for r in runs]
        for ri, run in enumerate(runs):
            txt = "".join(t for _, t in run)
            # ラベル（何番目のコーナーか）。同じ行のすぐ右に並びが続くものだけ。番号は後で「最後が4」になるよう振る
            if is_lab[ri] and ri + 1 < len(runs) and not is_lab[ri + 1] and len(labels) < 4:
                cur = len(labels)
                labels.append(cur)
                state[cur] = [0, None, set()]
                continue
            run = [(c, t) for c, t in run if not t.startswith("{")]
            if cur is None:
                continue
            # 数字をまとめてトークンにする
            toks, num, nx0, nx1 = [], "", None, None
            for c, t in run:
                if t.isdigit():
                    if not num:
                        nx0 = c["x0"]
                    num += t; nx1 = c["x1"]
                else:
                    if num:
                        toks.append(("n", int(num), nx0)); num = ""
                    if t in "()":
                        toks.append((t, None, None))
            if num:
                toks.append(("n", int(num), nx0))
            lead = set()
            nums = [(x, h) for k, h, x in toks if k == "n"]
            for mx in marks.get(li, []):                  # 「・」は馬番の先頭の数字とほぼ同じ x（2桁なら数字の間）
                c2 = [(x, h) for x, h in nums if x <= mx + 1.5 and mx - x <= 6]
                if c2:
                    lead.add(max(c2)[1])
            # 重なって描かれた「)(」は x 順で「()」になるので戻す
            fixed, i = [], 0
            while i < len(toks):
                if toks[i][0] == "(" and i + 1 < len(toks) and toks[i + 1][0] == ")":
                    fixed += [(")", None, None), ("(", None, None)]; i += 2
                else:
                    fixed.append(toks[i]); i += 1
            st = state[cur]

            def close(st):
                g = st[1] or []
                p = st[0] + 1
                ld = [x for x in g if x in st[2]]
                for x in g:
                    pos[x][cur] = p if (not ld or x in ld) else p + len(ld)
                st[0] += len(g)
                st[1] = None

            st[2] |= lead
            for k, h, _ in fixed:
                if k == "(":
                    if st[1]:
                        close(st)
                    st[1] = []
                elif k == ")":
                    if st[1] is not None:
                        close(st)
                elif 1 <= h <= 18 and cur not in pos[h] and h not in (st[1] or []):
                    if st[1] is None:
                        st[0] += 1
                        pos[h][cur] = st[0]
                    else:
                        st[1].append(h)
    for cur, st in state.items():            # 閉じカッコが無いまま終わった集団
        if st[1]:
            g, p = st[1], st[0] + 1
            ld = [x for x in g if x in st[2]]
            for x in g:
                pos[x][cur] = p if (not ld or x in ld) else p + len(ld)
    n = len(labels)
    remap = {k: 4 - n + 1 + k for k in labels} if n <= 4 else {k: k + 1 for k in labels}
    pos2 = {h: {remap[k]: v for k, v in d.items()} for h, d in pos.items()}
    return pos2, [remap[k] for k in labels]


LAST_EXPECTED = None


def dedupe(chars):
    """太字の重ね打ち（同じ字をわずかにずらして2〜4回描く）を1つにまとめる"""
    seen, out = {}, []
    for c in chars:
        k = (c.get("text"), round(c["x0"]), round(c.get("size", 0), 1))
        hit = False
        for kk in (k, (k[0], k[1] - 1, k[2]), (k[0], k[1] + 1, k[2])):
            for o in seen.get(kk, ()):
                if abs(o["x0"] - c["x0"]) < 1.0 and abs(o["top"] - c["top"]) < 1.2:
                    hit = True
                    break
            if hit:
                break
        if not hit:
            seen.setdefault(k, []).append(c)
            out.append(c)
    return out


def parse_half(page, x0, x1, meta, src):
    chars = dedupe([c for c in page.chars if x0 <= c["x0"] < x1])
    if not chars:
        return None
    rx = lambda c: c["x0"] - x0
    rows = collections.defaultdict(list)
    for c in chars:
        rows[round(c["top"])].append(c)
    # 出走馬の行の目印：行の左端に数字だけが2〜3文字（枠＋馬番）。左右の段で表の位置がずれるので基準 x を測る
    cand = []
    for top in sorted(rows):
        cs = sorted((c for c in rows[top] if rx(c) < 60), key=lambda c: c["x0"])
        if not cs or not (4 <= rx(cs[0]) < 40):
            continue
        run, last = "", None
        for c in cs:
            t = ctext(c)
            if last is not None and c["x0"] - last > 3:
                break
            if not t.isdigit():
                run = ""
                break
            run += t
            last = c["x1"]
        if re.fullmatch(r"[1-8]\d{1,2}", run) and 1 <= int(run[1:]) <= 18:
            cand.append((top, rx(cs[0]), int(run[0]), int(run[1:])))
    if len(cand) < 2:
        return None
    xs = sorted(x for _, x, _, _ in cand)
    origin = xs[len(xs) // 2]
    off = origin - 21.0
    rx = lambda c: c["x0"] - x0 - off
    anchors = []
    for top, x, waku, num in cand:
        if abs(x - origin) >= 2.5 or (anchors and abs(top - anchors[-1][0]) < 4):
            continue
        # 見出し（賞金・条件の行）との取り違えを防ぐ：同じ高さの「重量」列に2桁の数字があること
        wz = "".join(ctext(c) for c in sorted((c for c in chars if abs(c["top"] - top) <= 3 and 132 <= rx(c) < 154), key=lambda c: c["x0"]))
        if re.match(r"^\d\d", wz):
            anchors.append((top, waku, num))
    if len(anchors) < 2:
        return None
    full = page.crop((x0, 0, x1, page.height))
    text = dec(full.extract_text() or "")
    head = dec(page.crop((x0, 0, x1, max(anchors[0][0] - 10, 1))).extract_text() or "")
    nh = norm(head)
    race = {k: "" for k in RACE_FIELDS}
    race.update(jo=meta["jo"], kai=meta["kai"], nichi=meta["nichi"], src=src)
    m = re.search(r"(\d{1,2})月(\d{1,2})日", nh)
    race["ymd"] = "%04d%02d%02d" % (meta["year"], int(m.group(1)), int(m.group(2))) if m else ""
    m = re.search(r"第\s*(\d{1,2})\s*競走", nh)
    race["rno"] = int(m.group(1)) if m else ""
    m = re.search(r"(晴|曇|小雨|雨|小雪|雪)\s*(良|稍重|重|不良)", nh)
    if m:
        race["weather"], race["cond"] = m.group(1), m.group(2)
    m = re.search(r"\((芝|ダート|障害)([^)]*)\)", nh)
    if m:
        race["surf"] = {"芝": "芝", "ダート": "ダ", "障害": "障"}[m.group(1)]
        inner = m.group(2)
        race["inout"] = "外" if "外" in inner else ("内" if "内" in inner else "")
        race["turn"] = "左" if "左" in inner else ("右" if "右" in inner else ("直" if "直" in inner else ""))
    line = next((l for l in nh.splitlines() if re.search(r"第\s*\d{1,2}\s*競走", l)), "") or \
        next((l for l in nh.splitlines() if "競走" in l), "")
    tail = re.split(r"第\s*\d{1,2}\s*競走", line, 1)[-1] if re.search(r"第\s*\d{1,2}\s*競走", line) else line.split("競走", 1)[-1]
    md = re.findall(r"(\d,\d{3}|\d{4})", tail)
    race["dist"] = int(md[-1].replace(",", "")) if md else ""
    after = re.sub(r"\{\d+\}", "", tail)
    after = re.sub(r"(\d,\d{3}|\d{4})\s*$", "", after).strip()
    race["name"] = after.replace(" ", "")
    if re.search(r"障害|ジャンプ|J・G|Ｊ・Ｇ|ハードル", race["name"]):
        race["surf"] = "障"
    if not race["cond"]:   # 障害戦は「(芝)良 / (ダート)稍重」の2段書き。芝の状態を採る
        mc = re.search(r"\(\s*芝\s*\)\s*(良|稍重|重|不良)", nh) or re.search(r"\(\s*ダート\s*\)\s*(良|稍重|重|不良)", nh)
        mw = re.search(r"日\s*(晴|曇|小雨|雨|小雪|雪)", nh)
        if mc:
            race["cond"] = mc.group(1)
        if mw and not race["weather"]:
            race["weather"] = mw.group(1)
    g = re.search(r"\((G[ⅠⅡⅢ123]|Ｇ[ⅠⅡⅢ]|J・G[ⅠⅡⅢ]|L|Ｌ|リステッド)\)", after.replace("Ｇ", "G").replace("Ｌ", "L"))
    race["grade"] = g.group(1) if g else ""
    cls = re.search(r"(新馬|未勝利|1勝クラス|2勝クラス|3勝クラス|オープン)", after) or \
        re.search(r"(新馬|未勝利|1勝クラス|2勝クラス|3勝クラス|オープン)", nh.replace("除未出走馬および未勝利馬", "").replace("未出走", ""))
    race["cls_txt"] = cls.group(1) if cls else ("" if race["grade"] else "オープン")   # 条件の書かれない特別戦はオープン
    nt = norm(text)
    m = re.search(r"ハロンタイム\s*([\d.―\-]+)", nt)
    if m:
        laps = [x for x in re.split(r"[―\-]", m.group(1)) if re.fullmatch(r"\d{1,2}\.\d", x)]
        race["laps"] = "-".join(laps)
        if len(laps) >= 3:
            race["first3"] = "%.1f" % sum(float(x) for x in laps[:3])
            race["last3"] = "%.1f" % sum(float(x) for x in laps[-3:])
    m = re.search(r"上り\s*4F\s*([\d.]+)\s*―\s*3F\s*([\d.]+)", nt)
    if m:
        race["last4"], race["last3"] = m.group(1), m.group(2)
    m = re.search(r"\((\d{1,2})頭\)", nt)
    race["heads"] = int(m.group(1)) if m else len(anchors)
    cpos, labels = corner_region(chars)
    race["corner_labels"] = "-".join(str(x) for x in labels)

    # 出走馬
    runs, prev_time, place, last_pl = [], None, 0, 0
    for i, (top, waku, num) in enumerate(anchors):
        lo = (anchors[i - 1][0] + top) / 2 if i else top - 6
        hi = (top + anchors[i + 1][0]) / 2 if i + 1 < len(anchors) else top + 5.5
        blk = [c for c in chars if lo <= c["top"] < hi]
        zn = [c for c in blk if 45 <= rx(c) < 132 and c["size"] >= 4.5]
        # 最後の馬の下にある「（16頭）」の行を除く
        byrow = collections.defaultdict(list)
        for c in zn:
            byrow[round(c["top"])].append(c)
        drop = {t for t, cs in byrow.items() if re.search(r"\d+頭", norm(zone_text(cs)))}
        zn = [c for c in zn if round(c["top"]) not in drop]
        # 太字の重ね打ちで、ずらし幅が大きいもの（字送りの6割未満で同じ字が続く）も1つにまとめる
        kept = []
        for c in sorted(zn, key=lambda c: (c["x0"], c["top"])):
            if kept and ctext(kept[-1]) == ctext(c) and re.match(r"^[ァ-ヶー]$", ctext(c)) and c["x0"] - kept[-1]["x0"] < 0.6 * c["size"] and abs(c["top"] - kept[-1]["top"]) < 1.2:
                continue
            kept.append(c)
        zn = kept
        zw = [c for c in blk if 132 <= rx(c) < 154]
        zj = [c for c in blk if 154 <= rx(c) < 190.5 and c["size"] >= 4.5 and ctext(c) not in "()（）・"]
        zt = [c for c in blk if 300 <= rx(c) < 379]
        zo = [c for c in blk if 379 <= rx(c) < 401]
        ntx = norm(zone_text(zn)).replace(" ", "")
        mm = re.match(r"^(.+?)(牡|牝|セ|せん|\{\d+\})(\d{1,2})(.*)$", ntx)
        horse, sex, age = (mm.group(1), mm.group(2), int(mm.group(3))) if mm else (ntx, "", "")
        if sex.startswith("{") or sex == "せん":
            sex = "セ"   # せん馬（PDFでは外字）
        mwt = re.search(r"(\d\d(?:\.\d)?)", norm(zone_text(zw)))
        wt = mwt.group(1) if mwt else ""
        jockey = norm(zone_text(zj)).replace(" ", "").replace("．", ".")
        jockey = re.sub(r"^\{\d+\}島", "鮫島", jockey)     # 外字（鮫）
        tt = norm(zone_text(zt)).replace(" ", "")
        hw = hwd = tm = ""
        status = ""
        mw = re.search(r"(\d{3})([+\-±―]?)", tt)   # 先頭に馬装（B＝ブリンカー等）が混ざることがある。―＝初出走で増減なし
        rest = tt
        if mw:
            hw = int(mw.group(1))
            rest = tt[mw.end():]
            mt = re.match(r"^(\d{0,2}?)(\d:\d\d\.\d|\d\d\.\d)", rest)
            if mt:
                d = int(mt.group(1) or 0)
                hwd = "" if mw.group(2) in ("―", "") else (0 if mw.group(2) == "±" else (d if mw.group(2) == "+" else -d))
                tm = mt.group(2)
            else:
                md2 = re.match(r"^(\d{1,2})", rest)
                hwd = (0 if mw.group(2) == "±" else (int(md2.group(1)) * (1 if mw.group(2) == "+" else -1))) if md2 else ""
        blk_txt = norm("".join(ctext(c) for c in blk))
        for k in ("取消", "除外", "中止", "失格", "降着"):
            if k in blk_txt:
                status = k
        if not tm and "〃" in tt and prev_time:
            tm = prev_time
        if tm:
            prev_time = tm
        odds = norm(zone_text(zo)).strip()
        odds = odds if re.fullmatch(r"\d+\.\d", odds) else ""
        if status in ("取消", "除外", "中止", "失格") or not tm:
            pl = ""
        else:
            place += 1
            pl = last_pl if ("同着" in tt and last_pl) else place
            last_pl = pl
        cp = cpos.get(num, {})
        cs = [cp.get(l, "") for l in labels]
        runs.append({"ymd": race["ymd"], "jo": race["jo"], "kai": race["kai"], "nichi": race["nichi"], "rno": race["rno"],
                     "horse": horse, "place": pl, "status": status, "waku": waku, "num": num, "sex": sex, "age": age,
                     "wt": wt, "jockey": jockey, "hw": hw, "hwd": hwd, "time": tm, "odds": odds, "pop": "",
                     "c1": cs[0] if cs else "", "c2": "", "c3": "", "c4": cs[-1] if cs else "",
                     "corners": "-".join(str(x) for x in cs if x != "")})
    # 同タイムで同着の可能性は扱わない（並び順＝着順）。人気は単勝オッズの順
    od = sorted((float(r["odds"]), r["num"]) for r in runs if r["odds"])
    rank = {n: i + 1 for i, (_, n) in enumerate(od)}
    for r in runs:
        r["pop"] = rank.get(r["num"], "")
    return race, runs


def parse_pdf(path):
    import pdfplumber
    meta = parse_meta_from_name(path)
    if not meta:
        return [], []
    races, runs = [], []
    global LAST_EXPECTED
    LAST_EXPECTED = None
    with pdfplumber.open(path) as pdf:
        for pi, page in enumerate(pdf.pages):
            try:   # 太字（同じ字を重ね打ち）のレース。本文は parse_half の dedupe、見出しの文章はこちらで処理
                page = page.dedupe_chars(tolerance=1)
            except Exception:
                pass
            w = page.width
            for x0, x1 in ((0, w / 2), (w / 2, w)):
                try:
                    r = parse_half(page, x0, x1, meta, "%s#p%d%s" % (os.path.basename(path), pi + 1, "L" if x0 == 0 else "R"))
                except Exception as e:   # 1レースの失敗で全体を止めない
                    print("WARN", path, pi + 1, x0, repr(e), file=sys.stderr)
                    r = None
                if r:
                    races.append(r[0]); runs.extend(r[1])
                elif LAST_EXPECTED is None:
                    t = norm(dec(page.crop((x0, 0, x1, page.height)).extract_text() or ""))
                    m = re.search(r"第\s*\d+\s*日.*?競走回数\s*(\d+)\s*回", t, re.S)
                    if m:
                        LAST_EXPECTED = int(m.group(1))
    return races, runs


def parse_pdf_checked(path):
    """(races, runs, PDFの集計ページに書かれた実施レース数) を返す。取りやめのあった日は12より少ない"""
    r, u = parse_pdf(path)
    return r, u, LAST_EXPECTED


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--out-races", required=True)
    ap.add_argument("--out-runs", required=True)
    a = ap.parse_args()
    R, U = [], []
    for p in a.pdfs:
        r, u = parse_pdf(p)
        R += r; U += u
    for path, rows, fields in ((a.out_races, R, RACE_FIELDS), (a.out_runs, U, RUN_FIELDS)):
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader(); w.writerows(rows)
    print("races", len(R), "runs", len(U))


if __name__ == "__main__":
    main()
