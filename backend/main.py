import uuid
import threading
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from backend.db import supabase
from backend.orchestrator import terraform_plan, terraform_apply, terraform_destroy, terraform_cost
from backend.lex import lex_webhook

app = FastAPI()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# --- DB HELPERS ---
security = HTTPBearer()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials

    response = supabase.auth.get_user(token)

    if response.user is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    return response.user
                
def get_project_credentials(project_id, user_id):
    project_res = supabase.table("projects") \
        .select("user_id") \
        .eq("project_id", project_id) \
        .single() \
        .execute()

    if not project_res.data:
        raise HTTPException(status_code=404, detail="Project not found")

    if project_res.data["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")

    cred_res = supabase.table("aws_credentials") \
        .select("role_arn, external_id") \
        .eq("user_id", user_id) \
        .single() \
        .execute()

    if not cred_res.data:
        raise HTTPException(status_code=400, detail="AWS IAM Role not configured")

    return cred_res.data

# --- WORKERS WITH PERSISTENCE ---

def update_job_status(job_id, status, result=None):
    update_data = {"status": status}

    if status == "FAILED":
        update_data["error_message"] = str(result)
        update_data["result"] = None
    elif result is not None:
        update_data["result"] = result

    supabase.table("jobs") \
        .update(update_data) \
        .eq("job_id", job_id) \
        .execute()

def run_plan_worker(project_id, job_id, blueprint, credentials=None):
    try:
        update_job_status(job_id, "RUNNING")
        result = terraform_plan(project_id, blueprint, job_id, credentials=credentials)
        
        if "error" in result:
            update_job_status(job_id, "FAILED", result["error"])
        else:
            update_job_status(job_id, "COMPLETED", result["structured_plan"])
    except Exception as e:
        update_job_status(job_id, "FAILED", str(e))

def run_apply_worker(project_id, job_id, plan_job_id, blueprint, credentials=None):
    try:
        update_job_status(job_id, "RUNNING")

        result = terraform_apply(project_id, plan_job_id, blueprint, credentials=credentials)

        if "error" in result:
            update_job_status(job_id, "FAILED", result)
        else:
            update_job_status(job_id, "COMPLETED", result)

    except Exception as e:
        update_job_status(job_id, "FAILED", str(e))
                
def run_cost_worker(project_id, job_id, run_id, blueprint, credentials=None):
    try:
        update_job_status(job_id, "RUNNING")
        result = terraform_cost(project_id, run_id, blueprint, credentials=credentials)

        if "error" in result:
            update_job_status(job_id, "FAILED", result)
        else:
            update_job_status(job_id, "COMPLETED", result)
    except Exception as e:
        update_job_status(job_id, "FAILED", str(e))

# --- ROUTES ---
@app.post("/signup")
def signup(payload: dict):
    email = payload.get("email")
    password = payload.get("password")
    role_arn = payload.get("role_arn")
    external_id = payload.get("external_id")

    if not email or not password or not role_arn:
        raise HTTPException(
            status_code=400,
            detail="email, password, and role_arn required"
        )

    # 1️⃣ Create Supabase user
    auth_response = supabase.auth.sign_up({
        "email": email,
        "password": password
    })

    if auth_response.user is None:
        raise HTTPException(status_code=400, detail="Signup failed")

    user_id = auth_response.user.id

    # 2️⃣ OPTIONAL: Validate IAM role immediately
    try:
        import boto3

        sts = boto3.client("sts")
        assume_params = {
            "RoleArn": role_arn,
            "RoleSessionName": "SignupValidation"
        }

        if external_id:
            assume_params["ExternalId"] = external_id

        sts.assume_role(**assume_params)

    except Exception as e:
        # Rollback user creation if IAM invalid
        supabase.auth.admin.delete_user(user_id)
        raise HTTPException(
            status_code=400,
            detail=f"IAM Role validation failed: {str(e)}"
        )

    # 3️⃣ Store AWS credentials reference
    supabase.table("aws_credentials").insert({
        "user_id": user_id,
        "role_arn": role_arn,
        "external_id": external_id
    }).execute()

    return {
        "message": "Signup successful",
        "user_id": user_id
    }

@app.post("/login")
def login(payload: dict):
    email = payload.get("email")
    password = payload.get("password")

    if not email or not password:
        raise HTTPException(status_code=400, detail="email and password required")

    response = supabase.auth.sign_in_with_password({
        "email": email,
        "password": password
    })

    if response.session is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return {
        "access_token": response.session.access_token,
        "refresh_token": response.session.refresh_token,
        "user_id": response.user.id
    }

