# EvCam — SilkyEvCam + OpenEB セットアップ

CenturyArks **SilkyEvCam** を Ubuntu 24.04 で動かすための環境。
Prophesee の **OpenEB**（Metavision SDK の OSS 部分, Apache 2.0）をソースからビルドして使う。
有償の Metavision SDK は使わない。

## 使い方

```bash
source /home/maeda/projects/EvCam/env.sh

metavision_viewer                 # ライブ表示
python scripts/smoke_test.py      # 疎通確認（イベント数を出す）
```

`env.sh` を source したシェルでのみ有効。`~/.bashrc` は変更していない。

## 構成

```
SilkyEvCam (USB3, VID 31f7)
      ↓
libsilky_common_plugin.so     CenturyArks 製 HAL プラグイン。MV_HAL_PLUGIN_PATH で発見される
      ↓
libmetavision_hal.so.5        OpenEB。EVT3 デコード
      ↓
metavision_viewer / metavision_core (Python)
```

```
EvCam/
├── env.sh                    環境設定（これを source する）
├── openeb/                   OpenEB 5.2.0 ソース + build/（git 管理外）
├── vendor/
│   ├── SilkyEvCam_Plugin_Installer_for_ubuntu_v5.2.0.zip
│   ├── silkyevcam-plugin/    展開したもの（git 管理外）
│   └── silky-hal/            実際に使う配置
│       ├── plugins/libsilky_common_plugin.so
│       └── bin/silkyevcam_platform_info, silkyevcam_mask_pixel_util
├── scripts/smoke_test.py
└── .venv/                    Python 3.12 venv（git 管理外）
```

## バージョン固定

**OpenEB は 5.2.0 に固定。** プラグインの `readme.txt` に明記されている:

> This pack is for Metavision version 5.2.0. Cannot be used with version 5.1.1 or earlier

OpenEB を上げるときは、必ず https://centuryarks.com/download-2/ から対応するプラグインも上げること。

## セットアップ手順（実際に通したもの）

### 1. 依存パッケージ（要 root）

```bash
sudo apt-get update
sudo apt-get install -y cmake libboost-all-dev libusb-1.0-0-dev \
  libprotobuf-dev protobuf-compiler libhdf5-dev libglew-dev libglfw3-dev
```

`libopencv-dev` / `ffmpeg` / `build-essential` は導入済みだった。

### 2. Python venv

Ubuntu 24.04 は PEP 668 でシステム Python への pip を拒否するため venv に隔離。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install numpy opencv-python h5py scipy pytest
.venv/bin/python -m pip install "pybind11==2.13.6"
```

**pybind11 は 2.13.6 に固定。** OpenEB 5.2.0 は `find_package(pybind11 2.7)` で 2.x 系を前提にしており、
最新の 3.1.0 は API 非互換のリスクがある。

### 3. OpenEB のビルド

```bash
EVCAM_ROOT="$(pwd)"
git clone https://github.com/prophesee-ai/openeb.git --branch 5.2.0

cmake -S openeb -B openeb/build \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_TESTING=OFF \
  -DCOMPILE_PYTHON3_BINDINGS=ON \
  -DPython3_EXECUTABLE="${EVCAM_ROOT}/.venv/bin/python" \
  -Dpybind11_DIR="$("${EVCAM_ROOT}/.venv/bin/python" -c 'import pybind11; print(pybind11.get_cmake_dir())')" \
  -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc \
  -Wno-dev

cmake --build openeb/build -- -j$(nproc)
```

`install` はしない（ローカルビルドツリー方式）。

### 4. プラグイン配置

```bash
./scripts/fetch_plugin.sh
```

プラグインは CenturyArks の proprietary バイナリなのでリポジトリには含めていない
（`LICENSE_CA.txt`: "may be used only when using CenturyArks's products or services"）。
上記スクリプトが zip を取得して `vendor/silky-hal/` に配置する。

**同梱の `CA_Silky_installer.sh` は使わない。** あれは
`~/.bashrc` に `export MV_HAL_PLUGIN_PATH=...` を追記し、`/usr/lib/CenturyArks/` と `/usr/bin` に
system-wide でファイルを置くため、ローカルツリー方式と衝突する。
インストーラの残りの仕事は udev ルールだけなので、それは手動で入れる（次項）。

### 5. udev ルール（要 root）

```bash
sudo cp vendor/silkyevcam-plugin/.../resources/ca_device.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

