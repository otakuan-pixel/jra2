#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""collect_results.py — JRA公式の成績PDFを集めて、数字だけを data/results/ に保存する

  python3 tools/collect_results.py --years 2021-2026 --tracks kyoto,tokyo,fukushima [--cache pdfwork] [--offline]

- 取得済み（data/results/index.csv に載っている）PDFは飛ばす差分方式。既存のデータは消さない
- PDF本体はリポジトリに入れない（容量が約1GBになるため）。保存するのは読み取った結果だけ
    data/results/<年>/<場>_races.csv.gz   1行＝1レース（条件・馬場・ハロンタイム・上がり）
    data/results/<年>/<場>_runs.csv.gz    1行＝1頭（着順・タイム・通過順・騎手・斤量・馬体重・単勝オッズ・人気）
- --cache のフォルダに同名のPDFがあればダウンロードせずそれを使う（中山・阪神の保存済みPDF用）
"""
import argparse, re, csv, gzip, os, sys, time, traceback
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jra_pdf as J

BASE = "https://www.jra.go.jp/datafile/seiseki/report/{y}/{y}-{k}{t}{n}.pdf"
ALL = ["kyoto", "tokyo", "fukushima", "nakayama", "hanshin", "chukyo", "niigata", "kokura", "sapporo", "hakodate"]
ROOT = Path("data/results")
IDX_FIELDS = ["file", "year", "kai", "track", "nichi", "races", "runs", "bytes"]


def years(spec):
    spec = (spec or "").strip()
    if "-" in spec:
        a, b = spec.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in spec.split(",") if x.strip()]


def read_gz(path):
    if not path.exists():
        return []
    with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_gz(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", required=True)
    ap.add_argument("--tracks", default=",".join(ALL))
    ap.add_argument("--cache", default="pdfwork")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--redo", default="", help="取り直すPDF名を書いたファイル（1行1件。後ろに keep と書くとPDFを data/debug/pdf に残す）")
    a = ap.parse_args()
    cache = Path(a.cache)
    idx_path = ROOT / "index.csv"
    index = {}
    if idx_path.exists():
        index = {r["file"]: r for r in csv.DictReader(open(idx_path, encoding="utf-8"))}
    keep_pdf, redo_names, redone = set(), set(), set()
    if a.redo and Path(a.redo).exists():   # 読み取りを直した後の取り直し：一覧にある日は「未取得」に戻す
        for line in open(a.redo, encoding="utf-8"):
            w = line.split("#")[0].split()
            if not w:
                continue
            if not any(re.match(r"^\d{4}-\d%s\d+\.pdf$" % t.strip().lower(), w[0]) for t in a.tracks.split(",") if t.strip()):
                continue   # 今回の対象でない競馬場の分には触らない（索引から消したままにしない）
            if int(w[0][:4]) not in set(years(a.years)):
                continue   # 今回の対象でない年の分にも触らない
            redo_names.add(w[0])
            index.pop(w[0], None)
            if len(w) > 1 and w[1] == "keep":
                keep_pdf.add(w[0])
    s = None
    if not a.offline:
        import requests
        s = requests.Session()
        s.headers.update({"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                          "Accept": "application/pdf,*/*", "Accept-Language": "ja,en;q=0.8"})

    def get(y, k, t, n):
        """PDFの場所を返す（無ければ None）。キャッシュ → ダウンロード"""
        name = "%d-%d%s%d.pdf" % (y, k, t, n)
        for p in (cache / name, cache / t / name, cache / str(y) / t / name):
            if p.exists():
                return p
        if a.offline:
            return None
        for i in range(2):
            try:
                r = s.get(BASE.format(y=y, k=k, t=t, n=n), timeout=90)
                if r.status_code == 200 and r.content[:4] == b"%PDF":
                    p = cache / str(y) / t / name
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(r.content)
                    time.sleep(0.4)
                    return p
                if r.status_code in (403, 404):
                    return None
            except Exception as e:
                print("  EXC", name, repr(e), flush=True)
            time.sleep(2 * (i + 1))
        return None

    for t in [x.strip().lower() for x in a.tracks.split(",") if x.strip()]:
        for y in years(a.years):
            todo, known = [], 0
            for k in range(1, 8):
                hits = misses = 0
                for n in range(1, 16):
                    name = "%d-%d%s%d.pdf" % (y, k, t, n)
                    if name in index:
                        hits += 1; known += 1; misses = 0
                        continue
                    p = get(y, k, t, n)
                    if p is None:
                        misses += 1
                        if misses >= 2:
                            break
                        continue
                    misses = 0; hits += 1
                    todo.append(p)
                if hits == 0 and not a.offline:
                    break
            if not todo:
                print("[%d %s] 追加なし（取得済み %d 日）" % (y, t, known), flush=True)
                continue
            races, runs, bad_days = [], [], []
            with ProcessPoolExecutor(a.workers) as ex:
                for p, (r, u, exp) in zip(todo, ex.map(J.parse_pdf_checked, [str(p) for p in todo])):
                    races += r; runs += u
                    if exp is not None and exp != len(r):
                        print("  ※読めたレース数がPDFの実施数と違う: %s 読めた %d / 実施 %d" % (p.name, len(r), exp), flush=True)
                        bad_days.append(p.name)
                    index[p.name] = {"file": p.name, "year": y, "kai": p.name.split("-")[1][0], "track": t,
                                     "nichi": "".join(ch for ch in p.name.split(t)[-1] if ch.isdigit()),
                                     "races": len(r), "runs": len(u), "bytes": p.stat().st_size}
            for p in todo:
                if p.name in keep_pdf:
                    dbg = ROOT.parent / "debug" / "pdf"
                    dbg.mkdir(parents=True, exist_ok=True)
                    (dbg / p.name).write_bytes(p.read_bytes())
            new_src = {p.name for p in todo}
            redone |= new_src & redo_names
            rp, up = ROOT / str(y) / ("%s_races.csv.gz" % t), ROOT / str(y) / ("%s_runs.csv.gz" % t)
            old_r = [x for x in read_gz(rp) if x["src"].split("#")[0] not in new_src]
            keep = {(x["ymd"], x["rno"]) for x in old_r}
            old_u = [x for x in read_gz(up) if (x["ymd"], x["rno"]) in keep]
            R = sorted(old_r + races, key=lambda x: (str(x["ymd"]), int(x["rno"] or 0)))
            U = sorted(old_u + runs, key=lambda x: (str(x["ymd"]), int(x["rno"] or 0), int(x["place"] or 99), int(x["num"] or 0)))
            write_gz(rp, R, J.RACE_FIELDS)
            write_gz(up, U, J.RUN_FIELDS)
            bad = [p.name for p in todo if p.name in bad_days]
            print("[%d %s] +%d 日（計 %d 日）レース %d・出走 %d%s" % (y, t, len(todo), known + len(todo), len(R), len(U),
                                                         "  ※要確認: " + ",".join(bad) if bad else ""), flush=True)
            ROOT.mkdir(parents=True, exist_ok=True)
            with open(idx_path, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=IDX_FIELDS)
                w.writeheader()
                w.writerows(sorted(index.values(), key=lambda r: (int(r["year"]), r["track"], int(r["kai"]), int(r["nichi"]))))
    if redone:   # 取り直せた分は一覧から外す（残っていると次回も取り直してしまう）
        keep_lines = []
        for line in open(a.redo, encoding="utf-8"):
            w = line.split("#")[0].split()
            if w and w[0] in redone:
                continue
            keep_lines.append(line)
        with open(a.redo, "w", encoding="utf-8") as f:
            f.writelines(keep_lines)
        print("取り直し一覧から %d 件を外しました（残り %d 行）" % (len(redone), sum(1 for l in keep_lines if l.split("#")[0].strip())), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
