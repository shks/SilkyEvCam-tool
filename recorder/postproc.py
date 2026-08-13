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
    return subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT)


def probe(raw_path: Path) -> dict:
    """metavision_file_info の出力から要点を拾う。

    注意: このツールは表そのものを **stderr** に書く。stdout は空なので、
    stdout だけ読むと黙って何も取れない（実際にそれで全項目 null になった）。
    """
    r = _run(["metavision_file_info", "-i", str(raw_path)])
    out = (r.stdout or "") + (r.stderr or "")
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

    m = re.search(r"^Duration\s{2,}(.+?)\s*$", out, re.MULTILINE)
    if m:
        # "10s 251ms 989us" 形式
        text = m.group(1)
        total = 0.0
        for value, unit in re.findall(r"(\d+)(s|ms|us)", text):
            total += int(value) * {"s": 1.0, "ms": 1e-3, "us": 1e-6}[unit]
        info["duration_s"] = round(total, 6)

    m = re.search(r"^CD\s+(\d+)\s", out, re.MULTILINE)
    if m:
        info["events"] = int(m.group(1))

    return info


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
