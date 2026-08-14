#!/usr/bin/env python3
"""録画済み RAW をオフライン処理して、1 CPU 秒あたりの処理能力を測る.

    source env.sh
    python scripts/offline_bench.py samples/dense.raw
    python scripts/offline_bench.py samples/dense.raw --loops 20   # 遅いマシン向け

Raspberry Pi 検証の最初の一歩。カメラもプラグインも不要で、
OpenEB を arm64 でビルドできればこのスクリプトと samples/ の RAW だけで
「Pi の CPU で足りるか」が決着する。母艦（Intel Core Ultra 7 265K）の実測:

    デコードのみ            164.0 Mev/CPU秒
    + フレーム生成          135.0
    + フレーム生成 + JPEG   109.3

センサ上限は 50 Mev/s、実運用の負荷は 0.03〜6 Mev/s。

注意: ライブカメラでこの測定はできない。EventsIterator の消費ループは
カメラ待ちの間もビジーで 1 コアを回し続けるため、処理コストが埋もれる。
（ファイル入力なら待ちが無いので正しく測れる。ファイルに対する
EventsIterator の作り直しは安全 — double free するのはライブデバイスのみ）
"""

import argparse
import os
import time

import cv2
import metavision_sdk_core as msc
from metavision_core.event_io import EventsIterator


def cpu_now() -> float:
    t = os.times()
    return t[0] + t[1]  # user + system


def run(path: str, loops: int, do_frame: bool, do_jpeg: bool) -> tuple[int, float, float]:
    """(処理イベント数, CPU 秒, 1 周のセンサ時間 [s]) を返す。"""
    n = 0
    t_last = 0
    c0 = cpu_now()
    for _ in range(loops):
        it = EventsIterator(input_path=path, mode="delta_t", delta_t=20000)
        h, w = it.get_size()
        fg = None
        if do_frame:
            fg = msc.PeriodicFrameGenerationAlgorithm(w, h, 20000, 20.0)

            def on_frame(ts, frame):
                if do_jpeg:
                    cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])

            fg.set_output_callback(on_frame)
        for evs in it:
            n += len(evs)
            if len(evs):
                t_last = int(evs["t"][-1])
                if fg is not None:
                    fg.process_events(evs)
        del it
    return n, cpu_now() - c0, t_last / 1e6


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="RAW ファイル（samples/dense.raw など）")
    ap.add_argument("--loops", type=int, default=0,
                    help="繰り返し回数。0 なら CPU 1 秒ぶん以上になるよう自動調整")
    args = ap.parse_args()

    size = os.path.getsize(args.input)
    loops = args.loops
    if loops <= 0:
        # まず 1 周してかかった CPU 時間から、合計 1 CPU 秒以上になる回数を決める
        n, cpu, _ = run(args.input, 1, False, False)
        loops = max(1, int(1.0 / max(cpu, 1e-3)))
        print(f"1 周 = {n:,} イベント / {cpu:.3f} CPU秒 → {loops} 回繰り返して測定")
    print(f"入力: {args.input} ({size / 1e6:.1f} MB) x {loops} 周")
    print()
    # コストは 2 成分ある:
    #   - イベント数に比例する成分（デコード）→ Mev/CPU秒 で見る
    #   - 実時間に比例する成分（フレーム生成と JPEG は 20fps 固定）→ コア使用率で見る
    # 疎なシーンでは後者が支配的になるので、Mev/CPU秒 だけ見ると誤読する。
    print(f"{'処理':<26}{'events':>12}{'CPU':>9}{'Mev/CPU秒':>11}{'コア使用率':>10}")
    for label, do_frame, do_jpeg in (("(1) デコードのみ", False, False),
                                     ("(2) + フレーム生成", True, False),
                                     ("(3) + フレーム生成 + JPEG", True, True)):
        n, cpu, dur = run(args.input, loops, do_frame, do_jpeg)
        util = cpu / (loops * dur) if dur > 0 else 0.0
        print(f"{label:<26}{n / 1e6:>11.1f}M{cpu:>9.2f}{n / cpu / 1e6:>11.1f}"
              f"{util * 100:>9.1f}%")
    print()
    print("判定: (3) のコア使用率（実時間でこのデータを流したときの 1 コア占有率）が")
    print("100% を大きく下回っていれば録画サーバは 1 コアで足りる。")
    print("疎・密の両方のサンプルで確認すること。")


if __name__ == "__main__":
    main()
