#!/usr/bin/env python3
"""SilkyEvCam の疎通確認.

リポジトリ直下で `source ./env.sh` した状態で実行する:

    python scripts/smoke_test.py            # ライブカメラから 3 秒キャプチャ
    python scripts/smoke_test.py --seconds 5
    python scripts/smoke_test.py --input rec.raw   # 記録ファイルから読む

イベント総数が 0 でなければ、HAL プラグイン → USB → Python バインディングまで
一通り繋がっている。
"""

import argparse
import sys

from metavision_core.event_io import EventsIterator


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", default="", help="RAW/HDF5 ファイル。省略時はライブカメラ"
    )
    parser.add_argument("--seconds", type=float, default=3.0, help="キャプチャ秒数")
    args = parser.parse_args()

    max_duration_us = int(args.seconds * 1_000_000)
    # mode="mixed" は「n_events 到達」か「delta_t 経過」の早い方で切る。
    # delta_t はレイテンシの下限になるので、疎通確認でも大きくしない
    # （ここは低レイテンシ計測用の経路ではない。数 ms を狙う実装は C++ の
    #  camera.cd().add_callback() を使うこと）。
    iterator = EventsIterator(
        input_path=args.input,
        mode="mixed",
        delta_t=1_000,
        n_events=10_000,
        max_duration=max_duration_us,
    )

    height, width = iterator.get_size()
    print(f"sensor: {width} x {height}")

    total = 0
    positive = 0
    for events in iterator:
        total += len(events)
        if len(events):
            positive += int((events["p"] == 1).sum())

    print(f"duration: {args.seconds} s")
    print(f"events:   {total:,}")
    if total == 0:
        print("FAIL: イベントが 1 件も取れていない", file=sys.stderr)
        return 1

    print(f"  ON  (p=1): {positive:,}")
    print(f"  OFF (p=0): {total - positive:,}")
    print(f"  rate:      {total / args.seconds / 1e6:.2f} Mev/s")
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