@app.post("/projects")
def create_project(payload: dict, user=Depends(get_current_user)):
    project_name = payload.get("project_name")
    environment = payload.get("environment", "development")

    if not project_name:
        raise HTTPException(status_code=400, detail="project_name required")

    project = supabase.table("projects").insert({
        "user_id": user.id,
        "project_name": project_name,
        "environment": environment
    }).execute()

    return project.data[0]

@app.post("/lex-webhook")
def handle_lex(event: dict):
    return lex_webhook(event)

@app.post("/plan")
def plan_infra(payload: dict,user = Depends(get_current_user)):
    user_id = user.id
    project_id = payload.get("project_id")
    blueprint = payload.get("infra_blueprint") or payload
    
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id required")
    
    credentials = get_project_credentials(project_id,user_id)

    env = blueprint.get("environment", "dev")
    job_id = f"plan-{uuid.uuid4()}"

    supabase.table("jobs").insert({
        "job_id": job_id,
        "project_id": payload.get("project_id"),
        "job_type": "PLAN",
        "status": "RUNNING",
        "env": env
    }).execute()

    threading.Thread(
        target=run_plan_worker,
        args=(payload.get("project_id"), job_id, blueprint, credentials)
    ).start()
    return {"status": "accepted", "job_id": job_id}

@app.post("/cost")
def cost_infra(payload: dict):
    project_id = payload.get("project_id")

    if not project_id:
        raise HTTPException(status_code=400, detail="project_id required")
    
    run_id = payload.get("run_id")
    blueprint = payload.get("infra_blueprint") or payload
    if not run_id:
        raise HTTPException(status_code=400, detail="run_id required")

    credentials = get_project_credentials(project_id)

    job_id = f"cost-{uuid.uuid4()}"

    supabase.table("jobs").insert({
        "job_id": job_id,
        "project_id": payload.get("project_id"),
        "job_type": "COST",
        "status": "PENDING",
        "env": blueprint.get("environment"),
        "run_id": run_id
    }).execute()

    threading.Thread(
        target=run_cost_worker,
        args=(payload.get("project_id"), job_id, run_id, blueprint, credentials),
        daemon=True
    ).start()

    return {
        "status": "accepted",
        "job_id": job_id,
        "run_id": run_id
    }

@app.post("/apply")
def apply_infra(payload: dict, user=Depends(get_current_user)):
    project_id = payload.get("project_id")

    if not project_id:
        raise HTTPException(status_code=400, detail="project_id required")

    plan_job_id = payload.get("job_id")
    blueprint = payload.get("infra_blueprint")

    if not plan_job_id:
        raise HTTPException(status_code=400, detail="Plan Job ID required")

    user_id = user.id   

    credentials = get_project_credentials(project_id, user_id)

    job_id = f"apply-{uuid.uuid4()}"

    supabase.table("jobs").insert({
        "job_id": job_id,
        "project_id": project_id,
        "job_type": "APPLY",
        "status": "RUNNING",
        "env": blueprint.get("environment"),
        "plan_ref": plan_job_id
    }).execute()

    threading.Thread(
        target=run_apply_worker,
        args=(project_id, job_id, plan_job_id, blueprint, credentials),
        daemon=True
    ).start()

    return {"status": "accepted", "apply_job_id": job_id}

@app.get("/status/{job_id}")
def get_status(job_id: str):
    
    res = supabase.table("jobs") \
        .select("*") \
        .eq("job_id", job_id) \
        .single() \
        .execute()

    if not res.data:
        return {"job_id": job_id, "status": "NOT_FOUND"}

    job = res.data
    
    response = {
        "job_id": job["job_id"],
        "status": job["status"],
        "type": job["job_type"],
        "created_at": job["created_at"]
    }
    
    # If job completed and has result
    if job["status"] == "COMPLETED" and job.get("result"):

        # PLAN job → extract resources cleanly
        if job["job_type"] == "PLAN":
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
        elif job["job_type"] == "APPLY":
            response["outputs"] = job["result"].get("outputs", {})
            response["access"] = job["result"].get("access", [])

        elif job["job_type"] == "COST":
            response["cost_summary"] = job["result"].get("cost_summary", {})
            
    # If failed
    if job["status"] == "FAILED":
        response["error"] = job.get("error_message")

    return response