中身:

```
KERNEL=="*", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", ACTION=="add", ATTR{idVendor}=="31f7", MODE="666", TAG="causb_dev"
```

SilkyEvCam は **VID `31f7`（CenturyArks）**。Prophesee の `88-cyusb.rules`（VID `04b4`）は
Prophesee EVK 用なので**入れていない**。

## ハマりどころ

### cmake 4.2.0 は問題なかった

この PC の `cmake` は `/usr/local/bin` に手動導入された 4.2.0。OpenEB が古い
`cmake_minimum_required` を使っていると configure が落ちる懸念があったが、実測では警告のみ:

```
CMake Deprecation Warning at CMakeLists.txt:20 (cmake_minimum_required):
  Compatibility with CMake < 3.10 will be removed from a future version of CMake.
```

apt の cmake 3.28.3 も入れてあるが、必須ではない。

### CUDA は PATH に無い

CUDA 13.0 は入っているが `/usr/local/cuda/bin` が PATH に無く、そのままだと
`Looking for a CUDA compiler - NOTFOUND` で黙って CPU のみのビルドになる。
`-DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc` を明示すること。

### setup_env.sh は MV_HAL_PLUGIN_PATH を上書きする

`openeb/build/utils/scripts/setup_env.sh` は `MV_HAL_PLUGIN_PATH` を
OpenEB 自身のプラグインパスで**上書き**する。Silky プラグインの追加は必ずこの後に
**追記**すること（`env.sh` はそうしている）。

## レイテンシについて

目標は end-to-end で数 ms 以内。支配要因は以下（OpenEB のソースを読んで確認）。

### ① アプリ層のスライシング

Python の `EventsIterator` は `delta_t` 分バッファしてから返すので、窓幅がそのまま
レイテンシの床になる（`events_iterator.py:54`、デフォルト 10 ms）。
**数 ms を狙う経路に Python は使わない。** C++ の
`camera.cd().add_callback()`（`sdk/modules/stream/cpp/include/metavision/sdk/stream/camera.h:277`）
ならデコード済みバッファが届いた時点で即コールバックされる。

### ② USB 転送層

`hal_psee_plugins/src/boards/utils/psee_libusb_data_transfer.cpp:34-36`:

```cpp
constexpr size_t USB_TIME_OUT                = 100;        // ms
constexpr size_t N_ASYNC_TRANFERS_PER_DEVICE = 20;
constexpr size_t PACKET_SIZE                 = 128 * 1024;
```

環境変数で上書きできる。CenturyArks プラグインは OpenEB 5.2.0 の `hal_psee_plugins` を
そのままビルドしたものなので（`strings` でビルドパス `openeb520Silky` を確認）、同じ変数が効く:

- `MV_PSEE_DEBUG_PLUGIN_USB_PACKET_SIZE`
- `MV_PSEE_DEBUG_PLUGIN_USB_ASYNC_TRANSFER`
- `MV_PSEE_DEBUG_PLUGIN_USB_TIME_OUT`

**誤解しやすい点が 2 つある。**

`:219` のコメント `// Note: unlike raw libusb, partial transfers are reported as completed` のとおり、
FX3 が短パケットで区切れば 128 KiB 未満でも即届く。**PACKET_SIZE は上限であって下限ではない。**
実際の遅延は FX3 ファーム（クローズド）の挙動次第なので、**実測が要る**。

`:208-216` の `LIBUSB_TRANSFER_TIMED_OUT` は「データがゼロだった」ケースで、バッファは
破棄されず再投入されるだけ。**`MV_PSEE_DEBUG_PLUGIN_USB_TIME_OUT` を下げてもレイテンシは下がらない。**

### ③ ERC はこのカメラには無い

当初「ERC（Event Rate Controller）でイベントレートに上限をかけて最悪レイテンシを
有界にする」と見込んでいたが、**Gen3.1 VGA には ERC が実装されていない**。
実機で facility を引くと `None` が返る:

```
get_i_erc_module                          -> None
get_i_event_trail_filter_module           -> None   (STC/Trail filter)
get_i_antiflicker_module                  -> None
get_i_digital_crop                        -> None
get_i_digital_event_mask                  -> None
```

これらは IMX636（Gen4.1 = SilkyEvCam HD）の機能。Gen3.1 で使えるのは
`I_LL_Biases` と `I_ROI` のみ。輻輳を抑えたいならバイアスで感度を下げるか
ROI でデータ量を削るしかない。

