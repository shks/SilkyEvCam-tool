#!/bin/bash
# 静止シーンでの誤検知率を掃引して CSV を出す。
#
#   source env.sh && ./scripts/fp_sweep.sh [1回あたりの秒数] > out/fp_sweep.csv
#
# 前提: カメラを一様な無地の面（白壁など）に向け、画角内で何も動かさないこと。
# 事前に同一設定を数回回してイベントレートが揃うことを必ず確認する。
# シーンが揺れていると掃引結果は解釈できない（実際に一度それで失敗した）。
#
# 静止シーンなので検知は全て誤検知。
#
# なお、デフォルト設定（count 10 / window 1000 / cell 16）では誤検知は 0/s になる。
# 応答を見るには、以下のようにかなり緩い設定まで振る必要がある。
set -euo pipefail

SECS="${1:-4}"
EVCAM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROBE="${EVCAM_ROOT}/build/motion_probe"

run() {
    # $1=グループ名 $2=x軸の値 残り=motion_probe への引数
    local group="$1" xval="$2"
    shift 2
    local out ev fp dr
    out=$("$PROBE" --seconds "$SECS" --quiet "$@" 2>/dev/null) || { echo "run failed: $*" >&2; return; }
    ev=$(grep -oP 'events\s+\d+\s+\(\K[0-9.]+' <<<"$out")
    fp=$(grep -oP 'detections\s+\d+\s+\(\K[0-9.]+' <<<"$out")
    dr=$(grep -oP 'nnfilter.*\(\K[0-9.]+' <<<"$out" || true)
    echo "${group},${xval},${ev:-},${fp:-},${dr:-0}"
}

echo "group,x,mev_per_s,false_per_s,nn_drop_pct"

# A) 検知閾値 x 時間窓。誤検知が立ち上がる膝を見る
for w in 1000 10000 100000; do
    for c in 2 3 4 5 6 8 10; do
        run "window${w}" "$c" --count "$c" --window "$w" --cell 16
    done
done

# B) 近傍相関フィルタ。誤検知が出る設定でないと効果が見えない
for nn in 0 100 200 500 1000 5000; do
    run "nnfilter" "$nn" --count 2 --window 10000 --cell 16 --nnfilter "$nn"
done

# C) コントラスト閾値。374 が HAL の許す下限
for b in 374 384 420 460 500 600; do
    run "bias_diff_on" "$b" --count 3 --window 10000 --cell 16 --bias "bias_diff_on=${b}"
done

# D) フォロワ帯域。上げると応答が速くなる代わりにノイズが増える
for b in 1250 1400 1477 1600 1700 1800; do
    run "bias_fo" "$b" --count 3 --window 10000 --cell 16 --bias "bias_fo=${b}"
done

# E) フォトレセプタ帯域。デフォルト 1250 が下限 975 に近い
for b in 975 1100 1250 1400 1600 1800; do
    run "bias_pr" "$b" --count 3 --window 10000 --cell 16 --bias "bias_pr=${b}"
done
