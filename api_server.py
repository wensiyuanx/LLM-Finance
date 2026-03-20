import os
import uuid
import time
import traceback
import threading
from typing import Optional, Dict
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import tos

from database.db import SessionLocal, init_db
from database.models import BacktestRecord

# For running existing backtest logic
from scripts.run_etf_backtest import fetch_and_save_data as etf_fetch, run_backtest as etf_backtest
from scripts.run_backtest import fetch_and_save_data as std_fetch, run_backtest as std_backtest

app = FastAPI(title="LLM-Finance Backtest API")

from dotenv import load_dotenv
load_dotenv()

# TOS credentials
TOS_AK = os.getenv("TOS_AK", "")
TOS_SK = os.getenv("TOS_SK", "")
TOS_ENDPOINT = os.getenv("TOS_ENDPOINT", "tos-cn-beijing.volces.com")
TOS_REGION = os.getenv("TOS_REGION", "cn-beijing")
TOS_BUCKET = os.getenv("TOS_BUCKET", "")
TOS_PATH = os.getenv("TOS_PATH", "data/uploads")

# ---------------------------------------------------------------------------
# In-memory job store (keyed by job_id)
# status: "pending" | "running" | "done" | "error"
# ---------------------------------------------------------------------------
jobs: Dict[str, dict] = {}
jobs_lock = threading.Lock()


class BacktestRequest(BaseModel):
    code: str
    days: int = 365
    cash: float = 100000.0
    strategy: str = "etf"  # "etf" or "standard"
    user_id: int = 1


def upload_to_tos(local_file_path: str, object_key: str) -> str:
    try:
        client = tos.TosClientV2(TOS_AK, TOS_SK, TOS_ENDPOINT, TOS_REGION)
        client.put_object_from_file(TOS_BUCKET, object_key, local_file_path)
        url = f"https://{TOS_BUCKET}.{TOS_ENDPOINT}/{object_key}"
        return url
    except Exception as e:
        print(f"Error uploading to TOS: {e}")
        return ""


def run_backtest_job(job_id: str, req: BacktestRequest):
    """Execute the backtest in a background thread and update job status."""
    with jobs_lock:
        jobs[job_id]["status"] = "running"

    try:
        code = req.code
        strategy = req.strategy.lower()

        if strategy == "etf":
            success = etf_fetch(code, req.days)
            if not success:
                raise RuntimeError("Failed to fetch data for ETF backtest. Check that FutuOpenD is running.")
            etf_backtest(code, req.cash)
            local_plot_file = f"etf_backtest_result_{code}.png"
        else:
            success = std_fetch(code, req.days)
            if not success:
                raise RuntimeError("Failed to fetch data for standard backtest. Check that FutuOpenD is running.")
            std_backtest(code, req.cash)
            local_plot_file = f"backtest_result_{code}.png"

        if not os.path.exists(local_plot_file):
            raise RuntimeError(f"Plot file was not generated: {local_plot_file}")

        # Upload to TOS
        file_id = str(uuid.uuid4())[:8]
        object_key = f"{TOS_PATH}/{code}_{int(time.time())}_{file_id}.png"
        oss_url = upload_to_tos(local_plot_file, object_key)
        if not oss_url:
            raise RuntimeError("Failed to upload backtest plot to TOS")

        # Record in database
        db = SessionLocal()
        try:
            record = BacktestRecord(user_id=req.user_id, code=code, oss_url=oss_url)
            db.add(record)
            db.commit()
            db.refresh(record)
            record_id = record.id
        except Exception as e:
            db.rollback()
            raise RuntimeError(f"Database error: {str(e)}")
        finally:
            db.close()

        with jobs_lock:
            jobs[job_id].update({
                "status": "done",
                "record_id": record_id,
                "oss_url": oss_url,
                "message": "Backtest completed and plot uploaded successfully."
            })

    except Exception as e:
        with jobs_lock:
            jobs[job_id].update({
                "status": "error",
                "detail": f"{str(e)}\n{traceback.format_exc()}"
            })


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/health")
def health_check():
    """Check API status and FutuOpenD connectivity."""
    import socket
    futu_ok = False
    try:
        s = socket.create_connection(("127.0.0.1", 11111), timeout=2)
        s.close()
        futu_ok = True
    except Exception:
        pass
    return {
        "status": "ok",
        "futu_opend_connected": futu_ok,
        "tos_bucket": TOS_BUCKET or "NOT SET"
    }


@app.post("/api/backtest", status_code=202)
def trigger_backtest(req: BacktestRequest):
    """
    Submit a backtest job. Returns immediately with a job_id.
    Poll GET /api/backtest/{job_id} to check status.
    """
    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "job_id": job_id,
            "status": "pending",
            "code": req.code,
            "strategy": req.strategy,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")
        }

    # Start background thread
    t = threading.Thread(target=run_backtest_job, args=(job_id, req), daemon=True)
    t.start()

    return {
        "job_id": job_id,
        "status": "pending",
        "message": "Backtest job submitted. Poll /api/backtest/{job_id} for status.",
        "poll_url": f"/api/backtest/{job_id}"
    }


@app.get("/api/backtest/{job_id}")
def get_backtest_status(job_id: str):
    """Poll the status of a submitted backtest job."""
    with jobs_lock:
        job = jobs.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found.")

    return job


@app.get("/api/backtest")
def list_jobs():
    """List all submitted backtest jobs."""
    with jobs_lock:
        return list(jobs.values())


if __name__ == "__main__":
    import uvicorn
    # Make sure PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python is set in your env
    uvicorn.run("api_server:app", host="0.0.0.0", port=8069, reload=True)
