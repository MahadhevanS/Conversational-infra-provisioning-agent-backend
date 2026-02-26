import json
import os
import uuid
import threading
import time
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from orchestrator import terraform_plan, terraform_apply, terraform_destroy, terraform_cost
from lex import lex_webhook

app = FastAPI()

DB_FILE = "jobs_db.json"

def load_db():
    if not os.path.exists(DB_FILE):
        return {}
    with open(DB_FILE, "r") as f:
        return json.load(f)

def save_db(data):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=4)

# Load existing jobs on startup
jobs = load_db()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- WORKERS WITH PERSISTENCE ---

def update_job_status(job_id, status, result=None):
    """Updates the 'database' and saves to disk."""
    if job_id in jobs:
        jobs[job_id]["status"] = status
        if result is not None:
            jobs[job_id]["result"] = result
        save_db(jobs)

def run_plan_worker(job_id, blueprint, env):
    try:
        update_job_status(job_id, "RUNNING")
        result = terraform_plan(blueprint, job_id)
        
        if "error" in result:
            update_job_status(job_id, "FAILED", result["error"])
        else:
            update_job_status(job_id, "COMPLETED", result["structured_plan"])
    except Exception as e:
        update_job_status(job_id, "FAILED", str(e))

def run_apply_worker(job_id, plan_job_id, blueprint):
    try:
        update_job_status(job_id, "RUNNING")

        result = terraform_apply(plan_job_id, blueprint)

        if "error" in result:
            update_job_status(job_id, "FAILED", result)
        else:
            update_job_status(job_id, "COMPLETED", result)

    except Exception as e:
        update_job_status(job_id, "FAILED", str(e))
                
def run_cost_worker(job_id: str, run_id: str,blueprint):
    try:
        update_job_status(job_id, "RUNNING")
        result = terraform_cost(run_id,blueprint)  # cost runs in background inside orchestrator

        if "error" in result:
            update_job_status(job_id, "FAILED", result)
        else:
            update_job_status(job_id, "COMPLETED", result)
    except Exception as e:
        update_job_status(job_id, "FAILED", str(e))

# --- ROUTES ---

@app.post("/lex-webhook")
def handle_lex(event: dict):
    return lex_webhook(event)

@app.post("/plan")
def plan_infra(payload: dict):
    blueprint = payload.get("infra_blueprint") or payload
    env = blueprint.get("environment", "dev")
    job_id = f"plan-{uuid.uuid4()}"

    jobs[job_id] = {
        "status": "PENDING",
        "type": "PLAN",
        "env": env,
        "created_at": time.time()
    }
    save_db(jobs)

    threading.Thread(target=run_plan_worker, args=(job_id, blueprint, env)).start()
    return {"status": "accepted", "job_id": job_id}

@app.post("/cost")
def cost_infra(payload: dict):
    run_id = payload.get("run_id")
    blueprint = payload.get("infra_blueprint") or payload
    if not run_id:
        raise HTTPException(status_code=400, detail="run_id required")

    job_id = f"cost-{uuid.uuid4()}"

    jobs[job_id] = {
        "status": "PENDING",
        "type": "COST",
        "run_id": run_id,
        "created_at": time.time()
    }
    save_db(jobs)

    threading.Thread(
        target=run_cost_worker,
        args=(job_id, run_id,blueprint),
        daemon=True
    ).start()

    return {
        "status": "accepted",
        "job_id": job_id,
        "run_id": run_id
    }

@app.post("/apply")
def apply_infra(payload: dict):
    plan_job_id = payload.get("job_id")
    blueprint = payload.get("infra_blueprint")
    
    if not plan_job_id:
        raise HTTPException(status_code=400, detail="Plan Job ID required")

    job_id = f"apply-{uuid.uuid4()}"
    jobs[job_id] = {
        "status": "PENDING",
        "type": "APPLY",
        "plan_ref": plan_job_id,
        "created_at": time.time()
    }
    save_db(jobs)

    threading.Thread(target=run_apply_worker, args=(job_id, plan_job_id, blueprint)).start()
    return {"status": "accepted", "apply_job_id": job_id}

@app.get("/status/{job_id}")
def get_status(job_id: str):
    current_jobs = load_db()
    job = current_jobs.get(job_id)

    if not job:
        return {
            "job_id": job_id,
            "status": "NOT_FOUND"
        }

    response = {
        "job_id": job_id,
        "status": job.get("status"),
        "type": job.get("type"),
        "created_at": job.get("created_at")
    }

    # If job completed and has result
    if job.get("status") == "COMPLETED" and "result" in job:

        # PLAN job → extract resources cleanly
        if job.get("type") == "PLAN":
            structured_plan = job["result"]
            resource_changes = structured_plan.get("resource_changes", [])

            resources = []

            for r in resource_changes:
                resources.append({
                    "address": r.get("address"),
                    "type": r.get("type"),
                    "actions": r.get("change", {}).get("actions", [])
                })

            response["resources"] = resources

            # 🔥 IMPORTANT: also return full plan
            response["structured_plan"] = structured_plan

        # APPLY job → return outputs
        elif job.get("type") == "APPLY":
            result = job["result"]

            response["outputs"] = result.get("outputs", {})
            response["access"] = result.get("access", [])

        elif job.get("type") == "COST":
            result = job["result"]
            response["cost_summary"] = result.get("cost_summary", {})
            
    # If failed
    if job.get("status") == "FAILED":
        response["error"] = job.get("result")

    return response