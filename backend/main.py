import json
import os
import uuid
import threading
import time
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .orchestrator import terraform_plan, terraform_apply, infracost_run, run_status, run_result
from .lex import lex_webhook

app = FastAPI()

# -------------------------------
# PERSISTENCE (survive restart)
# -------------------------------
DB_FILE = os.path.join(os.path.dirname(__file__), "..", "jobs_db.json")
DB_FILE = os.path.abspath(DB_FILE)

def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(data):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

jobs = load_db()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------
# JOB HELPERS
# -------------------------------
def create_job(job_type: str, run_id: str, extra: dict | None = None):
    job_id = f"{job_type.lower()}-{uuid.uuid4()}"
    jobs[job_id] = {
        "status": "PENDING",
        "type": job_type,
        "run_id": run_id,
        "created_at": time.time(),
        **(extra or {})
    }
    save_db(jobs)
    return job_id

def update_job_status(job_id: str, status: str, error: str | None = None):
    if job_id not in jobs:
        return
    jobs[job_id]["status"] = status
    if error:
        jobs[job_id]["error"] = error
    save_db(jobs)

# -------------------------------
# WORKERS
# -------------------------------
def run_plan_worker(job_id: str, blueprint: dict, run_id: str):
    try:
        update_job_status(job_id, "RUNNING")
        terraform_plan(blueprint, run_id)  # plan runs in background inside orchestrator
        update_job_status(job_id, "RUNNING")
    except Exception as e:
        update_job_status(job_id, "FAILED", str(e))

def run_cost_worker(job_id: str, run_id: str):
    try:
        update_job_status(job_id, "RUNNING")
        infracost_run(run_id)  # cost runs in background inside orchestrator
        update_job_status(job_id, "RUNNING")
    except Exception as e:
        update_job_status(job_id, "FAILED", str(e))

def run_apply_worker(job_id: str, run_id: str, blueprint: dict):
    try:
        update_job_status(job_id, "RUNNING")
        terraform_apply(run_id, blueprint)  # apply runs in background inside orchestrator
        update_job_status(job_id, "RUNNING")
    except Exception as e:
        update_job_status(job_id, "FAILED", str(e))

# -------------------------------
# ROUTES
# -------------------------------
@app.post("/lex-webhook")
def handle_lex(event: dict):
    return lex_webhook(event)

# 1) PLAN BUTTON
@app.post("/plan")
def plan_infra(payload: dict):
    blueprint = payload.get("infra_blueprint") or payload
    run_id = uuid.uuid4().hex[:8]

    job_id = create_job(
        job_type="PLAN",
        run_id=run_id,
        extra={"env": blueprint.get("environment", "development")}
    )

    threading.Thread(target=run_plan_worker, args=(job_id, blueprint, run_id), daemon=True).start()
    return {"status": "accepted", "job_id": job_id, "run_id": run_id}

# 2) COST BUTTON
@app.post("/cost")
def cost_infra(payload: dict):
    run_id = payload.get("run_id")
    if not run_id:
        raise HTTPException(status_code=400, detail="run_id required")

    job_id = create_job(job_type="COST", run_id=run_id)
    threading.Thread(target=run_cost_worker, args=(job_id, run_id), daemon=True).start()
    return {"status": "accepted", "job_id": job_id, "run_id": run_id}

# 3) APPLY BUTTON
@app.post("/apply")
def apply_infra(payload: dict):
    run_id = payload.get("run_id")
    blueprint = payload.get("infra_blueprint") or payload.get("blueprint")

    if not run_id:
        raise HTTPException(status_code=400, detail="run_id required")
    if not blueprint:
        raise HTTPException(status_code=400, detail="infra_blueprint required")

    job_id = create_job(job_type="APPLY", run_id=run_id)
    threading.Thread(target=run_apply_worker, args=(job_id, run_id, blueprint), daemon=True).start()
    return {"status": "accepted", "job_id": job_id, "run_id": run_id}

# 4) STATUS POLL (by job_id)
@app.get("/status/{job_id}")
def get_status(job_id: str):
    current_jobs = load_db()
    job = current_jobs.get(job_id)
    if not job:
        return {"job_id": job_id, "status": "NOT_FOUND"}

    run_id = job.get("run_id")

    resp = {
        "job_id": job_id,
        "run_id": run_id,
        "status": job.get("status"),
        "type": job.get("type"),
        "created_at": job.get("created_at"),
    }

    # if worker itself failed
    if job.get("status") == "FAILED":
        resp["error"] = job.get("error")
        return resp

    # Poll runtime status
    if run_id:
        step_status = run_status(run_id)
        resp["step_status"] = step_status

        s = (step_status.get("status") or "").upper()

        if s in ["PLAN_DONE", "COST_DONE", "APPLY_DONE"]:
            current_jobs[job_id]["status"] = "COMPLETED"
            save_db(current_jobs)
            resp["status"] = "COMPLETED"

        if s in ["PLAN_FAILED", "COST_FAILED", "APPLY_FAILED", "PLAN_BLOCKED"]:
            current_jobs[job_id]["status"] = "FAILED"
            current_jobs[job_id]["error"] = step_status.get("message")
            save_db(current_jobs)
            resp["status"] = "FAILED"
            resp["error"] = current_jobs[job_id]["error"]

    return resp

# 5) RESULT (optional)
@app.get("/result/{run_id}")
def get_result(run_id: str):
    return run_result(run_id)