// 動き検知の可視化ビューア.
//
// motion_probe と同じ検知ロジックを走らせ、検知したセルをイベント画像に重ねて出す。
// 「本当に動きがあるのか」「どのパラメータでどこが光るのか」を目で確かめるための道具。
//
// ---------------------------------------------------------------------------
// これはレイテンシの基準器ではない
// ---------------------------------------------------------------------------
// 表示のためにフレーム生成をコールバック内で回すので、その分だけコールバックが重くなる。
// 判定処理そのものの時間は HUD に出しているが、数 ms を詰める話をするときは
// 描画を持たない motion_probe の数字を使うこと。
//
// 検知は cd() コールバック内で即時に確定する（表示の 30 fps とは無関係）。
// 描画は「検知したという事実」を人間が見える時間だけ保持して見せているだけ。
// ---------------------------------------------------------------------------

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <opencv2/imgproc.hpp>

#include <metavision/hal/facilities/i_ll_biases.h>
#include <metavision/hal/facilities/i_roi.h>
#include <metavision/sdk/base/events/event_cd.h>
#include <metavision/sdk/core/algorithms/periodic_frame_generation_algorithm.h>
#include <metavision/sdk/stream/camera.h>
#include <metavision/sdk/ui/utils/event_loop.h>
#include <metavision/sdk/ui/utils/window.h>

