import os
import uuid
import time
from typing import Optional
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

class BacktestRequest(BaseModel):
    code: str
    days: int = 365
    cash: float = 100000.0
    strategy: str = "etf" # "etf" or "standard"
    user_id: int = 1

def upload_to_tos(local_file_path: str, object_key: str) -> str:
    # Initialize TOS client
    try:
        client = tos.TosClientV2(TOS_AK, TOS_SK, TOS_ENDPOINT, TOS_REGION)
        client.put_object_from_file(TOS_BUCKET, object_key, local_file_path)
        # Generate the public URL
        url = f"https://{TOS_BUCKET}.{TOS_ENDPOINT}/{object_key}"
        return url
    except Exception as e:
        print(f"Error uploading to TOS: {e}")
        return ""

@app.on_event("startup")
def on_startup():
    init_db()

@app.post("/api/backtest")
def trigger_backtest(req: BacktestRequest):
    code = req.code
    strategy = req.strategy.lower()

    if strategy == "etf":
        success = etf_fetch(code, req.days)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to fetch data for ETF backtest")
        etf_backtest(code, req.cash)
        local_plot_file = f"etf_backtest_result_{code}.png"
    else:
        success = std_fetch(code, req.days)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to fetch data for standard backtest")
        std_backtest(code, req.cash)
        local_plot_file = f"backtest_result_{code}.png"

    # Verify if file was created
    if not os.path.exists(local_plot_file):
        raise HTTPException(status_code=500, detail=f"Backtest completed but plot file was not generated: {local_plot_file}")

    # Upload to TOS
    file_id = str(uuid.uuid4())[:8]
    object_key = f"{TOS_PATH}/{code}_{int(time.time())}_{file_id}.png"
    oss_url = upload_to_tos(local_plot_file, object_key)

    if not oss_url:
        raise HTTPException(status_code=500, detail="Failed to upload backtest plot to TOS")

    # Record in database
    db = SessionLocal()
    try:
        record = BacktestRecord(
            user_id=req.user_id,
            code=code,
            oss_url=oss_url
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        record_id = record.id
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
    finally:
        db.close()

    return {
        "status": "success",
        "message": "Backtest completed and plot uploaded successfully.",
        "record_id": record_id,
        "oss_url": oss_url
    }

if __name__ == "__main__":
    import uvicorn
    # Make sure PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python is set in your env
    uvicorn.run("api_server:app", host="0.0.0.0", port=8000, reload=True)
