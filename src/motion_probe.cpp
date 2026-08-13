// 最速の動き検知 + 自己レイテンシ計測.
//
// ---------------------------------------------------------------------------
// 設計方針
// ---------------------------------------------------------------------------
// 検知レイテンシの内訳:
//
//   [動きが起きる]
//      ↓ ① 閾値を越えるだけの輝度変化が溜まるまで   ← バイアス次第。ここが一番大きい
//   [画素が発火]
//      ↓ ② 200 us（センサ公称 Typical Latency）
//   [センサがタイムスタンプを打つ]
//      ↓ ③ 0〜4000 us（USB 配送グリッド待ち。実測済み・固定）
//   [コールバック到着]
//      ↓ ④ 判定処理                                  ← このプログラムが担当
//   [検知]
//
// ②③は動かせないので、このツールは ④ を最小化しつつ ① を掃引できるようにする。
//
// - フレーム化しない。cd() コールバック内でイベントを 1 個ずつ見て判定する。
// - 判定はバッチ末尾を待たず、条件を満たした最初のイベントで即確定する。
// - ホットパスで確保・ロック・システムコールをしない（--print 時の出力は除く）。
//
// 空間セルごとのスライディング窓は、正確なリングバッファではなく
// 「窓を超えたらリセットして数え直す」近似（tumbling window）を使う。
// O(1)・分岐 2 個で済み、レイテンシが予測可能になる。
// 代償として、窓の境界を跨いだイベントの集中を取りこぼすことがある。
// ---------------------------------------------------------------------------

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <string>
#include <thread>
#include <vector>

#include <metavision/hal/facilities/i_ll_biases.h>
#include <metavision/hal/facilities/i_roi.h>
#include <metavision/sdk/base/events/event_cd.h>
#include <metavision/sdk/stream/camera.h>