namespace {

constexpr int64_t kLongAgo = -1000000000000000LL;

int64_t now_us() {
    return std::chrono::duration_cast<std::chrono::microseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

struct Cell {
    int64_t win_start = 0;
    int64_t dead_until = kLongAgo;
    uint32_t count = 0;
};

// イベント画像はグレー基調なので、重ねる色は彩度のあるものを選ぶ。
const cv::Scalar kHit(52, 104, 235);    // BGR — 検知直後
const cv::Scalar kFade(120, 120, 120);  // 古い検知
const cv::Scalar kInk(255, 255, 255);
const cv::Scalar kShadow(0, 0, 0);

void usage(const char *argv0) {
    std::fprintf(stderr,
                 "usage: %s [options]\n"
                 "\n"
                 "  --cell PX        セルの一辺 px (default 16)\n"
                 "  --window US      スライディング窓 us (default 1000)\n"
                 "  --count N        窓内で N イベントに達したら検知 (default 10)\n"
                 "  --deadtime US    検知後そのセルを無視する時間 us (default 50000)\n"
                 "  --nnfilter US    8 近傍相関フィルタ。0 で無効 (default 0)\n"
                 "  --hold MS        検知表示を保持する時間 ms (default 200)\n"
                 "  --accum US       表示用の蓄積時間 us (default 10000)\n"
                 "  --fps N          表示 fps (default 30)\n"
                 "  --bias NAME=VAL  バイアス設定。複数指定可\n"
                 "\n"
                 "  ESC / q で終了\n",
                 argv0);
}

} // namespace

int main(int argc, char **argv) {
    int cell_px       = 16;
    int64_t window_us = 1000;
    uint32_t need     = 10;
    int64_t dead_us   = 50000;
    int64_t nn_us     = 0;
    int64_t hold_us   = 200000;
    uint32_t accum_us = 10000;
    double fps        = 30.0;
    std::vector<std::pair<std::string, int>> biases;

    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        auto next = [&]() { return argv[++i]; };
        if (a == "--cell" && i + 1 < argc) {
            cell_px = std::atoi(next());
        } else if (a == "--window" && i + 1 < argc) {
            window_us = std::atoll(next());
        } else if (a == "--count" && i + 1 < argc) {
            need = static_cast<uint32_t>(std::atoi(next()));
        } else if (a == "--deadtime" && i + 1 < argc) {
            dead_us = std::atoll(next());
        } else if (a == "--nnfilter" && i + 1 < argc) {
            nn_us = std::atoll(next());
        } else if (a == "--hold" && i + 1 < argc) {
            hold_us = std::atoll(next()) * 1000;
        } else if (a == "--accum" && i + 1 < argc) {
            accum_us = static_cast<uint32_t>(std::atoll(next()));
        } else if (a == "--fps" && i + 1 < argc) {
            fps = std::atof(next());
        } else if (a == "--bias" && i + 1 < argc) {
            const std::string kv = next();
            const auto eq        = kv.find('=');
            if (eq == std::string::npos) {
                usage(argv[0]);
                return 2;
            }
            biases.emplace_back(kv.substr(0, eq), std::atoi(kv.c_str() + eq + 1));
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
    const int width = geom.get_width(), height = geom.get_height();
    const int nx = (width + cell_px - 1) / cell_px, ny = (height + cell_px - 1) / cell_px;

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

    std::vector<Cell> cells(static_cast<size_t>(nx) * static_cast<size_t>(ny));
    std::vector<int64_t> last_ts;
    if (nn_us > 0) {
        last_ts.assign(static_cast<size_t>(width) * static_cast<size_t>(height), kLongAgo);
    }

    // 検知スレッドが書き、描画スレッドが読む。セルごとの最終検知時刻（センサ時刻）。
    auto cell_hit = std::make_unique<std::atomic<int64_t>[]>(cells.size());
    for (size_t i = 0; i < cells.size(); ++i) {
        cell_hit[i].store(kLongAgo, std::memory_order_relaxed);
    }

    std::atomic<uint64_t> n_events{0}, n_dets{0};
    std::atomic<int64_t> last_sensor_ts{0};
    std::atomic<int64_t> detect_us_total{0};
    std::atomic<uint64_t> detect_calls{0};

    Metavision::PeriodicFrameGenerationAlgorithm frame_gen(width, height, accum_us, fps);
    std::mutex frame_mu;
    cv::Mat shared_frame;
    bool have_frame = false;

    frame_gen.set_output_callback([&](Metavision::timestamp, cv::Mat &f) {
        std::lock_guard<std::mutex> lk(frame_mu);
        f.copyTo(shared_frame);
        have_frame = true;
    });

    camera.cd().add_callback([&](const Metavision::EventCD *begin, const Metavision::EventCD *end) {
        if (begin == end) {
            return;
        }
        // 検知を先にやる。描画のためのフレーム生成は後回しにして、判定を遅らせない。
        const int64_t t_enter = now_us();
        n_events.fetch_add(static_cast<uint64_t>(end - begin), std::memory_order_relaxed);

        for (const auto *e = begin; e != end; ++e) {
            const int64_t t = e->t;

            if (nn_us > 0) {
                const size_t idx = static_cast<size_t>(e->y) * static_cast<size_t>(width) + e->x;
                bool corr = false;
                const int x0 = std::max(0, e->x - 1), x1 = std::min(width - 1, e->x + 1);
                const int y0 = std::max(0, e->y - 1), y1 = std::min(height - 1, e->y + 1);
                for (int yy = y0; yy <= y1 && !corr; ++yy) {
                    const int64_t *row = &last_ts[static_cast<size_t>(yy) * static_cast<size_t>(width)];
                    for (int xx = x0; xx <= x1; ++xx) {
                        if ((xx != e->x || yy != e->y) && t - row[xx] <= nn_us) {
                            corr = true;
                            break;
                        }
                    }
                }
                last_ts[idx] = t;
                if (!corr) {
                    continue;
                }
            }

            const int ci = e->x / cell_px, cj = e->y / cell_px;
            const size_t k = static_cast<size_t>(cj) * static_cast<size_t>(nx) + static_cast<size_t>(ci);
            Cell &c = cells[k];

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
            c.dead_until = t + dead_us;
            c.count      = 0;
            c.win_start  = t;
            cell_hit[k].store(t, std::memory_order_relaxed);
            n_dets.fetch_add(1, std::memory_order_relaxed);
        }

        detect_us_total.fetch_add(now_us() - t_enter, std::memory_order_relaxed);
        detect_calls.fetch_add(1, std::memory_order_relaxed);
        last_sensor_ts.store((end - 1)->t, std::memory_order_relaxed);

        frame_gen.process_events(begin, end);
    });

    Metavision::Window window("EvCam motion", width, height, Metavision::Window::RenderMode::BGR);
    window.set_keyboard_callback(
        [&window](Metavision::UIKeyEvent key, int, Metavision::UIAction action, int) {
            if (action == Metavision::UIAction::RELEASE &&
                (key == Metavision::UIKeyEvent::KEY_ESCAPE || key == Metavision::UIKeyEvent::KEY_Q)) {
                window.set_close_flag();
            }
        });

    camera.start();

    cv::Mat canvas(height, width, CV_8UC3, cv::Scalar(30, 30, 30));
    int64_t t_rate = now_us();
    uint64_t ev_prev = 0, det_prev = 0;
    double ev_rate = 0, det_rate = 0, det_us = 0;

    // cv::putText は Hershey フォントで ASCII しか描けない。非 ASCII を渡すと
    // 1 文字ごとに '?' になる。HUD に出す文字列は ASCII に限ること。
    auto put = [&](const std::string &s, int x, int y, double scale, const cv::Scalar &col) {
        cv::putText(canvas, s, {x + 1, y + 1}, cv::FONT_HERSHEY_SIMPLEX, scale, kShadow, 2, cv::LINE_AA);
        cv::putText(canvas, s, {x, y}, cv::FONT_HERSHEY_SIMPLEX, scale, col, 1, cv::LINE_AA);
    };

    while (!window.should_close() && camera.is_running()) {
        Metavision::EventLoop::poll_and_dispatch(2);

        {
            std::lock_guard<std::mutex> lk(frame_mu);
            if (have_frame) {
                shared_frame.copyTo(canvas);
            }
        }

        const int64_t ts = last_sensor_ts.load(std::memory_order_relaxed);

        // 検知したセルを重ねる。新しいものほど濃く、hold 時間で消える。
        int active = 0;
        for (int j = 0; j < ny; ++j) {
            for (int i = 0; i < nx; ++i) {
                const int64_t hit = cell_hit[static_cast<size_t>(j) * nx + i].load(std::memory_order_relaxed);
                const int64_t age = ts - hit;
                if (hit == kLongAgo || age < 0 || age > hold_us) {
                    continue;
                }
                ++active;
                const double f = 1.0 - static_cast<double>(age) / static_cast<double>(hold_us);
                const cv::Scalar col = kHit * f + kFade * (1.0 - f);
                cv::rectangle(canvas, {i * cell_px, j * cell_px, cell_px, cell_px}, col, f > 0.6 ? 2 : 1);
            }
        }

        const int64_t now = now_us();
        if (now - t_rate >= 500000) {
            const double dt = (now - t_rate) / 1e6;
            const uint64_t ev = n_events.load(), dt_n = n_dets.load();
            ev_rate  = static_cast<double>(ev - ev_prev) / dt;
            det_rate = static_cast<double>(dt_n - det_prev) / dt;
            const uint64_t calls = detect_calls.exchange(0);
            det_us = calls ? static_cast<double>(detect_us_total.exchange(0)) / static_cast<double>(calls) : det_us;
            if (!calls) {
                detect_us_total.store(0);
            }
            ev_prev = ev, det_prev = dt_n, t_rate = now;
        }

        // 検知中は枠を光らせる。動きがあるかを一目で分かるようにする。
        if (active > 0) {
            cv::rectangle(canvas, {0, 0, width - 1, height - 1}, kHit, 3);
        }

        char buf[256];
        std::snprintf(buf, sizeof(buf), "%.3f Mev/s   %.1f det/s   active %d", ev_rate / 1e6, det_rate, active);
        put(buf, 8, 20, 0.5, kInk);
        std::snprintf(buf, sizeof(buf), "count %u  window %ld us  cell %d px  nnfilter %ld us", need,
                      static_cast<long>(window_us), cell_px, static_cast<long>(nn_us));
        put(buf, 8, 40, 0.42, cv::Scalar(190, 190, 190));
        std::snprintf(buf, sizeof(buf), "detect %.1f us/callback  (detection only, excludes rendering)", det_us);
        put(buf, 8, height - 12, 0.42, cv::Scalar(190, 190, 190));

        window.show(canvas); // Window は show_async を持たない（それは MTWindow）。show() が poll と描画をまとめて行う
    }

    camera.stop();
    return 0;
}
