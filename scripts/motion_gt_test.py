#!/usr/bin/env python3
"""正解つきで動き検知の真陽性・偽陽性を測る.

    source env.sh
    python scripts/motion_gt_test.py [-- motion_probe への追加引数...]

画面に「動いてください / 止まってください」を交互に出し、その区間を正解として
検知が実際にどちらの区間に落ちたかを集計する。

これが要る理由: 静止シーンだけでは偽陽性しか測れず、逆に人が写っているだけでは
「本当に動いていたか」が分からない。実際、被写体が座っているだけの状態では
イベントレートが白壁より低くなり（0.004 Mev/s）、周辺部のノイズが支配的だった。
検知が 0 でもフィルタのせいなのか動きが無かったせいなのか切り分けられない。

タイミングのずれに強くするため、区間は数秒単位に取り、境界付近は集計から除く。
"""

import re
import subprocess
import sys
import time
from pathlib import Path

# (ラベル, 秒数) — MOVE 区間で検知されるべき、STILL 区間では検知されるべきでない
SCHEDULE = [
    ("STILL", 5),
    ("MOVE", 8),
    ("STILL", 6),
    ("MOVE", 8),
    ("STILL", 5),
]
EDGE_GUARD = 1.0  # 区間境界の前後この秒数は集計から除く（人間の反応時間ぶん）

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "build" / "motion_probe"


def main() -> int:
    extra = sys.argv[1:]
    if extra and extra[0] == "--":
        extra = extra[1:]

    total = sum(d for _, d in SCHEDULE)
    if not PROBE.exists():
        print(f"ERROR: {PROBE} が無い。scripts/build_tools.sh を先に実行", file=sys.stderr)
        return 1

    cmd = [str(PROBE), "--seconds", str(total), "--warmup", "0", *extra]
    print("=" * 56)
    print("  合図に従って動いてください。")
    print("  MOVE  = カメラの前で手を振る / 体を動かす")
    print("  STILL = できるだけ動かない")
    print("=" * 56)
    print(f"  設定: {' '.join(extra) if extra else '(既定)'}")
    print()

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    # motion_probe の t_host は camera.start() 基準。初期化に数秒かかるので、
    # 最初の出力行（設定サマリ）が出たあとを基準に合図を始める。
    assert proc.stdout is not None
    header = proc.stdout.readline()
    print(f"  {header.strip()}")
    print()

    t0 = time.monotonic()
    spans = []
    at = 0.0
    for label, dur in SCHEDULE:
        spans.append((label, at, at + dur))
        at += dur

    # 合図を別スレッドで出しつつ、本体の stdout を読む
    import threading

    def cue():
        for label, start, end in spans:
            while time.monotonic() - t0 < start:
                time.sleep(0.05)
            mark = "▶▶ 動いてください" if label == "MOVE" else "■■ 止まってください"
            print(f"  [{start:5.1f}s] {mark}  ({end - start:.0f} 秒)", flush=True)

    th = threading.Thread(target=cue, daemon=True)
    th.start()

    dets = []
    tail = []
    pat = re.compile(r"DETECT t_host=\s*([0-9.]+) s")
    for line in proc.stdout:
        m = pat.match(line)
        if m:
            dets.append(float(m.group(1)))
        else:
            tail.append(line.rstrip())
    proc.wait()

    # t_host は camera.start() 基準、合図は「最初の行を読んだ時刻」基準。
    # 両者のずれは初期化ぶんで未知なので、推定せずに済むよう区間を広く取っている。
    print()
    print("=" * 56)
    print(f"  検知 {len(dets)} 件")
    print()

    # 1 秒ビンのタイムライン。合図と検知が目で対応するかを見る
    print("  タイムライン（1 秒ごと / █ = 検知あり、数値 = 件数）")
    nbins = int(total) + 1
    bins = [0] * nbins
    for t in dets:
        i = int(t)
        if 0 <= i < nbins:
            bins[i] += 1
    labels = []
    for i in range(nbins):
        lab = "?"
        for name, s, e in spans:
            if s <= i < e:
                lab = "M" if name == "MOVE" else "."
        labels.append(lab)
    print("    合図: " + "".join(labels))
    print("    検知: " + "".join("█" if b else " " for b in bins))
    print(f"    件数: {max(bins) if bins else 0} 件/秒 が最大")
    print()

    # 境界付近を除いた集計
    def count_in(label):
        n = 0.0
        dur = 0.0
        for name, s, e in spans:
            if name != label:
                continue
            a, b = s + EDGE_GUARD, e - EDGE_GUARD
            if b <= a:
                continue
            dur += b - a
            n += sum(1 for t in dets if a <= t < b)
        return n, dur

    tp, tp_dur = count_in("MOVE")
    fp, fp_dur = count_in("STILL")
    print(f"  MOVE  区間 ({tp_dur:.0f}s, 境界±{EDGE_GUARD:.0f}s を除く): {int(tp):5d} 件  = {tp/tp_dur if tp_dur else 0:7.2f} /s")
    print(f"  STILL 区間 ({fp_dur:.0f}s, 同上):                 {int(fp):5d} 件  = {fp/fp_dur if fp_dur else 0:7.2f} /s")
    if fp_dur and tp_dur:
        r_move, r_still = tp / tp_dur, fp / fp_dur
        if r_still > 0:
            print(f"  → 分離比 {r_move / r_still:.1f}x")
        elif r_move > 0:
            print("  → STILL 区間で 0 件。完全に分離")
        else:
            print("  → どちらも 0 件。動きが足りないか閾値が厳しすぎる")
    print()
    for line in tail:
        if line.strip():
            print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
