#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""race_video.py — シミュレーションの1回分を「馬が走る」イメージ動画にする

  python3 -I race_video.py --engine keiba_sim.py --db db --race 20261108:京都:11 --going "良:1" --out out
  （--preview を付けると、本番前に3コマだけPNGで出す）

出力: race_<ID>.mp4（1280x720・YouTubeやX向け）、race_<ID>.gif（幅620px・noteに直接貼る用）
再生するのは、N回のうち「最も勝つ回数が多い馬が勝ち、全体の着順が平均に最も近い1回」。
"""
import argparse, glob, hashlib, importlib.util, math, os, subprocess, sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1280, 720, 30
HEAD, SIDE, FOOT = 84, 214, 22
WAKU = {"1": ((255, 255, 255), (17, 17, 17)), "2": ((34, 34, 34), (255, 255, 255)), "3": ((216, 53, 42), (255, 255, 255)),
        "4": ((31, 99, 181), (255, 255, 255)), "5": ((242, 205, 31), (17, 17, 17)), "6": ((45, 138, 78), (255, 255, 255)),
        "7": ((239, 138, 28), (17, 17, 17)), "8": ((241, 156, 184), (17, 17, 17))}
COATS = [(122, 74, 42), (122, 74, 42), (168, 98, 45), (66, 46, 36), (66, 46, 36), (150, 150, 152), (36, 30, 28)]


def font(size, bold=False):
    for pat in (("NotoSansCJK-Bold" if bold else "NotoSansCJK-Regular"), "NotoSansCJK", "NotoSansJP", "ipaexg", "ipag", "DejaVuSans"):
        hits = [f for f in glob.glob("/usr/share/fonts/**/*", recursive=True) if pat.lower() in os.path.basename(f).lower()]
        if hits:
            return ImageFont.truetype(sorted(hits)[0], size)
    return ImageFont.load_default()


def chip(dr, x, y, w, h, waku, text, f):
    bg, fg = WAKU.get(str(waku), ((238, 238, 238), (17, 17, 17)))
    dr.rounded_rectangle((x, y, x + w, y + h), radius=4, fill=bg, outline=(110, 110, 105), width=1)
    dr.text((x + w / 2, y + h / 2 - 1), str(text), font=f, fill=fg, anchor="mm")


def draw_horse(dr, x, y, s, coat, waku, phase):
    """x=鼻先のx、y=胴の中心。s=拡大率"""
    dark = tuple(int(c * 0.72) for c in coat)
    y = y + 1.5 * s * math.sin(2 * phase)
    P = lambda dx, dy: (x + dx * s, y + dy * s)
    dr.line([P(-44, -2), P(-52, 3 + 2 * math.sin(phase))], fill=dark, width=max(2, int(3 * s)))          # 尾
    for j, (bx, by) in enumerate(((-39, 4), (-39, 4), (-21, 5), (-21, 5))):                               # 脚4本
        ph = phase + (0, 2.4, 3.3, 5.6)[j]
        a = 0.75 * math.sin(ph)
        b = 0.9 * max(0.0, math.cos(ph)) * (1 if j < 2 else -1)
        k = (bx + 8 * math.sin(a), by + 8 * math.cos(a))
        f_ = (k[0] + 8 * math.sin(a - b), k[1] + 8 * math.cos(a - b))
        dr.line([P(bx, by), P(*k), P(*f_)], fill=dark if j % 2 else coat, width=max(2, int(2.6 * s)), joint="curve")
    dr.ellipse((*P(-46, -6), *P(-15, 9)), fill=coat)                                                       # 胴
    dr.polygon([P(-20, -3), P(-10, -13), P(-5, -9), P(-15, 6)], fill=coat)                                 # 首
    dr.polygon([P(-11, -14), P(-2, -9), P(0, -5), P(-7, -5)], fill=coat)                                   # 頭
    dr.polygon([P(-11, -14), P(-9, -18), P(-8, -13)], fill=dark)                                           # 耳
    bg, _ = WAKU.get(str(waku), ((238, 238, 238), (17, 17, 17)))
    dr.ellipse((*P(-33, -13), *P(-21, -4)), fill=bg, outline=(60, 60, 60), width=1)                        # 騎手の胴
    dr.ellipse((*P(-23, -17), *P(-17, -11)), fill=bg, outline=(60, 60, 60), width=1)                       # 帽子
    dr.rectangle((*P(-36, -3), *P(-27, 4)), fill=(250, 250, 250), outline=(120, 120, 120), width=1)        # ゼッケン


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True); ap.add_argument("--db", required=True); ap.add_argument("--race", required=True)
    ap.add_argument("--going", default="良:1"); ap.add_argument("--n", type=int, default=10000); ap.add_argument("--out", default="out")
    ap.add_argument("--secs", type=float, default=24.0); ap.add_argument("--preview", action="store_true")
    a = ap.parse_args()
    spec = importlib.util.spec_from_file_location("ks", a.engine); ks = importlib.util.module_from_spec(spec); spec.loader.exec_module(ks)
    import numpy as np
    races, runs = ks.load_db(a.db)
    race = ks.find_race(races, spec=a.race)[0]
    going = ks.parse_going(a.going)
    o = ks.simulate(ks.Model(runs, asof=race["date"]), race, going=going, n=a.n, raw=True)
    raw, hs = o["_raw"], o["horses"]
    m, D = len(hs), race["dist"]
    rank = raw["rank"]
    fav = int(np.argmax([h["win"] for h in hs]))
    avg = rank.mean(axis=0)
    cand = np.where(rank[:, fav] == 0)[0]
    it = int(cand[np.argmin(np.abs(rank[cand] - avg[None, :]).sum(axis=1))])
    T = raw["times"][it] if raw["times"] is not None else (61.0 - raw["perf"][it]) * D / 1000.0
    rel, pz = raw["rel"][it], float(raw["pz"][it])
    order = np.argsort(T)
    # 残り600m地点の通過：先頭からの遅れ＝テンの位置に比例。上がり3Fが非現実的に速くならないよう間隔を詰める
    L0 = o.get("course_last3") or (35.2 if race["surf"] == "芝" else 37.3)
    L_lead = L0 + 0.5 * pz
    lo = 33.0 if race["surf"] == "芝" else 34.8
    G = 1.6 + 0.4 * m / 18.0
    t1_lead = T.min() - L_lead + 0.0
    for _ in range(40):
        t1 = t1_lead + G * rel
        if (T - t1).min() >= lo:
            break
        G *= 0.9; t1_lead -= 0.05
    t1 = t1_lead + G * rel
    front = D - 600.0

    def pos(tau):
        u = np.clip(tau / t1, 0, 1)
        ease = (u - 0.03 * (1 - np.exp(-u / 0.03))) / (1 - 0.03 * (1 - math.exp(-1 / 0.03)))
        s1 = front * ease
        s2 = front + 600.0 * (tau - t1) / (T - t1)
        return np.where(tau <= t1, s1, s2)

    T_end = float(T.min()) + 2.6
    k = T_end / a.secs                                   # 動画1秒あたりのレース時間
    Vw = 72.0                                            # 画面に入る距離（m）
    px_m = (W - SIDE) / Vw
    lane_h = min(52.0, (H - HEAD - FOOT - 24) / m)
    sc = min(1.5, lane_h / 34.0)
    turf = race["surf"] != "ダ"
    G1, G2 = ((63, 143, 79), (72, 156, 90)) if turf else ((172, 134, 88), (181, 143, 97))
    f_h1, f_h2, f_big, f_lab, f_chip, f_s = font(30, True), font(17), font(34, True), font(15), font(14, True), font(13)
    surf = {"芝": "芝", "ダ": "ダート", "障": "障害"}[race["surf"]]
    d = race["date"]
    gtxt = "・".join("%s%d%%" % (g, round(v * 100)) for g, v in going.items()) if len(going) > 1 else list(going)[0]
    sub = "%d/%d %s%dR　%s%dm%s　%d頭　馬場想定：%s" % (int(d[4:6]), int(d[6:]), race["venue"], race["r"], surf, D, race["inout"], m, gtxt)
    coats = [COATS[int(hashlib.md5(h["name"].encode()).hexdigest(), 16) % len(COATS)] for h in hs]
    lane_y = [HEAD + 8 + lane_h * (i + 0.5) for i in range(m)]

    def frame(tau, vt):
        im = Image.new("RGB", (W, H), G1)
        dr = ImageDraw.Draw(im)
        s = pos(tau)
        lead = float(s.max())
        cam = min(lead - 0.78 * Vw, D - 0.70 * Vw)
        X = lambda dist: SIDE + (dist - cam) * px_m
        y0, y1 = HEAD, H - FOOT
        st0 = math.floor(cam / 10.0)
        for i in range(st0, st0 + int(Vw / 10) + 2):       # 芝目（10mごとの縞）で流れを見せる
            if i % 2 == 0:
                dr.rectangle((max(SIDE, X(i * 10)), y0, X(i * 10 + 10), y1), fill=G2)
        dr.line((SIDE, y0 + 3, W, y0 + 3), fill=(250, 250, 250), width=3)
        dr.line((SIDE, y1 - 3, W, y1 - 3), fill=(250, 250, 250), width=3)
        for rem in range(0, int(D), 200):                 # 残り距離の標識
            xx = X(D - rem)
            if SIDE - 40 < xx < W + 40:
                if rem == 0:
                    for r in range(int((y1 - y0) / 12)):
                        for c in range(2):
                            if (r + c) % 2 == 0:
                                dr.rectangle((xx + c * 6, y0 + r * 12, xx + c * 6 + 6, y0 + r * 12 + 12), fill=(250, 250, 250))
                            else:
                                dr.rectangle((xx + c * 6, y0 + r * 12, xx + c * 6 + 6, y0 + r * 12 + 12), fill=(20, 20, 20))
                else:
                    dr.line((xx, y0, xx, y1), fill=(235, 235, 235), width=1)
                    dr.rounded_rectangle((xx - 34, y0 + 8, xx + 34, y0 + 30), radius=4, fill=(250, 250, 250))
                    dr.text((xx, y0 + 18), "残り%d" % rem, font=f_s, fill=(30, 30, 30), anchor="mm")
        ph = vt * 2 * math.pi * 2.6
        for i, h in enumerate(hs):
            xx = X(float(s[i]))
            if xx > SIDE - 30:
                draw_horse(dr, xx, lane_y[i], sc, coats[i], h["waku"], ph + i * 1.7)
                chip(dr, xx - 80 * sc, lane_y[i] - 9, 24, 18, h["waku"], h["num"], f_chip)
        # 左の馬名欄
        dr.rectangle((0, HEAD, SIDE, H), fill=(248, 248, 246))
        dr.line((SIDE, HEAD, SIDE, H), fill=(200, 200, 195), width=1)
        fl_ = font(int(max(11, min(15, lane_h * 0.42))))
        for i, h in enumerate(hs):
            chip(dr, 10, lane_y[i] - 9, 24, 18, h["waku"], h["num"], f_chip)
            dr.text((42, lane_y[i] - 1), h["name"], font=fl_, fill=(20, 20, 20), anchor="lm")
        dr.rectangle((0, H - FOOT, W, H), fill=(248, 248, 246))
        dr.text((10, H - FOOT / 2 - 1), "%sのシミュレーションのうち、平均的な着順に最も近い1回の再生です。結果を予告するものではありません。" % ks.n_txt(a.n), font=f_s, fill=(82, 81, 78), anchor="lm")
        # 上の帯
        dr.rectangle((0, 0, W, HEAD), fill=(24, 30, 26))
        dr.text((18, 28), "%s　シミュレーション再生" % race["name"], font=f_h1, fill=(255, 255, 255), anchor="lm")
        dr.text((18, 63), sub, font=f_h2, fill=(200, 208, 202), anchor="lm")
        rem = max(0.0, D - lead)
        dr.text((W - 18, 28), "残り %s m" % format(int(round(rem / 10.0) * 10), ",") if rem > 0 else "ゴール", font=f_big, fill=(255, 255, 255), anchor="rm")
        top = np.argsort(-s)[:3] if rem > 0 else order[:3]
        xx = W - 18
        for j in list(top)[::-1]:
            chip(dr, xx - 26, 52, 26, 22, hs[j]["waku"], hs[j]["num"], f_chip); xx -= 32
        dr.text((xx - 6, 63), "現在の1〜3番手" if rem > 0 else "1〜3着", font=f_h2, fill=(200, 208, 202), anchor="rm")
        return im

    def still(kind):
        if kind == "intro":
            im = Image.new("RGB", (W, H), (24, 30, 26)); dr = ImageDraw.Draw(im)
            dr.text((W / 2, 250), race["name"], font=font(64, True), fill=(255, 255, 255), anchor="mm")
            dr.text((W / 2, 330), "シミュレーション再生", font=font(34), fill=(200, 208, 202), anchor="mm")
            dr.text((W / 2, 410), sub, font=font(24), fill=(255, 255, 255), anchor="mm")
            dr.text((W / 2, 520), "%sのシミュレーションのうち、平均的な着順に最も近い1回を再生します。" % ks.n_txt(a.n), font=font(22), fill=(200, 208, 202), anchor="mm")
            dr.text((W / 2, 556), "結果を予告するものではありません。", font=font(22), fill=(200, 208, 202), anchor="mm")
            return im
        im = frame(T_end, a.secs).point(lambda v: int(v * 0.35)); dr = ImageDraw.Draw(im)
        bx, by, bw, bh = 250, 110, 780, 500
        dr.rounded_rectangle((bx, by, bx + bw, by + bh), radius=14, fill=(250, 250, 248))
        dr.text((bx + 34, by + 46), "この回の着順", font=font(32, True), fill=(20, 20, 20), anchor="lm")
        dr.text((bx + bw - 34, by + 46), "勝ち時計 %s" % ks.fmt_time(round(float(T.min()), 1)), font=font(24), fill=(82, 81, 78), anchor="rm")
        for r_, j in enumerate(order[:5]):
            yy = by + 108 + r_ * 56
            dr.text((bx + 40, yy), "%d着" % (r_ + 1), font=font(26, True), fill=(20, 20, 20), anchor="lm")
            chip(dr, bx + 120, yy - 16, 40, 32, hs[j]["waku"], hs[j]["num"], font(20, True))
            dr.text((bx + 178, yy), hs[j]["name"], font=font(26, True), fill=(20, 20, 20), anchor="lm")
            dr.text((bx + bw - 190, yy), "" if r_ == 0 else ("僅差" if T[j] - T.min() < 0.05 else "+%.1f秒" % (T[j] - T.min())), font=font(22), fill=(82, 81, 78), anchor="rm")
            dr.text((bx + bw - 34, yy), "勝率 %s" % ks.pct(hs[j]["win"]), font=font(22), fill=(82, 81, 78), anchor="rm")
        dr.line((bx + 34, by + 392, bx + bw - 34, by + 392), fill=(221, 220, 214), width=1)
        dr.text((bx + 34, by + 424), "右端の勝率は、%s走らせたうち各馬が1着になった割合です。" % ks.n_txt(a.n), font=font(19), fill=(82, 81, 78), anchor="lm")
        dr.text((bx + 34, by + 456), "この動画はそのうちの1回で、結果を予告するものではありません。", font=font(19), fill=(82, 81, 78), anchor="lm")
        return im

    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    tag = "%s_%s_%02d" % (race["date"], race["jo"], race["r"])
    if a.preview:
        for nm, im in (("p1_intro", still("intro")), ("p2_early", frame(T_end * 0.12, 3.0)), ("p3_stretch", frame(float(T.min()) - 4.0, 20.0)),
                       ("p4_finish", frame(float(T.min()) + 0.15, 23.0)), ("p5_result", still("result"))):
            im.save(out / ("%s.png" % nm))
        print("preview →", out, "| 代表回 it=%d 勝ち馬 %s番 時計 %.1f G=%.2f 上がり最速 %.1f" % (it, hs[int(order[0])]["num"], T.min(), G, (T - t1).min()))
        return
    enc = ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", str(FPS)]
    parts = []
    for nm, dur in (("intro", 2.5), ("result", 5.0)):
        p = out / ("_%s.png" % nm); still(nm).save(p)
        tmp = out / ("_%s.tmp.mp4" % nm)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", str(p), "-vf", "format=yuv420p,loop=-1:1,trim=duration=%s" % dur] + enc + [str(tmp)], check=True)
        os.replace(tmp, out / ("_%s.mp4" % nm))
    tmp = out / "_race.tmp.mp4"
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H), "-framerate", str(FPS), "-i", "-"] + enc + [str(tmp)], stdin=subprocess.PIPE)
    hold = int(FPS * 0.8)
    nfr = int(a.secs * FPS)
    for fi in range(-hold, nfr + int(FPS * 0.6)):
        vt = max(0, fi) / FPS
        ff.stdin.write(frame(min(vt * k, T_end), vt).tobytes())
    ff.stdin.close()
    if ff.wait() != 0:
        sys.exit("ffmpeg failed")
    os.replace(tmp, out / "_race.mp4")
    lst = out / "_list.txt"
    lst.write_text("".join("file '%s'\n" % (out / ("_%s.mp4" % nm)).resolve() for nm in ("intro", "race", "result")))
    mp4_tmp, mp4 = out / ("race_%s.tmp.mp4" % tag), out / ("race_%s.mp4" % tag)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(mp4_tmp)], check=True)
    os.replace(mp4_tmp, mp4)
    gif = out / ("race_%s.gif" % tag)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp4), "-filter_complex",
                    "fps=10,scale=620:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse=dither=bayer:bayer_scale=4", str(gif)], check=True)
    for f in out.glob("_*"):
        f.unlink()
    print("出力:", mp4, "%.1fMB" % (mp4.stat().st_size / 1e6), "|", gif, "%.1fMB" % (gif.stat().st_size / 1e6))


if __name__ == "__main__":
    main()
