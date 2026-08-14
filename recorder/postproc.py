"""録画後の処理: メタデータ抽出と MP4 プレビュー生成.

ブラウザでレビューできるようにするため、RAW から H.264 MP4 を作る。
公式ツールは AVI (MJPG) しか吐かないので ffmpeg で詰め替える。
実測では 8.25 秒の録画が AVI 3.5 MB → MP4 304 KB、変換は 0.9 秒程度。

RAW は消さない。MP4 はあくまで閲覧用で、再解析には無損失の RAW を使う。
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

_TIMEOUT = 600


@dataclass
class RecordingMeta:
    id: str
    state: str = "processing"       # processing | ready | failed
    note: str = ""
    started_at: str = ""
    duration_s: float | None = None
    events: int | None = None
    event_rate: float | None = None
    encoding: str | None = None
    serial: str | None = None
    generation: str | None = None
    raw_bytes: int | None = None
    preview: str | None = None      # 相対パス
    error: str | None = None


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    # 後処理は録画より優先度を下げる。定期録画では前チャンクの MP4 変換と
    # 次チャンクの録画が同時に走り、4 コアの Pi では変換が全コアを食って
    # 録画ループとスケジューラを飢えさせる（チャンク切替が 20 秒近く
    # 遅れるのを実測した）。nice 10 + ffmpeg 2 スレッドで録画側を守る。
    if cmd[0] == "ffmpeg":
        cmd = cmd[:1] + ["-threads", "2"] + cmd[1:]
    return subprocess.run(["nice", "-n", "10", *cmd],
                          capture_output=True, text=True, timeout=_TIMEOUT)


# metavision_file_info の human_readable_time が使う 6 単位
# （ソース metavision_file_info.cpp:45 で確認: {"d","h","m","s","ms","us"}）
_UNIT_S = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0, "ms": 1e-3, "us": 1e-6}


def parse_info(out: str) -> dict:
    """metavision_file_info の出力テキストから要点を拾う。

    probe() から分離してあるのはテストのため（実カメラ無しで検証できる）。
    """
    info: dict = {}

    def grab(label: str, cast=str):
        m = re.search(rf"^{re.escape(label)}\s{{2,}}(.+?)\s*$", out, re.MULTILINE)
        if m:
            try:
                info[label] = cast(m.group(1))
            except ValueError:
                pass

    grab("Data encoding")
    grab("Camera serial")
    grab("Camera generation")

    # 長さは CD 行の末尾タイムスタンプ [us] から取るのが最も正確。
    # 行の形式: "CD  <count>  <first_ts>  <last_ts>  <rate>"
    m = re.search(r"^CD\s+(\d+)\s+(\d+)\s+(\d+)", out, re.MULTILINE)
    if m:
        info["events"] = int(m.group(1))
        info["duration_s"] = round(int(m.group(3)) / 1e6, 6)

    # フォールバック: Duration 行の人間可読表記（"1m 30s 251ms 989us" 等）。
    # 単位は s/ms/us だけでなく d/h/m もある。以前は s/ms/us しか拾っておらず、
    # 1 分以上の録画で分・時が黙って脱落していた（90 秒の録画が 30 秒になる）。
    # 正規表現の選択肢は長い単位を先に置くこと。"251ms" が 251 分に化ける。
    if "duration_s" not in info:
        m = re.search(r"^Duration\s{2,}(.+?)\s*$", out, re.MULTILINE)
        if m:
            total = 0.0
            for value, unit in re.findall(r"(\d+)\s*(us|ms|d|h|m|s)\b", m.group(1)):
                total += int(value) * _UNIT_S[unit]
            info["duration_s"] = round(total, 6)

    return info


def probe(raw_path: Path) -> dict:
    """metavision_file_info を実行して要点を拾う。

    注意: このツールは表そのものを **stderr** に書く。stdout は空なので、
    stdout だけ読むと黙って何も取れない（実際にそれで全項目 null になった）。
    """
    r = _run(["metavision_file_info", "-i", str(raw_path)])
    return parse_info((r.stdout or "") + (r.stderr or ""))


def make_preview(raw_path: Path, out_dir: Path) -> Path:
    """RAW → AVI → H.264 MP4。ブラウザで再生できる形にする。"""
    avi = out_dir / "_preview.avi"
    mp4 = out_dir / "preview.mp4"

    # metavision_file_to_video は相対パスで親ディレクトリを誤認するので絶対パスを渡す
    r = _run(["metavision_file_to_video", "-i", str(raw_path.resolve()),
              "-o", str(avi.resolve())])
    if r.returncode != 0 or not avi.exists():
        raise RuntimeError(f"metavision_file_to_video が失敗: {r.stderr.strip()[:400]}")

    r = _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(avi),
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
              "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)])
    avi.unlink(missing_ok=True)
    if r.returncode != 0 or not mp4.exists():
        raise RuntimeError(f"ffmpeg が失敗: {r.stderr.strip()[:400]}")
    return mp4


def meta_path(rec_dir: Path) -> Path:
    return rec_dir / "meta.json"


def load_meta(rec_dir: Path) -> RecordingMeta | None:
    p = meta_path(rec_dir)
    if not p.exists():
        return None
    try:
        return RecordingMeta(**json.loads(p.read_text()))
    except Exception:  # noqa: BLE001 — 壊れた meta で一覧全体を落とさない
        return RecordingMeta(id=rec_dir.name, state="failed", error="meta.json が読めません")


def save_meta(rec_dir: Path, meta: RecordingMeta) -> None:
    meta_path(rec_dir).write_text(json.dumps(asdict(meta), ensure_ascii=False, indent=2))


def process_async(rec_dir: Path, meta: RecordingMeta) -> None:
    """停止直後に呼ぶ。バックグラウンドで解析とプレビュー生成を行う。"""

    def work() -> None:
        raw = rec_dir / "events.raw"
        try:
            if not raw.exists():
                raise RuntimeError("events.raw がありません")
            meta.raw_bytes = raw.stat().st_size

            info = probe(raw)
            meta.duration_s = info.get("duration_s")
            meta.events = info.get("events")
            meta.encoding = info.get("Data encoding")
            meta.serial = info.get("Camera serial")
            meta.generation = info.get("Camera generation")
            if meta.duration_s:
                meta.event_rate = (meta.events or 0) / meta.duration_s

            mp4 = make_preview(raw, rec_dir)
            meta.preview = mp4.name
            meta.state = "ready"
        except Exception as exc:  # noqa: BLE001 — 失敗は UI に見せる
            meta.state = "failed"
            meta.error = str(exc)
        finally:
            save_meta(rec_dir, meta)

    save_meta(rec_dir, meta)
    threading.Thread(target=work, name=f"postproc-{meta.id}", daemon=True).start()