### ④ 無関係なもの

**CUDA はキャプチャレイテンシには効かない**（下流の推論用）。

## レイテンシ実測結果

計測ツール: `src/latency_probe.cpp` → `scripts/build_tools.sh` でビルド。

```bash
source env.sh
./scripts/build_tools.sh
./build/latency_probe --seconds 10
./scripts/latency_sweep.sh 8      # USB パラメータ掃引
```

### 結論: 約 4 ms の配送周期がハードな下限。host 側のチューニングでは下がらない

Gen3.1 VGA / デフォルト設定 / 静止シーン / warmup 500 ms 除外:

| 指標 | mean | p50 | p90 | p99 | max |
|---|---|---|---|---|---|
| lag (バッチ内で最も古いイベント) | 3.80 ms | 3.89 | 4.08 | 4.20 | **5.12 ms** |
| lag (バッチ内で最も新しいイベント) | 0.36 ms | 0.28 | 0.78 | 1.42 | 2.55 ms |
| コールバック間隔 | 4.000 ms | 3.999 | 4.07 | 4.19 | 5.85 ms |
| バッチのセンサ時間幅 | 3.44 ms | 3.55 | 3.87 | 3.96 | 3.99 ms |

コールバック間隔の中央値が **3.999 ms** と異常なほど安定している。これは FX3 ファームウェアが
内部タイマで約 4 ms ごとにバッファをコミットしているため。結果として個々のイベントの
上乗せ遅延は 0〜4 ms に一様分布し、平均約 2 ms、実測最悪 5.1 ms になる。

### USB パラメータ掃引: 効果なし

`scripts/latency_sweep.sh` の結果（`lag_p99`）:

| 設定 | rate | lag_p99 |
|---|---|---|
| packet 128 KiB (default) | 29 kev/s | 4225 us |
| packet 32 KiB | 73 kev/s | 4219 us |
| packet 8 KiB | 358 kev/s | 4189 us |
| packet 4 KiB | 3.5 kev/s | 4187 us |
| async 20 (default) | 515 kev/s | 4208 us |
| async 4 | 3.2 kev/s | 4166 us |
| async 2 | 3.4 kev/s | 4201 us |

**`lag_p99` は全条件で 4.17〜4.23 ms に張り付く。** イベントレートが 3.4 kev/s 〜 515 kev/s と
150 倍変わっても動かない。つまりホスト側のバッファリングではなくカメラ側の配送周期が支配的で、
`MV_PSEE_DEBUG_PLUGIN_USB_PACKET_SIZE` も `_ASYNC_TRANSFER` も**レイテンシには効かない**。

### 測定手法上の注意（2 つとも実際に踏んだ）

**1. ベースラインに全区間の最小値＋一次回帰を使ってはいけない。**
EVT3 の `NonMonotonicTimeHigh` が 1 回混ざるだけで回帰が壊れ、初回計測では
ドリフト **-5800 ppm**（非現実的）、`lag_p50` 32 ms という汚染された数値が出た。
前後 1 秒の**移動最小値**をベースラインにすると、ドリフトは 16〜20 ppm という妥当な値に収まり、
孤立したタイムスタンプ跳びにも影響されない。

**2. 起動直後の約 250 ms を捨てること。**
`camera.start()` 直後は FX3 側に溜まっていた分がまとめて降ってくるため、
`lag_max` が全条件で約 251 ms に張り付いていた。`--warmup 500`（デフォルト）で除外すると
**251,172 us → 5,123 us** になる。定常時の挙動を見たいならこれを含めてはいけない。

**3. `NonContinuousTimeHigh` は警告ではない。**
robust デコーダを使うと 10 秒で 1274 件出るが、これは疎なシーンで `time_high` が
2 段以上飛んだだけで正常。`NonMonotonicTimeHigh` とは区別して扱うこと
（`latency_probe` は種類別に数えている）。

### 測れていないもの

報告値は**ベストケースからの上乗せ分**であって絶対レイテンシではない。
センサ時刻と host 時刻は同期していないので、未知の定数オフセット
C（光子到達 → センサ読み出し → USB 転送 → デコードの最小遅延）が分離できない。

絶対値が必要なら外部刺激が要る:

- host が既知時刻に GPIO で LED を光らせ、その輝度変化イベントが返ってくるまでを測る
- または `I_TriggerIn` に既知時刻の信号を入れる（要配線）

