"""SilkyEvCam 録画サーバ.

    source env.sh
    python -m recorder.app            # http://127.0.0.1:8000

カメラは排他アクセスなので、このプロセスが起動している間は
metavision_viewer や motion_viewer と同時に使えない。

スケジューラは今回は作らない。ただし録画の開始・停止は HTTP API に
分離してあるので、後から cron なり常駐スケジューラなりを
API を叩く側として足せる（UI 側の作り直しは不要）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from . import postproc
from .camera import CameraWorker
from .postproc import RecordingMeta

ROOT = Path(__file__).resolve().parent.parent
RECORDINGS = ROOT / "out" / "recordings"
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="EvCam Recorder")
worker = CameraWorker(RECORDINGS)


class StartBody(BaseModel):
    note: str = ""


@app.on_event("startup")
def _startup() -> None:
    RECORDINGS.mkdir(parents=True, exist_ok=True)
    worker.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    worker.shutdown()


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text()


@app.get("/static/{name}")
def static_file(name: str):
    p = STATIC / name
    if not p.exists() or p.parent != STATIC:
        raise HTTPException(404)
    return FileResponse(p)


@app.get("/api/status")
def status() -> dict:
    s = worker.status()
    usage = shutil.disk_usage(RECORDINGS)
    out = vars(s).copy()
    out["disk_free_bytes"] = usage.free
    # 現在の書き込みレートで残り何時間録れるか。
    # 静止シーンでも 0.31 MB/s 出るので、この見積もりは常に意味がある。
    rate = s.write_rate if s.recording and s.write_rate > 0 else 310_000
    out["disk_hours_left"] = usage.free / rate / 3600
    return out


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
    rec_id = dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    try:
        worker.begin_recording(rec_id)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc

    meta = RecordingMeta(id=rec_id, state="recording", note=body.note,
                         started_at=dt.datetime.now().isoformat(timespec="seconds"))
    (RECORDINGS / rec_id).mkdir(parents=True, exist_ok=True)
    postproc.save_meta(RECORDINGS / rec_id, meta)
    return {"id": rec_id}


@app.post("/api/record/stop")
def record_stop() -> dict:
    s = worker.status()
    rec_id = s.recording_id
    try:
        worker.end_recording()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    worker.wait_idle()

    if rec_id:
        rec_dir = RECORDINGS / rec_id
        meta = postproc.load_meta(rec_dir) or RecordingMeta(id=rec_id)
        meta.state = "processing"
        postproc.process_async(rec_dir, meta)
    return {"id": rec_id}


@app.get("/api/recordings")
def recordings() -> list[dict]:
    items = []
    for d in sorted(RECORDINGS.iterdir(), reverse=True) if RECORDINGS.exists() else []:
        if not d.is_dir():
            continue
        meta = postproc.load_meta(d)
        if meta is None:
            continue
        row = vars(meta).copy()
        raw = d / "events.raw"
        row["raw_bytes"] = raw.stat().st_size if raw.exists() else row.get("raw_bytes")
        items.append(row)
    return items


@app.get("/api/recordings/{rec_id}/preview.mp4")
def preview_file(rec_id: str):
    p = RECORDINGS / rec_id / "preview.mp4"
    if p.parent.parent != RECORDINGS or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="video/mp4")


@app.get("/api/recordings/{rec_id}/events.raw")
def raw_file(rec_id: str):
    p = RECORDINGS / rec_id / "events.raw"
    if p.parent.parent != RECORDINGS or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="application/octet-stream",
                        filename=f"{rec_id}.raw")


@app.delete("/api/recordings/{rec_id}")
def delete_recording(rec_id: str) -> dict:
    d = RECORDINGS / rec_id
    if d.parent != RECORDINGS or not d.is_dir():
        raise HTTPException(404)
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