namespace {

int64_t now_us() {
    return std::chrono::duration_cast<std::chrono::microseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

// win_start / dead_until を INT64_MIN で初期化してはいけない。
// センサ時刻 t は正の小さい値なので t - INT64_MIN が符号付きオーバーフローし、
// 窓のリセット判定 (t - win_start > window_us) が永久に false になる。
// その結果、各セルの最初の検知だけが「起動以来の全イベントを窓なしで数えた」
// ものになり、--count を上げるほど誤検知が増えるという逆転が起きた。
// count == 0 を「未初期化」の印として使い、引き算そのものを避ける。
constexpr int64_t kLongAgo = -1000000000000000LL; // 引いてもオーバーフローしない過去

struct Cell {
    int64_t win_start = 0;
    int64_t dead_until = kLongAgo;
    uint32_t count = 0;
};

struct Detection {
    int64_t t_event;   // センサ時刻
    int64_t h_cb;      // そのバッチのコールバック開始 host 時刻
    int64_t h_detect;  // 判定確定 host 時刻
    uint16_t cx, cy;   // セル座標
    uint16_t x, y;     // 発火画素
};

double percentile(const std::vector<double> &sorted, double p) {
    if (sorted.empty()) {
        return 0.0;
    }
    const auto i = static_cast<size_t>(p / 100.0 * static_cast<double>(sorted.size() - 1) + 0.5);
    return sorted[std::min(i, sorted.size() - 1)];
}

void print_dist(const char *label, const char *unit, std::vector<double> v) {
    if (v.empty()) {
        std::printf("  %-24s (データなし)\n", label);
        return;
    }
    std::sort(v.begin(), v.end());
    double sum = 0;
    for (double x : v) {
        sum += x;
    }
    std::printf("  %-24s mean %8.1f | p50 %8.1f | p90 %8.1f | p99 %8.1f | max %8.1f  %s\n", label,
                sum / static_cast<double>(v.size()), percentile(v, 50), percentile(v, 90), percentile(v, 99),
                percentile(v, 100), unit);
}

std::vector<double> causal_min(const std::vector<double> &h, const std::vector<double> &v, double W) {
    std::vector<double> out(v.size());
    std::deque<size_t> dq;
    for (size_t i = 0; i < v.size(); ++i) {
        while (!dq.empty() && v[dq.back()] >= v[i]) {
            dq.pop_back();
        }
        dq.push_back(i);
        while (h[dq.front()] < h[i] - W) {
            dq.pop_front();
        }
        out[i] = v[dq.front()];
    }
    return out;
}

std::vector<double> anticausal_min(const std::vector<double> &h, const std::vector<double> &v, double W) {
    std::vector<double> out(v.size());
    std::deque<size_t> dq;
    for (size_t k = v.size(); k > 0; --k) {
        const size_t i = k - 1;
        while (!dq.empty() && v[dq.back()] >= v[i]) {
            dq.pop_back();
        }
        dq.push_back(i);
        while (h[dq.front()] > h[i] + W) {
            dq.pop_front();
        }
        out[i] = v[dq.front()];
    }
    return out;
}

void usage(const char *argv0) {
    std::fprintf(stderr,
                 "usage: %s [options]\n"
                 "\n"
                 "検知パラメータ:\n"
                 "  --cell PX        セルの一辺 px (default 16)\n"
                 "  --window US      スライディング窓 us (default 1000)\n"
                 "  --count N        窓内で N イベントに達したら検知 (default 10)\n"
                 "  --deadtime US    検知後そのセルを無視する時間 us (default 50000)\n"
                 "  --nnfilter US    BA ノイズ除去。8 近傍に US 以内のイベントが\n"
                 "                   無いイベントを捨てる。0 で無効 (default 0)\n"
                 "\n"
                 "センサパラメータ:\n"
                 "  --bias NAME=VAL  バイアス設定。複数指定可\n"
                 "                   例: --bias bias_diff_on=340 --bias bias_fo=1700\n"
                 "  --roi X,Y,W,H    読み出し領域を制限\n"
                 "\n"
                 "実行:\n"
                 "  --seconds N      計測秒数 (default 10)\n"
                 "  --warmup MS      起動直後を捨てる時間 ms (default 500)\n"
                 "  --quiet          検知ごとの行を出さずサマリのみ\n"
                 "\n"
                 "静止シーンで走らせれば、検知は全て誤検知。誤検知率の測定に使える。\n",
                 argv0);
}

} // namespace

int main(int argc, char **argv) {
    int cell_px       = 16;
    int64_t window_us = 1000;
    uint32_t need     = 10;
    int64_t dead_us   = 50000;
    int32_t nn_us     = 0;
    double seconds    = 10.0;
    double warmup_ms  = 500.0;
    bool quiet        = false;
    std::vector<std::pair<std::string, int>> biases;
    int roi[4] = {-1, -1, -1, -1};

    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        if (a == "--cell" && i + 1 < argc) {
            cell_px = std::atoi(argv[++i]);
        } else if (a == "--window" && i + 1 < argc) {
            window_us = std::atoll(argv[++i]);
        } else if (a == "--count" && i + 1 < argc) {
            need = static_cast<uint32_t>(std::atoi(argv[++i]));
        } else if (a == "--deadtime" && i + 1 < argc) {
            dead_us = std::atoll(argv[++i]);
        } else if (a == "--nnfilter" && i + 1 < argc) {
            nn_us = static_cast<int32_t>(std::atol(argv[++i]));
        } else if (a == "--seconds" && i + 1 < argc) {
            seconds = std::atof(argv[++i]);
        } else if (a == "--warmup" && i + 1 < argc) {
            warmup_ms = std::atof(argv[++i]);
        } else if (a == "--quiet") {
            quiet = true;
        } else if (a == "--bias" && i + 1 < argc) {
            const std::string kv = argv[++i];
            const auto eq        = kv.find('=');
            if (eq == std::string::npos) {
                usage(argv[0]);
                return 2;
            }
            biases.emplace_back(kv.substr(0, eq), std::atoi(kv.c_str() + eq + 1));
        } else if (a == "--roi" && i + 1 < argc) {
            if (std::sscanf(argv[++i], "%d,%d,%d,%d", &roi[0], &roi[1], &roi[2], &roi[3]) != 4) {
                usage(argv[0]);
                return 2;
            }
        } else {
            usage(argv[0]);
            return 2;
        }
    }

    if (cell_px <= 0 || need == 0) {
        usage(argv[0]);
        return 2;
    }

    Metavision::Camera camera;
    try {
        camera = Metavision::Camera::from_first_available();
    } catch (const Metavision::CameraException &e) {
        std::fprintf(stderr, "カメラを開けませんでした: %s\n", e.what());
        return 1;
    }

    const auto &geom = camera.geometry();
    const int width  = geom.get_width();
    const int height = geom.get_height();
    const int nx     = (width + cell_px - 1) / cell_px;
    const int ny     = (height + cell_px - 1) / cell_px;

    if (!biases.empty()) {
        auto *ll = camera.get_device().get_facility<Metavision::I_LL_Biases>();
        if (ll == nullptr) {
            std::fprintf(stderr, "このカメラはバイアス設定に対応していません\n");
            return 1;
        }
        for (const auto &[name, value] : biases) {
            if (!ll->set(name, value)) {
                std::fprintf(stderr, "バイアス設定に失敗: %s=%d（レンジ外の可能性）\n", name.c_str(), value);
                return 1;
            }
        }
    }

