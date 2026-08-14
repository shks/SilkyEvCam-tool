#!/usr/bin/env python
"""録画済み RAW をオフライン処理して 1 CPU 秒あたりの処理量を測る.

README「性能は問題にならない（実測）」の表を再現するためのスクリプト。
母艦（Intel Core Ultra 7 265K）で測った値と Raspberry Pi 5 を比べるのが目的なので、
**recorder と同じ経路・同じパラメータ**を通すこと自体が要件になっている:

- EventsIterator を mode="mixed" / delta_t=20000 / n_events=200_000 で回す
- フレーム生成は PeriodicFrameGenerationAlgorithm(20ms 蓄積, 20 fps)
- JPEG は cv2.imencode(".jpg", quality=75)

いずれも recorder/camera.py の値に合わせてある。片方だけ変えると比較にならない。

ライブ計測ではなくオフラインにしているのは、EventsIterator がカメラ待ちの間も
ビジーで回り続け、CPU 時間がビジーウェイトに埋もれて測れないため（README 参照）。

使い方:
    python scripts/offline_bench.py out/rec/<id>/events.raw
    python scripts/offline_bench.py events.raw --json result.json
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

# 蓄積時間と表示 fps は recorder/camera.py の既定値と揃える
ACCUMULATION_US = 20000
PREVIEW_FPS = 20.0
JPEG_QUALITY = 75

# EventsIterator の既定値も recorder/camera.py と同じ
DELTA_T_US = 20000
N_EVENTS = 200_000


def _iterator(raw_path: Path):
    from metavision_core.event_io import EventsIterator

    return EventsIterator(input_path=str(raw_path), mode="mixed",
                          delta_t=DELTA_T_US, n_events=N_EVENTS)


def bench(raw_path: Path, frames: bool, jpeg: bool) -> dict:
    """RAW を 1 回通し、CPU 時間と処理量を返す。

    frames=False なら EVT3 デコードのみ。frames=True でフレーム生成を足し、
    さらに jpeg=True で JPEG エンコードまで足す。3 段の差分が各処理のコスト。
    """
    import cv2
    import numpy as np

    it = _iterator(raw_path)
    height, width = it.get_size()

    frame_gen = None
    n_frames = 0
    if frames:
        import metavision_sdk_core as msc

        def on_frame(ts: int, frame: np.ndarray) -> None:
            nonlocal n_frames
            n_frames += 1
            if jpeg:
                cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])

        frame_gen = msc.PeriodicFrameGenerationAlgorithm(
            width, height, ACCUMULATION_US, PREVIEW_FPS)
        frame_gen.set_output_callback(on_frame)

    n_events = 0
    # process_time はプロセス全体（全スレッド）の user+sys CPU 時間。
    # コールバックが別スレッドで回っても取りこぼさない。
    cpu0, wall0 = time.process_time(), time.perf_counter()
    for events in it:
        n = len(events)
        if not n:
            continue
        n_events += n
        if frame_gen is not None:
            frame_gen.process_events(events)
    cpu = time.process_time() - cpu0
    wall = time.perf_counter() - wall0

    size_mb = raw_path.stat().st_size / 1e6
    return {
        "events": n_events,
        "frames": n_frames,
        "cpu_s": round(cpu, 3),
        "wall_s": round(wall, 3),
        "mev_per_cpu_s": round(n_events / 1e6 / cpu, 1) if cpu > 0 else None,
        "mb_per_cpu_s": round(size_mb / cpu, 1) if cpu > 0 else None,
    }


STAGES = [
    ("デコードのみ", dict(frames=False, jpeg=False)),
    ("＋ フレーム生成", dict(frames=True, jpeg=False)),
    ("＋ フレーム生成 ＋ JPEG", dict(frames=True, jpeg=True)),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("raw", type=Path, help="録画済みの RAW ファイル")
    ap.add_argument("--repeat", type=int, default=1,
                    help="各段の試行回数。最良値（最短 CPU 時間）を採る")
    ap.add_argument("--json", type=Path, help="結果を JSON でも書き出す")
    args = ap.parse_args()

    if not args.raw.exists():
        print(f"ERROR: ファイルがありません: {args.raw}", file=sys.stderr)
        return 1

    size_mb = args.raw.stat().st_size / 1e6
    print(f"file    : {args.raw} ({size_mb:.1f} MB)")
    print(f"machine : {platform.machine()} / {platform.node()}")
    print()

    results = {}
    for label, kw in STAGES:
        best = None
        for _ in range(args.repeat):
            r = bench(args.raw, **kw)
            if best is None or r["cpu_s"] < best["cpu_s"]:
                best = r
        results[label] = best
        print(f"{label:<26} {best['mev_per_cpu_s']:>7} Mev/CPU秒  "
              f"{best['mb_per_cpu_s']:>7} MB/CPU秒  "
              f"(CPU {best['cpu_s']:.2f}s / wall {best['wall_s']:.2f}s, "
              f"{best['events']:,} ev, {best['frames']} frames)")

    if args.json:
        args.json.write_text(json.dumps({
            "file": str(args.raw),
            "size_mb": round(size_mb, 3),
            "machine": platform.machine(),
            "node": platform.node(),
            "platform": platform.platform(),
            "stages": results,
        }, ensure_ascii=False, indent=2))
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
