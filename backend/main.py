# from fastapi.middleware.cors import CORSMiddleware
# from fastapi import FastAPI
# from orchestrator import terraform_plan
# from orchestrator import terraform_apply
# from lex import lex_webhook
# import uuid
# import threading

# app = FastAPI()

# # ===============================
# # CORS
# # ===============================

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# # ===============================
# # In-Memory Job Store
# # ===============================

# jobs = {}


# # ===============================
# # Background Worker
# # ===============================

# def run_plan_job(job_id: str, blueprint: dict):
#     try:
#         jobs[job_id]["status"] = "RUNNING"
#         # Pass job_id so orchestrator creates a named folder
#         result = terraform_plan(blueprint, job_id) 
#         jobs[job_id]["status"] = "COMPLETED"
#         jobs[job_id]["result"] = result
#     except Exception as e:
#         jobs[job_id]["status"] = "FAILED"
#         jobs[job_id]["result"] = str(e)

# def run_apply_job(new_apply_id: str, job_id: str, blueprint: dict):
#     try:
#         jobs[new_apply_id]["status"] = "RUNNING"
#         # Correctly call the orchestrator with the PLAN's job_id
#         result = terraform_apply(job_id, blueprint) 

#         jobs[new_apply_id]["status"] = "COMPLETED"
#         jobs[new_apply_id]["result"] = result
#     except Exception as e:
#         jobs[new_apply_id]["status"] = "FAILED"
#         jobs[new_apply_id]["result"] = str(e)

# # ===============================
# # PLAN (Async)
# # ===============================

# @app.post("/lex-webhook")
# def handle_lex_webhook(event: dict):
#     return lex_webhook(event)

# @app.post("/plan")
# def generate_plan(blueprint: dict):

#     job_id = str(uuid.uuid4())

#     jobs[job_id] = {
#         "status": "PENDING",
#         "result": None
#     }

#     thread = threading.Thread(
#         target=run_plan_job,
#         args=(job_id, blueprint)
#     )

#     thread.start()

#     return {
#         "status": "accepted",
#         "job_id": job_id
#     }

# # ===============================
# # STATUS CHECK
# # ===============================

# @app.get("/status/{job_id}")
# def get_status(job_id: str):

#     job = jobs.get(job_id)

#     if not job:
#         return {
#             "status": "NOT_FOUND"
#         }

#     return job


# # ===============================
# # HEALTH CHECK
# # ===============================

# @app.get("/health")
# def health():
#     return {"status": "ok"}

# # ===============================
# # APPROVAL GATING
# # ===============================
# # 2. Update the POST /apply route
# @app.post("/apply")
# def generate_apply(payload: dict):
#     # Retrieve the original job_id from the plan phase
#     job_id = payload.get("job_id") 
#     blueprint = payload.get("infra_blueprint")
    
#     if not job_id:
#         return {"status": "error", "message": "Missing job_id"}

#     new_apply_id = str(uuid.uuid4())
#     jobs[new_apply_id] = {"status": "PENDING", "result": None}

#     # Pass all 3 required arguments to the worker
#     thread = threading.Thread(
#         target=run_apply_job, 
#         args=(new_apply_id, job_id, blueprint)
#     )
#     thread.start()
    
#     return {
#         "status": "accepted", 
#         "apply_job_id": new_apply_id
#     }

from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, HTTPException
from orchestrator import terraform_plan, terraform_apply
from lex import lex_webhook
import uuid
import threading

app = FastAPI()

# ===============================
# CORS
# ===============================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===============================
# In-Memory Job Store
# ===============================
# Structure: { job_id: { "status": "...", "result": ..., "type": "PLAN|APPLY" } }
jobs = {}

# ===============================
# Background Workers
# ===============================

def run_plan_job(job_id: str, blueprint: dict):
    try:
        jobs[job_id]["status"] = "RUNNING"
        # Pass job_id so orchestrator creates a named folder
        result = terraform_plan(blueprint, job_id) 
        jobs[job_id]["status"] = "COMPLETED"
        jobs[job_id]["result"] = result
    except Exception as e:
        jobs[job_id]["status"] = "FAILED"
        jobs[job_id]["result"] = str(e)

def run_apply_job(apply_id: str, plan_job_id: str, blueprint: dict):
    try:
        jobs[apply_id]["status"] = "RUNNING"
        # Correctly call the orchestrator with the original PLAN's job_id to find the .tf files
        result = terraform_apply(plan_job_id, blueprint) 

        jobs[apply_id]["status"] = "COMPLETED"
        jobs[apply_id]["result"] = result
    except Exception as e:
        jobs[apply_id]["status"] = "FAILED"
        jobs[apply_id]["result"] = str(e)

# ===============================
# ENDPOINTS
# ===============================

@app.post("/lex-webhook")
def handle_lex_webhook(event: dict):
    # Lex will call this; ensure your lex.py logic sets plan_job_id in session attributes
    return lex_webhook(event)

@app.post("/plan")
def generate_plan(blueprint: dict):
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "PENDING",
        "result": None,
        "type": "PLAN"
    }

    thread = threading.Thread(
        target=run_plan_job,
        args=(job_id, blueprint)
    )
    thread.start()

    return {
        "status": "accepted",
        "job_id": job_id
    }

@app.post("/apply")
def generate_apply(payload: dict):
    plan_job_id = payload.get("job_id") 
    blueprint = payload.get("infra_blueprint")
    
    if not plan_job_id:
        raise HTTPException(status_code=400, detail="Missing job_id from planning phase")

    apply_job_id = f"apply-{str(uuid.uuid4())}" # Prefixing helps debugging
    jobs[apply_job_id] = {
        "status": "PENDING", 
        "result": None,
        "type": "APPLY"
    }

    thread = threading.Thread(
        target=run_apply_job, 
        args=(apply_job_id, plan_job_id, blueprint)
    )
    thread.start()
    
    return {
        "status": "accepted", 
        "apply_job_id": apply_job_id
    }

@app.get("/status/{job_id}")
def get_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return {"status": "NOT_FOUND"}
    return job

@app.get("/health")
def health():
    return {"status": "ok", "active_jobs": len(jobs)}