### 数 ms 目標に対する評価

目標「数 ms 以内」に対し、**平均 約 2 ms / p99 約 4.2 ms**（+ 未測定の定数 C）。
ただし約 4 ms はカメラファームウェア由来のため、**ここから下げる手段はホスト側には無い**。
さらに詰めるなら CenturyArks にファームウェアのコミット周期を問い合わせる必要がある。

## trigger loopback による検証（配線不要）

`src/trigger_probe.cpp`。**Raspberry Pi も host GPIO も要らない。**

このカメラは `I_TriggerOut`（周期パルス生成）と `I_TriggerIn` を両方持ち、
TriggerIn には **LOOPBACK チャネル**がある（`get_available_channels()` →
`{Channel.MAIN: 0, Channel.LOOPBACK: 6}`）。つまりカメラが自分でパルスを出し、
自分でタイムスタンプを打てる。**単一クロックドメインなので同期問題が消える。**

```bash
./build/trigger_probe --seconds 10 --period 8600
```

### 結果: 配送グリッド 4.00 ms を独立に確認

エッジ間隔 4300 us（4 ms と非整数比なので位相が掃引される）で 10 秒:

| 指標 | mean | p50 | p90 | p99 | max |
|---|---|---|---|---|---|
| sensor edge interval | 4300.0 | 4300.0 | 4300.0 | 4300.0 | **4300.0** us |
| host arrival lag | **2045.9** | 2034.0 | 3633.0 | **4025.0** | 7656.0 us |

**`sensor edge interval` は 2328 エッジ全部が 4300.0 us**（max まで一致）。
センサ側のタイムベースにジッタが無く、完璧な基準信号として使える。

そのうえで `host arrival lag` は **[0, 4000 us] の一様分布そのもの**
（mean 2046 ≒ 4000/2、p50 2034、p99 4025）。エッジ間隔 3333 us でも同じ結果。

シーンにもイベントレートにも依存しない完全周期信号で測ってこうなるので、
**配送グリッドが 4.00 ms であることが独立に確定した**。
`latency_probe` のシーン依存な測定と完全に一致する。

つまり任意のイベントのグリッド待ち時間は **平均 2.0 ms / 最悪 4.0 ms**。

## 絶対レイテンシの測定（Step 2、要 LED 配線）

残る未知量は定数 C（光子到達 → センサタイムスタンプ）だけ。これは
**trigger_out の外部端子に LED を付けてセンサに向ければ測れる**:

```bash
./build/trigger_probe --optical --seconds 20
```

Δ = `t_cd(発光)` − `t_trigger(loopback)` を出す。**両方センサクロックなので
オフセット未知の問題が発生しない。**

### trigger_out の端子（仕様書で確定）

