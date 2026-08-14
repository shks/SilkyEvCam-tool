"""SilkyEvCam 録画サーバ.

    source env.sh
    python -m recorder.app            # http://127.0.0.1:8000

カメラは排他アクセスなので、このプロセスが起動している間は
metavision_viewer や motion_viewer と同時に使えない。

録画の開始・停止は _do_start / _do_stop に共通化してあり、
手動（HTTP API）と定期実行（recorder/scheduler.py）の両方がこれを使う。
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import shutil
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from . import postproc
from .camera import CameraWorker
from .postproc import RecordingMeta
from .scheduler import ScheduleConfig, Scheduler, validate_config

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RECORDINGS = ROOT / "out" / "recordings"
SCHEDULER_CONFIG = ROOT / "out" / "scheduler.json"
STATIC = Path(__file__).resolve().parent / "static"

# 保存先は設定で変えられるため可変。読み書きは recordings_dir() 経由で行う。
_recordings_dir = DEFAULT_RECORDINGS

app = FastAPI(title="EvCam Recorder")
worker = CameraWorker(DEFAULT_RECORDINGS)


def recordings_dir() -> Path:
    return _recordings_dir


def _resolve_dest(dest: str) -> Path:
    """設定の dest_dir を絶対パスに解決する。空は既定の場所。"""
    if not dest:
        return DEFAULT_RECORDINGS
    p = Path(dest).expanduser()
    if not p.is_absolute():
        p = ROOT / p
    return p.resolve()


def _apply_dest(dest: str) -> None:
    """保存先を検証して切り替える。書けない場所は 400 で拒否する。

    録画中の切り替えは呼び出し側で 409 にする（RAW と meta が別の場所に
    割れるため）。
    """
    global _recordings_dir
    p = _resolve_dest(dest)
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write_test"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise HTTPException(400, f"保存先に書き込めません: {p} ({exc})") from exc
    _recordings_dir = p
    worker.recordings_dir = p


class StartBody(BaseModel):
    note: str = ""


class SchedulerBody(BaseModel):
    enabled: bool = False
    start: str = "08:00"
    end: str = "18:00"
    chunk_minutes: int = 10
    dest_dir: str = ""
    min_free_gb: float = 2.0


# 録画 ID は record_start が生成する形式（ISO 時刻）だけを受け付ける。
# この検証なしで RECORDINGS / rec_id を組み立てると、rec_id=".." のとき
# pathlib の .parent が字句的に働いて containment チェックを素通りし、
# DELETE /api/recordings/.. が out/ を丸ごと rmtree できてしまう（再現確認済み）。
_REC_ID = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}$")


def _rec_dir(rec_id: str) -> Path:
    if not _REC_ID.fullmatch(rec_id):
        raise HTTPException(404)
    d = recordings_dir() / rec_id
    if not d.is_dir():
        raise HTTPException(404)
    return d


def _do_start(note: str) -> str:
    """録画を開始し ID を返す。手動 API とスケジューラの共通経路。

    失敗は RuntimeError（呼び出し側が 409 なり再試行なりにする）。
    """
    rec_id = dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    worker.begin_recording(rec_id)
    meta = RecordingMeta(id=rec_id, state="recording", note=note,
                         started_at=dt.datetime.now().isoformat(timespec="seconds"))
    (recordings_dir() / rec_id).mkdir(parents=True, exist_ok=True)
    postproc.save_meta(recordings_dir() / rec_id, meta)
    return rec_id


def _do_stop() -> str | None:
    """録画を停止し、後処理を起動して ID を返す。共通経路。"""
    rec_id = worker.end_recording()
    worker.wait_idle()
    if rec_id:
        rec_dir = recordings_dir() / rec_id
        meta = postproc.load_meta(rec_dir) or RecordingMeta(id=rec_id)
        meta.state = "processing"
        postproc.process_async(rec_dir, meta)
    return rec_id


scheduler = Scheduler(
    config_path=SCHEDULER_CONFIG,
    get_status=worker.status,
    start_fn=_do_start,
    stop_fn=_do_stop,
    free_bytes_fn=lambda: shutil.disk_usage(recordings_dir()).free,
)


def _recover_interrupted(base: Path) -> None:
    # 前回の異常終了からの回復。録画中・後処理中に落ちると meta が
    # "recording"/"processing" のまま残り、UI に永久にそう表示され続ける。
    # events.raw は log_raw_data が逐次書いているので、中断時点までの内容は
    # 有効なファイルとして読める。改めて後処理にかける。
    for d in sorted(base.iterdir()) if base.exists() else []:
        if not d.is_dir():
            continue
        meta = postproc.load_meta(d)
        if meta is None or meta.state not in ("recording", "processing"):
            continue
        if meta.state == "recording" and "[中断された録画]" not in meta.note:
            meta.note = (meta.note + " " if meta.note else "") + "[中断された録画]"
        meta.state = "processing"
        meta.error = None
        postproc.process_async(d, meta)


@app.on_event("startup")
def _startup() -> None:
    # 保存先はスケジューラ設定に永続化されている。起動時に反映する。
    # 書けない場所が保存されていた場合（USB SSD が抜かれた等）は既定に退避し、
    # エラーを UI に出す。
    try:
        _apply_dest(scheduler.config.dest_dir)
    except HTTPException as exc:
        scheduler.last_error = str(exc.detail)
        _apply_dest("")
    _recover_interrupted(recordings_dir())
    worker.start()
    scheduler.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    scheduler.shutdown()
    worker.shutdown()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text()


@app.get("/static/{name}")
def static_file(name: str):
    # ".." 等を弾く。パス結合前に名前そのものを検証する（_REC_ID と同じ理由）
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name.startswith("."):
        raise HTTPException(404)
    p = STATIC / name
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p)


def estimate_write_rate(event_rate: float) -> float:
    """イベントレートから RAW の書き込みレート [B/s] を見積もる。

    EVT3 のワード内訳を実測して組み立てた式（docs/tech-notes.md「イベントカメラのデータ形式」）:

      EVT_TIME_HIGH  イベントの有無に関わらず約 62 kHz で出続ける → 125 kB/s の下限
      EVT_ADDR_X     1 イベント 1 ワード
      EVT_TIME_LOW   イベントのある us ごとに 1 ワード（1 MHz で頭打ち）
      EVT_ADDR_Y     (us, 行) の組ごとに 1 ワード。密になるほど 1 行に相乗りする

    実測との突き合わせ: 29.7 kev/s → 303 kB/s（実測 308）、
    6.15 Mev/s → 20.4 MB/s（実測 20.4）。

    固定値で代用すると桁を間違える。実際、非録画時に 310 kB/s 決め打ちにしていた
    ときは「残り 514 時間」と出ていたが、そのときの実レートでは約 10 時間だった。
    """
    ev = max(0.0, event_rate)
    # 密なシーンでは ADDR_Y 1 つあたり平均 2.07 イベントが相乗りする（実測）
    share = 1.0 + 1.07 * min(1.0, ev / 6.0e6)
    addr_x = 2.0 * ev
    time_low = 2.0 * min(ev, 1.0e6)
    addr_y = 2.0 * ev / share
    return 125_000.0 + addr_x + time_low + addr_y


@app.get("/api/status")
def status() -> dict:
    s = worker.status()
    usage = shutil.disk_usage(recordings_dir())
    out = vars(s).copy()
    out["disk_free_bytes"] = usage.free
    # 録画中は実測値、そうでなければ現在のイベントレートからの見積もり。
    rate = s.write_rate if s.recording and s.write_rate > 0 else estimate_write_rate(s.event_rate)
    out["estimated_write_rate"] = rate
    out["disk_hours_left"] = usage.free / rate / 3600
    return out


@app.get("/api/preview.jpg")
def preview_still():
    """ライブの 1 フレームだけを返す。

    サムネイル取得やスクリプトからの状態確認用。MJPEG ストリーム
    （/api/preview.mjpg）はヘッドレスブラウザや curl と相性が悪いので、
    単発で足りる用途はこちらを使う。
    """
    jpeg = worker.latest_jpeg(timeout=2.0)
    if jpeg is None:
        raise HTTPException(503, "プレビューがまだありません")
    from fastapi import Response
    return Response(content=jpeg, media_type="image/jpeg")


@app.get("/api/preview.mjpg")
def preview():
    def gen():
        while True:
            jpeg = worker.latest_jpeg(timeout=2.0)
            if jpeg is None:
                continue
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                   b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.post("/api/record/start")
def record_start(body: StartBody) -> dict:
    try:
        return {"id": _do_start(body.note)}
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/record/stop")
def record_stop() -> dict:
    # 停止対象の ID は end_recording の戻り値から取る（_do_stop 内）。
    # status() から取ると、開始要求がまだワーカーに拾われていない窓（〜20ms）で
    # recording_id が None になり、その開始が誰にも止められなくなる。
    try:
        return {"id": _do_stop()}
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/scheduler")
def scheduler_get() -> dict:
    out = scheduler.snapshot()
    out["dest_dir_resolved"] = str(_resolve_dest(out["dest_dir"]))
    return out


@app.put("/api/scheduler")
def scheduler_put(body: SchedulerBody) -> dict:
    cfg = ScheduleConfig(**body.model_dump())
    errors = validate_config(asdict(cfg))
    if errors:
        raise HTTPException(400, " / ".join(errors))

    # 保存先の変更は録画中には行わない。RAW を書いている場所と meta を書く
    # 場所が割れて、その録画が一覧から消える（実害を確認してこの仕様にした）。
    # ただしスケジューラ自身の録画を「無効化しつつ保存先も変える」要求は、
    # 先に同期停止してから適用する（1 リクエストで復元できないと不便）。
    dest_changed = _resolve_dest(cfg.dest_dir) != recordings_dir()
    if dest_changed and worker.status().recording:
        if not cfg.enabled and worker.status().recording_id == scheduler.owned_id:
            scheduler.save(cfg)          # 先に無効化を保存（tick との競合を断つ）
            scheduler.stop_owned()
        else:
            raise HTTPException(409, "録画中は保存先を変更できません。停止してから変更してください")
    if dest_changed:
        _apply_dest(cfg.dest_dir)           # 書けない場所なら 400 でここで止まる

    scheduler.save(cfg)
    return scheduler_get()


@app.get("/api/recordings")
def recordings() -> list[dict]:
    items = []
    base = recordings_dir()
    for d in sorted(base.iterdir(), reverse=True) if base.exists() else []:
        if not d.is_dir():
            continue
        meta = postproc.load_meta(d)
        if meta is None:
            continue
        row = vars(meta).copy()
        raw = d / "events.raw"
        row["raw_bytes"] = raw.stat().st_size if raw.exists() else row.get("raw_bytes")
        # MP4 は閲覧用の派生物なので、RAW と並べて一覧に出せるようサイズも返す
        mp4 = d / "preview.mp4"
        row["preview_bytes"] = mp4.stat().st_size if mp4.exists() else None
        items.append(row)
    return items


@app.get("/api/recordings/{rec_id}/preview.mp4")
def preview_file(rec_id: str):
    p = _rec_dir(rec_id) / "preview.mp4"
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, media_type="video/mp4")


@app.get("/api/recordings/{rec_id}/events.raw")
def raw_file(rec_id: str):
    p = _rec_dir(rec_id) / "events.raw"
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, media_type="application/octet-stream",
                        filename=f"{rec_id}.raw")


@app.delete("/api/recordings/{rec_id}")
def delete_recording(rec_id: str) -> dict:
    d = _rec_dir(rec_id)
    if worker.status().recording_id == rec_id:
        raise HTTPException(409, "録画中です")
    shutil.rmtree(d)
    return {"deleted": rec_id}


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