    if (roi[2] > 0) {
        auto *r = camera.get_device().get_facility<Metavision::I_ROI>();
        if (r == nullptr) {
            std::fprintf(stderr, "このカメラは ROI に対応していません\n");
            return 1;
        }
        if (!r->set_window(Metavision::I_ROI::Window(roi[0], roi[1], roi[2], roi[3])) || !r->enable(true)) {
            std::fprintf(stderr, "ROI 設定に失敗\n");
            return 1;
        }
    }

    std::vector<Cell> cells(static_cast<size_t>(nx) * static_cast<size_t>(ny));

    // BA（background activity）ノイズ除去用の近傍相関フィルタ。
    // このセンサ（Gen3.1）には STC/trail filter が無いのでソフトで行う。
    // 実測では、静止シーンのノイズは時間的にも空間的にも散らばった孤立バーストで、
    // 照明フリッカでもホットピクセルでもなかった。本物の動きは輪郭に沿って
    // 空間的に相関したイベントを出すので、8 近傍に直近のイベントが無いものを捨てる。
    //
    // 1 画素あたり int64 のタイムスタンプ。640x480 で 2.4 MB。
    // int32 + INT32_MIN 初期化にすると t - INT32_MIN がオーバーフローし、
    // 一度も発火していない近傍が「相関あり」と誤判定されてフィルタが素通しになる。
    // 1 イベントあたり 8 回のルックアップで済むので判定コストはほぼ増えない。
    std::vector<int64_t> last_ts;
    if (nn_us > 0) {
        last_ts.assign(static_cast<size_t>(width) * static_cast<size_t>(height), kLongAgo);
    }
    uint64_t dropped_by_nn = 0;

    // コールバックスレッドが書き、main スレッドが読む。単一生産者・単一消費者。
    // vector::push_back と size() の並行アクセスは形式上 UB なので、
    // 領域を先に確保しておいて公開インデックスだけ atomic にする。
    constexpr size_t kDetCap = 1u << 20;
    std::vector<Detection> dets(kDetCap);
    std::atomic<size_t> n_det{0};

    // warmup を過ぎるまで検知を記録しない。起動直後は FX3 の溜まりが降ってくるため。
    // main スレッドが camera.start() 後に書き、コールバックスレッドが読むので atomic。
    std::atomic<int64_t> record_from{INT64_MAX};
    std::atomic<uint64_t> total_events{0};

    camera.cd().add_callback([&](const Metavision::EventCD *begin, const Metavision::EventCD *end) {
        const int64_t h_cb = now_us();
        total_events.fetch_add(static_cast<uint64_t>(end - begin), std::memory_order_relaxed);

        for (const auto *e = begin; e != end; ++e) {
            if (nn_us > 0) {
                const int64_t te = e->t;
                const size_t idx = static_cast<size_t>(e->y) * static_cast<size_t>(width) + e->x;
                bool correlated   = false;
                const int x0 = std::max(0, e->x - 1), x1 = std::min(width - 1, e->x + 1);
                const int y0 = std::max(0, e->y - 1), y1 = std::min(height - 1, e->y + 1);
                for (int yy = y0; yy <= y1 && !correlated; ++yy) {
                    const int64_t *row = &last_ts[static_cast<size_t>(yy) * static_cast<size_t>(width)];
                    for (int xx = x0; xx <= x1; ++xx) {
                        if (xx == e->x && yy == e->y) {
                            continue; // 自分自身は相関の根拠にしない
                        }
                        if (te - row[xx] <= nn_us) {
                            correlated = true;
                            break;
                        }
                    }
                }
                last_ts[idx] = te; // 相関の有無に関わらず自分の時刻は残す
                if (!correlated) {
                    ++dropped_by_nn;
                    continue;
                }
            }

            const int ci  = e->x / cell_px;
            const int cj  = e->y / cell_px;
            Cell &c       = cells[static_cast<size_t>(cj) * static_cast<size_t>(nx) + static_cast<size_t>(ci)];
            const int64_t t = e->t;

            if (t < c.dead_until) {
                continue;
            }
            if (c.count == 0 || t - c.win_start > window_us) {
                c.win_start = t;
                c.count     = 0;
            }
            if (++c.count < need) {
                continue;
            }

            // 判定確定。バッチ末尾を待たない。
            c.dead_until = t + dead_us;
            c.count      = 0;
            c.win_start  = t;

            if (h_cb >= record_from.load(std::memory_order_relaxed)) {
                const size_t i = n_det.load(std::memory_order_relaxed);
                if (i < kDetCap) {
                    dets[i] = Detection{t,  h_cb, now_us(), static_cast<uint16_t>(ci), static_cast<uint16_t>(cj),
                                        e->x, e->y};
                    n_det.store(i + 1, std::memory_order_release); // 要素の書き込みを先に公開する
                }
            }
        }
    });

