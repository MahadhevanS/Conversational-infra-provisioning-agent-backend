import uuid
import threading
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from backend.db import supabase
from backend.orchestrator import terraform_plan, terraform_apply, terraform_destroy, terraform_cost
from backend.lex import lex_webhook
from fastapi.concurrency import run_in_threadpool

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
                
def get_project_credentials(project_id):
    print(f"🔍 Looking up credentials for project_id: '{project_id}'")
    
    # 1. Look up the user_id from the project (Removed .single()!)
    project_res = supabase.table("projects") \
        .select("user_id") \
        .eq("project_id", project_id) \
        .execute()

    # Safely check if the list is empty
    if not project_res.data:
        print(f"❌ Could not find project {project_id} in database!")
        raise HTTPException(status_code=404, detail=f"Project not found for id: {project_id}")

    # Grab the first item from the list
    user_id = project_res.data[0]["user_id"]

    # 2. Get the credentials for that user (Removed .single()!)
    cred_res = supabase.table("aws_credentials") \
        .select("role_arn, external_id") \
        .eq("user_id", user_id) \
        .execute()

    if not cred_res.data:
        print(f"❌ Could not find AWS credentials for user {user_id}!")
        raise HTTPException(status_code=400, detail="AWS IAM Role not configured")

    print("✅ Credentials found successfully!")
    return cred_res.data[0]

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
            # 🔥 WE ADDED THIS PRINT STATEMENT
            print(f"❌ TERRAFORM ERROR: {result['error']}") 
            update_job_status(job_id, "FAILED", result["error"])
        else:
            update_job_status(job_id, "COMPLETED", result["structured_plan"])
    except Exception as e:
        # 🔥 WE ADDED THIS PRINT STATEMENT
        print(f"❌ WORKER CRASHED: {str(e)}") 
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
import os
from fastapi import HTTPException

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

    # 1️⃣ Create Supabase user inside a try-except block
    try:
        auth_response = supabase.auth.sign_up({
            "email": email,
            "password": password
        })
    except Exception as e:
        # Catch errors like "User already exists" or invalid passwords
        raise HTTPException(status_code=400, detail=str(e))

    user_id = auth_response.user.id

    # 2️⃣ OPTIONAL: Validate IAM role immediately
    # 2️⃣ OPTIONAL: Validate IAM role immediately
    try:
        import boto3

        # 🔥 THE ULTIMATE FIX: Strip spaces AND quotation marks!
        access_key = os.environ.get("AWS_ACCESS_KEY_ID", "").replace('"', '').replace("'", "").strip()
        secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "").replace('"', '').replace("'", "").strip()
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1").replace('"', '').replace("'", "").strip()

        # Debug print to prove exactly what FastAPI is seeing
        print(f"🕵️‍♂️ DEBUG ACCESS KEY: [{access_key}]")
        print(f"🕵️‍♂️ DEBUG SECRET KEY LENGTH: {len(secret_key)}")

        sts = boto3.client(
            "sts",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region
        )
        
        assume_params = {
            "RoleArn": role_arn,
            "RoleSessionName": "SignupValidation"
        }

        if external_id:
            assume_params["ExternalId"] = external_id

        sts.assume_role(**assume_params)

    except Exception as e:
        # Rollback user creation if IAM invalid
        try:
            # Note: This requires the Supabase client to be initialized with the SERVICE_ROLE_KEY
            supabase.auth.admin.delete_user(user_id)
        except Exception as rollback_error:
            print(f"⚠️ Warning: Could not delete user {user_id}. Error: {rollback_error}")
            
        raise HTTPException(
            status_code=400,
            detail=f"IAM Role validation failed: {str(e)}"
        )

    # 3️⃣ Store AWS credentials reference
    try:
        supabase.table("aws_credentials").insert({
            "user_id": user_id,
            "role_arn": role_arn,
            "external_id": external_id
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Database storage failed: {str(e)}")

    return {
        "message": "Signup successful! Please check your email to verify your account.",
        "user_id": user_id
    }

@app.post("/login")
def login(payload: dict):
    email = payload.get("email")
    password = payload.get("password")

    if not email or not password:
        raise HTTPException(status_code=400, detail="email and password required")

    try:
        # If credentials are wrong, this throws an Exception!
        response = supabase.auth.sign_in_with_password({
            "email": email,
            "password": password
        })
        
        # We also need to fetch the user's details for the frontend profile
        user_id = response.user.id
        
        # Fetch AWS credentials from your DB to send back to the frontend
        cred_res = supabase.table("aws_credentials") \
            .select("role_arn, external_id") \
            .eq("user_id", user_id) \
            .execute()
            
        aws_creds = cred_res.data[0] if cred_res.data else {}

        return {
            "access_token": response.session.access_token,
            "refresh_token": response.session.refresh_token,
            "user_id": user_id,
            "email": email,
            "full_name": response.user.user_metadata.get("full_name", ""),
            "aws_account_id": "", # You can parse this from ARN if needed
            "aws_region": "", 
            "role_arn": aws_creds.get("role_arn", ""),
            "external_id": aws_creds.get("external_id", "")
        }
        
    except Exception as e:
        # Safely catch the error and return it to the UI
        raise HTTPException(status_code=401, detail=str(e))
    
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
async def handle_lex(request: Request):
    # 1. Catch the raw data from AWS Lex
    event = await request.json()
    
    intent_name = event.get("sessionState", {}).get("intent", {}).get("name", "Unknown")
    print(f"🚀 WEBHOOK HIT! Intent: {intent_name}")
    
    # 🔥 THE FIX: Run the synchronous Lex logic in a separate thread.
    # This prevents the local HTTP calls from deadlocking FastAPI!
    return await run_in_threadpool(lex_webhook, event)

@app.post("/plan")
def plan_infra(payload: dict): # Notice we removed Depends(get_current_user)!
    project_id = payload.get("project_id")
    blueprint = payload.get("infra_blueprint") or payload
    
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id required")
    
    # Securely get credentials using ONLY the project_id
    credentials = get_project_credentials(project_id)

    env = blueprint.get("environment", "dev")
    job_id = f"plan-{uuid.uuid4()}"

    supabase.table("jobs").insert({
        "job_id": job_id,
        "project_id": project_id,
        "job_type": "PLAN",
        "status": "RUNNING",
        "env": env
    }).execute()

    threading.Thread(
        target=run_plan_worker,
        args=(project_id, job_id, blueprint, credentials)
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
def apply_infra(payload: dict): # Removed Depends(get_current_user)!
    project_id = payload.get("project_id")

    if not project_id:
        raise HTTPException(status_code=400, detail="project_id required")

    plan_job_id = payload.get("job_id")
    blueprint = payload.get("infra_blueprint")

    if not plan_job_id:
        raise HTTPException(status_code=400, detail="Plan Job ID required")

    credentials = get_project_credentials(project_id)

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

@app.get("/projects")
def get_all_projects(user=Depends(get_current_user)):
    res = supabase.table("projects") \
        .select("project_id, project_name, created_at") \
        .eq("user_id", user.id) \
        .order("created_at", desc=True) \
        .execute()
    return {"projects": res.data}

@app.get("/chats/{project_id}")
def get_chat_history(project_id: str, user=Depends(get_current_user)):
    # 1. Fetch Chat Messages
    messages_res = supabase.table("chat_messages") \
        .select("message_id, sender, message_text, created_at, job_id") \
        .eq("project_id", project_id) \
        .order("created_at", desc=False) \
        .execute()
    
    messages = messages_res.data

    # 2. Fetch all Jobs for the project
    jobs_res = supabase.table("jobs") \
        .select("*") \
        .eq("project_id", project_id) \
        .execute()
    
    jobs_list = jobs_res.data
    jobs_map = {job["job_id"]: job for job in jobs_list}

    # 🔥 THE COST FIX: Find all Cost jobs and link them to their parent Plan
    cost_map = {}
    for job in jobs_list:
        if job["job_type"] == "COST" and job["status"] == "COMPLETED" and job.get("run_id"):
            cost_map[job["run_id"]] = job.get("result", {}).get("cost_summary")

    # 3. Merge Job data into Messages
    for msg in messages:
        if msg.get("job_id") and msg["job_id"] in jobs_map:
            msg["job_details"] = jobs_map[msg["job_id"]]
            
            # 🔥 Inject the cost summary directly into the Plan details!
            if msg["job_details"]["job_type"] == "PLAN" and msg["job_id"] in cost_map:
                msg["job_details"]["cost_summary"] = cost_map[msg["job_id"]]

    return {"messages": messages}

@app.post("/chats")
def save_chat_message(payload: dict, user=Depends(get_current_user)):
    project_id = payload.get("project_id")
    sender = payload.get("sender") # Expects 'USER' or 'BOT'
    message_text = payload.get("message_text")
    
    # 🔥 1. Catch the job_id sent by React
    job_id = payload.get("job_id") 
    
    if not project_id or not sender or not message_text:
        raise HTTPException(status_code=400, detail="Missing chat data")
        
    # 🔥 2. Build the insert payload dynamically
    insert_data = {
        "project_id": project_id,
        "sender": sender.upper(),
        "message_text": message_text
    }
    
    # 🔥 3. If this message is attached to a Terraform job, link it!
    if job_id:
        insert_data["job_id"] = job_id
        
    res = supabase.table("chat_messages").insert(insert_data).execute()
    
    return {"status": "success"}

@app.post("/jobs/{job_id}/discard")
def discard_job(job_id: str):
    # Update the job status in the database permanently
    supabase.table("jobs").update({"status": "DISCARDED"}).eq("job_id", job_id).execute()
    return {"status": "success"}