出典: [SilkyEvCam-USB_Spec_Rev102.pdf](https://www.centuryarks.com/images/product/sensor/silkyevcam/SilkyEvCam-USB_Spec_Rev102.pdf) P6 Table 4
（`vendor/docs/` にも保存。gitignore 対象）

USB Type-C とは別に **IX Series コネクタ（HIROSE **IX80G-B-10P**）** が背面にある。
嵌合プラグは `IX30G-B-10S-CV(7.0)` または `IX31G-B-10S-CV(7.0)`。
**標準付属品は USB3.0 Type-C ケーブルのみで、trigger ケーブルは付属しない。**

| Pin | Signal | Pin | Signal |
|---|---|---|---|
| 1 | **TRIGGER_OUT / SYNC_OUT_P (+3.3V)** | 6 | TRIG_IN_N — opto-coupled |
| 2 | **SYNC_OUT_N** | 7 | No use |
| 3 | SYNC_IN_P — opto-coupled | 8 | No use |
| 4 | SYNC_IN_N — opto-coupled | 9 | No use |
| 5 | TRIG_IN_P — opto-coupled | 10 | No use |

master 時は **pin 1 & 2** に出力が出る。用途は 2 択:

- `TRIGGER_OUT`: 周期・デューティがプログラム可能なパルス ← **`I_TriggerOut` で叩いているのはこれ**
- `EXT_SYNC_CLK_OUT`: 1 MHz 同期クロック

slave 時は pin 3 & 4 に 1 MHz クロック、pin 5 & 6 に `MAIN_TRIGGER_IN` を受ける。

**注意 1: TRIG_IN 側はフォトカプラ入力。** 他機材と同期させる用途では
フォトカプラの伝搬遅延（素子次第で 1〜10 us 以上）が乗る。
今回使っている **LOOPBACK チャネルは内部経路なのでフォトカプラを通らない**。

**注意 2: pin 1 は 3.3V の P/N ペア。** LED を直接ドライブできる電流は期待できないので
MOSFET かトランジスタでバッファする。P/N が LVDS なのか 3.3V CMOS + 相補出力なのかは
仕様書に明記が無いため、オシロで確認するのが確実。

必要なもの:

- HIROSE の嵌合プラグ `IX30G-B-10S-CV(7.0)` / `IX31G-B-10S-CV(7.0)`
  （手組しにくいので CenturyArks から完成ケーブルを買う方が現実的）
- MOSFET / トランジスタ + LED + 電流制限抵抗
- LED をセンサに向ける

### センサ自体のレイテンシは仕様書に載っている

同 PDF P5 Table 1 より、センサ（PPS3MVCD / PROPHESEE）の

> **Typical Latency 200us**
> Maximum readout throughput 50Mevents/s

つまり未知定数 C のうち支配項は **約 200 us** とメーカー公称値がある。
4.00 ms の配送グリッドに対して **5% 程度**にすぎない。

したがって絶対レイテンシの内訳はほぼこう見積もれる:

```
絶対レイテンシ ≒ 200 us (センサ)  +  0〜4000 us (配送グリッド待ち)  +  小 (USB 転送 + デコード)
             ≒ 平均 2.2 ms / 最悪 4.3 ms
```

**Step 2 の LED 測定は「200 us の検証」という位置づけに下がる。**
数 ms 目標の判断には既に十分な材料が揃っている。

### なぜ Raspberry Pi を使わないか

1. **プラグインが x86-64 専用。** `file` で確認済み
   （`ELF 64-bit LSB shared object, x86-64`）。zip に ARM/aarch64 バイナリは無い。
   CenturyArks が aarch64 版を出していない限り、**Pi ではカメラ自体が動かない**
2. **Pi を別マシンの刺激源にすると悪化する。** Pi と host のクロックを 4 ms より
   十分細かく同期する必要があるが、chrony/NTP は ms オーダー＝測りたい量と同じ桁。
   ハードウェアタイムスタンプ付き PTP なら足りるが、労力に見合わない
3. **そもそも不要。** カメラが trigger_out + LOOPBACK を持っているので単一
   クロックドメインで完結する

## 動き検知（`src/motion_probe.cpp`）

```bash
source env.sh
./build/motion_probe --seconds 10
./build/motion_probe --count 40 --window 300 --cell 16 --nnfilter 1000 --quiet
```

セル内のイベント数がスライディング窓で閾値を超えたら検知し、標準出力に出す。
検知レイテンシを自己計測する。

### 検知レイテンシの内訳

```
[動きが起きる]
   ↓ ① 閾値を越える輝度変化が溜まるまで   ← バイアス・照明次第。可変
[画素が発火]
   ↓ ② 200 us（センサ公称）
[タイムスタンプ]
   ↓ ③ 0〜4000 us（配送グリッド。実測済み・固定）
[コールバック]
   ↓ ④ 判定処理
[検知]
```

### 実測（シーンに依存しない部分）

| 指標 | mean | p50 | p90 | p99 | max |
|---|---|---|---|---|---|
| ④ compute | 1.9 | 1.0 | 4.0 | 8.0 | 26.0 us |
| ②+③ event → detect lag | 1980 | 1862 | 3586 | 4095 | 7280 us |

**④ は p50 1 us / p99 8 us で無視できる。** 判定ロジックはボトルネックではない。
②+③ は予想どおり [0, 4000 us] の一様分布で、配送グリッドが支配的。

つまり **絶対検知レイテンシ ≒ 200 us + 平均 2.0 ms（最悪 4.1 ms）+ ①**。

### 使えるパラメータ

センサ側（`I_LL_Biases`、実機のレンジ）:

| バイアス | 現在値 | レンジ | 効き |
|---|---|---|---|
| `bias_diff_on` | 384 | 0–1800 | ON 側のコントラスト閾値。下げると早く発火 |
| `bias_diff_off` | 221 | 0–1800 | OFF 側の閾値 |
| `bias_diff` | 299 | 200–400 | 上記の基準点 |
| `bias_fo` | 1477 | 1250–1800 | フォロワ帯域。上げると応答が速い |
| `bias_pr` | 1250 | 975–1800 | フォトレセプタ帯域。低照度でのレイテンシに直結 |
| `bias_refr` | 1500 | 1300–1800 | リフラクトリ |
| `bias_hpf` | 1448 | 900–1800 | ハイパスカットオフ |

**注意: 上表のレンジは静的な値で、実際にはもっと狭い。** `bias_diff_on=330` を
設定しようとすると HAL が拒否する:

```
[HAL][WARNING] Current bias_diff_on minimal value is 374
```

`bias_diff` との相対関係で下限が動く。強引に超えたい場合は device config の
`ll_biases_range_check_bypass`（`metavision_platform_info` に表示される）を使う。

そのほか: `I_ROI`（読み出し領域制限）、照明とレンズ絞り（F1.4）。
**照明はソフトのどのパラメータより効く** — 200 us は好条件での Typical 値で、
低照度ではフォトレセプタ応答が ms オーダーまで落ちる。

### ノイズ除去: 近傍相関フィルタ

このセンサには STC/trail filter が無いので、`--nnfilter US` として
ソフトで 8 近傍相関フィルタを実装した。8 近傍に US 以内のイベントが無ければ捨てる。
本物の動きは輪郭に沿って空間相関したイベントを出すので残る。
1 画素 int32（640x480 で 1.2 MB）、1 イベントあたり 8 ルックアップ。

ホットピクセルが問題になる場合は、CenturyArks の
`vendor/silky-hal/bin/silkyevcam_mask_pixel_util` でマスクできる。

### 踏んだバグ: 符号付きオーバーフローによる偽の誤検知

初版では `Cell::win_start` を `INT64_MIN` で初期化していた。センサ時刻 `t` は
正の小さい値なので `t - win_start` がオーバーフローし、窓のリセット判定
`(t - win_start > window_us)` が永久に false になる。結果として各セルの最初の検知だけが
「起動以来の全イベントを窓なしで数えた」ものになった。

症状は**一見 BA ノイズにそっくりだった**: 全画面に散らばり、同一セルの再発なし、
時間的に固まらない（各セルが 1 回ずつ発火するので当然そうなる）。
一度これを「典型的な BA ノイズ」と誤診している。

気づいたきっかけは掃引の途中結果で、**`--count` を上げるほど誤検知が増えていた**こと
（20 → 25.5/s、80 → 43.3/s）。閾値を厳しくして検知が増えるのはあり得ない。
`--count` が大きいほど偽検知の発生が遅れ、warmup 期間から計測期間へずれ込むためだった。

`--nnfilter` 側にも同型のバグがあった（`int32` + `INT32_MIN` 初期化で
`t - last_ts` がオーバーフローし、一度も発火していない近傍が「相関あり」と
誤判定されてフィルタが素通しになる）。

修正後、同じ静止シーンで誤検知は全条件 0.00/s になった。

### 誤検知率の掃引結果

```bash
./scripts/fp_sweep.sh 4 > out/fp_sweep.csv
python scripts/plot_fp_sweep.py out/fp_sweep.csv out/fp_sweep.html
```

静止した白壁に向け、同一設定 5 秒 x 6 回でイベントレートが 0.019〜0.020 Mev/s に
揃うことを確認したうえで計測（この確認をせずに掃引して一度失敗している）。

**既定設定（`--count 10 --window 1000 --cell 16`）では誤検知 0/s。**
以下は応答を見るために意図的に緩めた領域の値。

判定閾値（`--count`）と時間窓の関係 — 誤検知 /s:

| --count | window 1 ms | window 10 ms | window 100 ms |
|---|---|---|---|
| 2 | 1106 | 2582 | 2795 |
| 3 | 82.8 | 1474 | 2308 |
| 4 | 1.00 | 702 | 1877 |
| 5 | **0.00** | 231 | 1593 |
| 8 | 0.00 | 2.49 | 1041 |
| 10 | 0.00 | **0.00** | 763 |

窓が短いほど膝が鋭い。window 1 ms なら count 5 で 0 に落ちるが、
window 100 ms では count 10 でも 763/s 残る。

近傍相関フィルタ（`--count 2 --window 10000` 固定）:

| --nnfilter | 誤検知 /s | 除去率 |
|---|---|---|
| 0（無効） | 2355 | — |
| 100 us | **0.00** | 99.7% |
| 1000 us | 1.99 | 99.4% |
| 5000 us | 34.7 | 98.2% |

**除去率が 98〜99% あることに注意。** 静止シーンのノイズはほぼ空間相関を持たないので
当然そうなるが、本物の動きをどれだけ通すかは未検証（動きのある計測が必要）。

センサバイアス（`--count 3 --window 10000` 固定）:

| bias | 値 | Mev/s | 誤検知 /s |
|---|---|---|---|
| `bias_diff_on` | 374（下限） | 0.043 | 2460 |
| | 384（既定） | 0.022 | 1447 |
| | 460 | 0.013 | 630 |
| `bias_fo` | 1250（下限） | **52.4** | 21743 |
| | 1477（既定） | 0.023 | 1484 |
| | 1600 | 0.000 | 0.00 |
| `bias_pr` | 975（下限） | 0.011 | 617 |
| | 1250（既定） | 0.021 | 1388 |
| | 1600 | 3.22 | 10686 |

**`bias_fo` は既定値が崖の縁にある。** 1250 まで下げるとイベントレートが 52.4 Mev/s
（センサ仕様の最大読み出し 50 Mev/s 超＝飽和）に達し、逆に 1600 まで上げると
0.000 Mev/s でセンサが実質何も出さなくなる。可動域は極めて狭い。

`bias_pr` は上げるほどイベントが増える（1600 で 3.2 Mev/s）。
`bias_diff_on` は下げるほど感度が上がるが、HAL の下限 374 と既定 384 の差は 10 しかない。

**注意: これらは静止シーンでのノイズ応答であって、応答速度の利得ではない。**
「感度を上げると速くなる」側の効果は、動きのある被写体でないと測れない。

グラフ: `out/fp_sweep.html`（自己完結 HTML、`scripts/plot_fp_sweep.py` が生成）

## 可視化ビューア（`src/motion_viewer.cpp`）

```bash
source env.sh
./build/motion_viewer --count 5 --window 1000 --cell 16 --hold 250 --accum 20000
# ESC または q で終了
```

`motion_probe` と同じ検知ロジックを走らせ、**検知したセルをイベント画像に重ねて表示する。**
オレンジの四角が検知セル（新しいほど濃く、`--hold` の時間で消える）、
画面全体を囲む枠が「いまどこかで検知中」の表示。
HUD にイベントレート・検知レート・現在光っているセル数・判定処理時間を出す。

**これはレイテンシの基準器ではない。** 表示用のフレーム生成をコールバック内で回すぶん
コールバックが重くなる。数 ms を詰める議論では描画を持たない `motion_probe` を使うこと。
検知自体は `cd()` コールバック内で即時確定しており、表示の 30 fps とは無関係。

### これで解けた切り分け問題

静止シーンだけでは偽陽性しか測れず、人が写っているだけでは「本当に動いていたか」が
分からない。実際、被写体が座っているだけの状態ではイベントレートが白壁より低くなり
（0.004 Mev/s、90% のイベントが周辺部の 10% のセルに集中）、
検知が 0 でもフィルタのせいなのか動きが無かったせいなのか切り分けられなかった。
ビューアだと**見れば分かる**。

正解つきで数字が要る場合は `scripts/motion_gt_test.py`（合図を出して MOVE / STILL 区間を
比較する）。

### 動作点: 誤検知ゼロのまま真陽性 171/s

`--count 5 --window 1000 --cell 16` での実測:

| シーン | 検知 /s |
|---|---|
| 白壁（静止） | **0.00** |
| 手を振る | **171.3** |

判定処理は **3.4 us/callback**。周辺部に散るノイズ点は、この閾値では四角を一つも出さない。
誤検知カーブの膝（`--count` 2〜4 のあたり）より十分安全側に実用的な動作点がある。

### cv::putText は ASCII のみ

HUD に日本語を渡すと 1 文字ごとに `?` になる（Hershey フォントには字形が無い）。
`motion_viewer` の HUD 文字列は ASCII に限ること。

## 未了

- [ ] `--nnfilter` が本物の動きを通すかの確認（除去率 98〜99% あるため要検証）
- [ ] Step 2: LED 配線して光→イベントの絶対遅延を測定
- [ ] 高イベントレート下での輻輳時挙動と ERC の効果検証
      （掃引時にシーンが揃っておらず、レート依存性を分離できていない）