    std::printf("sensor %dx%d | cell %dpx (%dx%d cells) | window %ld us | count %u | deadtime %ld us\n", width,
                height, cell_px, nx, ny, static_cast<long>(window_us), need, static_cast<long>(dead_us));
    if (!biases.empty()) {
        std::printf("biases:");
        for (const auto &[name, value] : biases) {
            std::printf(" %s=%d", name.c_str(), value);
        }
        std::printf("\n");
    }
    if (roi[2] > 0) {
        std::printf("roi: %d,%d,%d,%d\n", roi[0], roi[1], roi[2], roi[3]);
    }
    std::fflush(stdout);

    camera.start();
    const int64_t t_start   = now_us();
    const int64_t rec_start = t_start + static_cast<int64_t>(warmup_ms * 1000.0);
    record_from.store(rec_start, std::memory_order_relaxed);
    const int64_t t_end = rec_start + static_cast<int64_t>(seconds * 1e6);

    // 検知行の出力はコールバックの外で行う。ホットパスに write を入れない。
    size_t printed = 0;
    while (camera.is_running() && now_us() < t_end) {
        const size_t have = n_det.load(std::memory_order_acquire);
        if (!quiet) {
            for (; printed < have; ++printed) {
                const Detection &d = dets[printed];
                std::printf("DETECT t_host=%8.3f s  t_sensor=%10ld us  cell=(%3u,%3u)  px=(%3u,%3u)  compute=%4ld us\n",
                            static_cast<double>(d.h_detect - t_start) / 1e6, static_cast<long>(d.t_event), d.cx,
                            d.cy, d.x, d.y, static_cast<long>(d.h_detect - d.h_cb));
            }
            std::fflush(stdout);
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(2));
    }
    camera.stop();

    const size_t n_final = n_det.load(std::memory_order_acquire);
    dets.resize(n_final);
    const double dur = (now_us() - rec_start) / 1e6;

    std::printf("\n=== summary ===\n");
    std::printf("  duration          %.2f s\n", dur);
    std::printf("  events            %llu  (%.3f Mev/s)\n", static_cast<unsigned long long>(total_events.load()),
                static_cast<double>(total_events.load()) / dur / 1e6);
    std::printf("  detections        %zu  (%.2f /s)\n", dets.size(), dets.size() / dur);
    if (nn_us > 0) {
        const uint64_t tot = total_events.load();
        std::printf("  nnfilter          %d us, dropped %llu / %llu  (%.1f%%)\n", nn_us,
                    static_cast<unsigned long long>(dropped_by_nn), static_cast<unsigned long long>(tot),
                    tot ? 100.0 * static_cast<double>(dropped_by_nn) / static_cast<double>(tot) : 0.0);
    }
    std::printf("\n  静止シーンで走らせた場合、上の detections は全て誤検知。\n");

    if (dets.size() < 2) {
        std::printf("\n");
        return 0;
    }

    // ④ 判定処理そのものの時間。host 時刻の差なので絶対値として意味がある。
    std::vector<double> compute;
    compute.reserve(dets.size());
    for (const auto &d : dets) {
        compute.push_back(static_cast<double>(d.h_detect - d.h_cb));
    }

    // ②+③ 発火から検知までの遅れ。センサ時刻と host 時刻の未知オフセットは
    // 前後 1 秒の移動最小値で除去する（latency_probe と同じ手法）。
    const int64_t h0 = dets.front().h_detect;
    const int64_t s0 = dets.front().t_event;
    std::vector<double> h(dets.size()), lag(dets.size());
    for (size_t i = 0; i < dets.size(); ++i) {
        h[i]   = static_cast<double>(dets[i].h_detect - h0);
        lag[i] = h[i] - static_cast<double>(dets[i].t_event - s0);
    }
    const auto fwd = causal_min(h, lag, 1e6);
    const auto bwd = anticausal_min(h, lag, 1e6);
    for (size_t i = 0; i < lag.size(); ++i) {
        lag[i] -= std::min(fwd[i], bwd[i]);
    }

    std::printf("\n  ④ 判定処理時間（コールバック開始 → 検知確定。絶対値）\n");
    print_dist("compute", "us", compute);
    std::printf("\n  ②+③ 発火 → 検知（ベストケースからの上乗せ分。絶対値ではない）\n");
    print_dist("event -> detect lag", "us", lag);
    std::printf("\n  絶対検知レイテンシ ≒ 200us (センサ) + 上記 lag + ①（バイアス次第）\n\n");

    return 0;
}
