import uuid
import threading
import os
import time
import boto3
from typing import Optional, Dict, Any
import traceback

from pydantic import BaseModel
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi import FastAPI, HTTPException, Depends, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool

from backend.db import supabase, make_auth_client
from backend.orchestrator import terraform_plan, terraform_apply, terraform_destroy, terraform_cost
from backend.lex import lex_webhook
from backend.ai_analyser import analyse_failure

from backend.email_service import send_invitation_email, send_destroy_approval_email

app = FastAPI()

# FIX A: Restore allow_origins=["*"] so deployed environments (not just localhost)
# can reach the API.  Note: allow_credentials must be False when origins is wildcard.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- DB HELPERS ---
security = HTTPBearer()


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    last_error = None

    for attempt in range(3):
        try:
            response = supabase.auth.get_user(token)

            if response.user is None:
                raise HTTPException(status_code=401, detail="Invalid token")

            return response.user
        except HTTPException:
            raise
        except Exception as e:
            error_msg = str(e).lower()
            # 🔥 FIX: If the token is expired, instantly throw a 401 so React logs them out!
            if "expired" in error_msg or "invalid jwt" in error_msg:
                raise HTTPException(status_code=401, detail="Session expired. Please log in again.")
            
            last_error = e
            print(f"⚠️ get_current_user attempt {attempt + 1} failed: {e}")
            time.sleep(0.3)

    raise HTTPException(
        status_code=503,
        detail=f"Auth service temporarily unavailable: {last_error}"
    )


def get_project_credentials(project_id):
    print(f"🔍 Looking up credentials for project_id: '{project_id}'")

    project_res = (
        supabase.table("projects")
        .select("user_id")
        .eq("project_id", project_id)
        .execute()
    )

    if not project_res.data:
        print(f"❌ Could not find project {project_id} in database!")
        raise HTTPException(status_code=404, detail=f"Project not found for id: {project_id}")

    user_id = project_res.data[0]["user_id"]

    cred_res = (
        supabase.table("aws_credentials")
        .select("role_arn, external_id")
        .eq("user_id", user_id)
        .execute()
    )

    if not cred_res.data:
        print(f"❌ Could not find AWS credentials for user {user_id}!")
        raise HTTPException(status_code=400, detail="AWS IAM Role not configured")

    print("✅ Credentials found successfully!")
    return cred_res.data[0]


# --- NOTIFICATION HELPERS ---

def get_project_user_id(project_id):
    try:
        print(f"🔎 Fetching user_id for project_id={project_id}")
        res = (
            supabase.table("projects")
            .select("user_id")
            .eq("project_id", project_id)
            .execute()
        )
        if not res.data:
            print(f"⚠️ No project found for project_id={project_id}")
            return None
        user_id = res.data[0].get("user_id")
        print(f"✅ Found user_id={user_id} for project_id={project_id}")
        return user_id
    except Exception as e:
        print(f"❌ Failed to fetch user_id for project {project_id}: {e}")
        return None


def build_notification_key(title, metadata=None):
    metadata = metadata or {}
    safe_title = str(title).strip().upper().replace(" ", "_")
    return "|".join([
        safe_title,
        str(metadata.get("job_type", "")),
        str(metadata.get("job_id", "")),
        str(metadata.get("run_id", "")),
        str(metadata.get("plan_job_id", "")),
        str(metadata.get("project_id", "")),
    ])


def create_notification_for_user(project_id,user_id, title, message, type="INFO", metadata=None):
    try:
        if not user_id:
            print("⚠️ Skipping notification because user_id is missing")
            return

        metadata = metadata or {}
        notification_key = build_notification_key(title, metadata)

        existing = (
            supabase.table("notifications")
            .select("id, is_deleted")
            .eq("user_id", str(user_id))
            .eq("notification_key", notification_key)
            .execute()
        )

        if existing.data:
            print(f"⚠️ Notification already exists for key={notification_key} -> skipping")
            return

        insert_payload = {
            "user_id": str(user_id),
            "project_id": str(project_id),
            "title": str(title),
            "message": str(message),
            "type": str(type),
            "is_read": False,
            "is_deleted": False,
            "notification_key": notification_key,
            "metadata": metadata,
        }

        print(f"🔔 Inserting notification: {insert_payload}")
        result = supabase.table("notifications").insert(insert_payload).execute()
        print(f"✅ Notification insert result: {result.data}")
    except Exception as e:
        print(f"❌ Notification creation failed: {e}")



# --- WORKERS WITH PERSISTENCE ---

def update_job_status(job_id, status, result=None):
    update_data = {"status": status}
    if status == "FAILED":
        update_data["error_message"] = str(result)
        update_data["result"] = None
    elif result is not None:
        update_data["result"] = result
    supabase.table("jobs").update(update_data).eq("job_id", job_id).execute()


def _fetch_log_chunks(job_id: str) -> list:
    """Fetch log_chunks from the jobs row — used by the AI analysis thread."""
    try:
        res = (
            supabase.table("jobs")
            .select("log_chunks")
            .eq("job_id", job_id)
            .execute()
        )
        if res.data:
            return res.data[0].get("log_chunks") or []
    except Exception as e:
        print(f"⚠️ Could not fetch log_chunks for {job_id}: {e}")
    return []


def _fire_ai_analysis(project_id: str, job_id: str, job_type: str):
    """
    Spawn a daemon thread that:
      1. Calls OpenAI to analyse the failure logs.
      2. Posts the result as a BOT chat message in the project.
      3. Attaches the result to the existing failure notification's metadata.

    Fire-and-forget — never blocks the worker that calls it.
    """
    def _run():
        print(f"🤖 AI analysis started for {job_type} job {job_id}")

        log_chunks = _fetch_log_chunks(job_id)
        analysis = analyse_failure(job_id=job_id, job_type=job_type, log_chunks=log_chunks)

        root_cause = analysis.get("root_cause", "Unknown error")
        fix_steps = analysis.get("fix_steps", [])
        category = analysis.get("category", "unknown")

        structured = {
            "root_cause": root_cause,
            "fix_steps": fix_steps,
            "category": category,
        }

        # ── 1. Persist structured analysis on the job row ─────────────────
        # Primary data source: /status returns it, loadChatHistory reads it,
        # DeploymentFailureView renders it. No text parsing needed anywhere.
        try:
            supabase.table("jobs").update({
                "ai_analysis": structured
            }).eq("job_id", job_id).execute()
            print(f"✅ AI analysis saved to job row for {job_id}")
        except Exception as e:
            print(f"❌ Failed to save AI analysis to job row: {e}")

        # ── 2. Attach analysis to the failure notification metadata ────────
        try:
            notif_res = (
                supabase.table("notifications")
                .select("id, metadata")
                .eq("metadata->>job_id", job_id)
                .eq("type", "ERROR")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if notif_res.data:
                existing_meta = notif_res.data[0].get("metadata") or {}
                existing_meta["ai_analysis"] = structured
                supabase.table("notifications").update({
                    "metadata": existing_meta
                }).eq("id", notif_res.data[0]["id"]).execute()
                print(f"✅ AI analysis attached to notification for job {job_id}")
        except Exception as e:
            print(f"❌ Failed to attach AI analysis to notification: {e}")

    threading.Thread(target=_run, daemon=True).start()

# ===============================
# PLAN WORKER (UNCHANGED LOG ROOT)
# ===============================

def run_plan_worker(project_id, job_id, blueprint, credentials=None, triggered_by=None):
    try:
        update_job_status(job_id, "RUNNING")

        # 🔥 PLAN is the ROOT LOG STREAM
        result = terraform_plan(project_id, blueprint, job_id, credentials=credentials)

        if "error" in result:
            update_job_status(job_id, "FAILED", result["error"])

            create_notification_for_user(
                project_id=project_id,
                user_id=triggered_by,
                title="PLAN failed",
                message=f"PLAN job failed for project {project_id}",
                type="ERROR",
                metadata={"job_id": job_id, "job_type": "PLAN"},
            )

            _fire_ai_analysis(project_id, job_id, "PLAN")

        else:
            update_job_status(job_id, "COMPLETED", result["structured_plan"])

            create_notification_for_user(
                project_id=project_id,
                user_id=triggered_by,
                title="PLAN completed",
                message=f"PLAN job completed for project {project_id}",
                type="SUCCESS",
                metadata={"job_id": job_id, "job_type": "PLAN"},
            )

    except Exception as e:
        update_job_status(job_id, "FAILED", str(e))

        create_notification_for_user(
            project_id=project_id,
            user_id=triggered_by,
            title="PLAN failed",
            message=f"PLAN job failed for project {project_id}",
            type="ERROR",
            metadata={"job_id": job_id, "job_type": "PLAN"},
        )

        _fire_ai_analysis(project_id, job_id, "PLAN")


# ===============================
# APPLY WORKER (FIXED)
# ===============================

def run_apply_worker(project_id, job_id, plan_job_id, blueprint, credentials=None, triggered_by=None):
    """
    job_id      = APPLY job (status only)
    plan_job_id = ROOT LOG STREAM (VERY IMPORTANT)
    """
    try:
        update_job_status(job_id, "RUNNING")

        # 🔥 CRITICAL FIX: store logs in PLAN JOB ID
        result = terraform_apply(
            project_id,
            plan_job_id,
            blueprint,
            credentials=credentials,
            apply_job_id=plan_job_id,   # ✅ FIXED
        )

        if "error" in result:
            update_job_status(job_id, "FAILED", result)

            create_notification_for_user(
                project_id=project_id,
                user_id=triggered_by,
                title="Deployment failed",
                message=f"Infrastructure deployment failed for project {project_id}",
                type="ERROR",
                metadata={
                    "job_id": plan_job_id,   # ✅ FIXED
                    "job_type": "APPLY"
                },
            )

            _fire_ai_analysis(project_id, plan_job_id, "APPLY")

        else:
            update_job_status(job_id, "COMPLETED", result)

            create_notification_for_user(
                project_id=project_id,
                user_id=triggered_by,
                title="Deployment completed",
                message=f"Infrastructure deployed for project {project_id}",
                type="SUCCESS",
                metadata={
                    "job_id": plan_job_id,   # ✅ FIXED
                    "job_type": "APPLY"
                },
            )

    except Exception as e:
        update_job_status(job_id, "FAILED", str(e))

        create_notification_for_user(
            project_id=project_id,
            user_id=triggered_by,
            title="Deployment failed",
            message=f"Infrastructure deployment failed for project {project_id}",
            type="ERROR",
            metadata={
                "job_id": plan_job_id,   # ✅ FIXED
                "job_type": "APPLY"
            },
        )

        _fire_ai_analysis(project_id, plan_job_id, "APPLY")


# ===============================
# COST WORKER (UNCHANGED)
# ===============================

def run_cost_worker(project_id, job_id, run_id, blueprint, credentials=None, triggered_by=None):
    try:
        update_job_status(job_id, "RUNNING")

        result = terraform_cost(project_id, run_id, blueprint, credentials=credentials)

        if "error" in result:
            update_job_status(job_id, "FAILED", result)

            create_notification_for_user(
                project_id=project_id,
                user_id=triggered_by,
                title="Cost estimation failed",
                message=f"Cost estimation failed for project {project_id}",
                type="ERROR",
                metadata={"job_id": job_id, "job_type": "COST"},
            )

        else:
            update_job_status(job_id, "COMPLETED", result)

            create_notification_for_user(
                project_id=project_id,
                user_id=triggered_by,
                title="Cost estimation ready",
                message=f"Cost estimation completed for project {project_id}",
                type="SUCCESS",
                metadata={"job_id": job_id, "job_type": "COST"},
            )

    except Exception as e:
        update_job_status(job_id, "FAILED", str(e))

        create_notification_for_user(
            project_id=project_id,
            user_id=triggered_by,
            title="Cost estimation failed",
            message=f"Cost estimation crashed for project {project_id}.",
            type="ERROR",
            metadata={"job_id": job_id, "job_type": "COST"},
        )


# ===============================
# DESTROY WORKER (FIXED)
# ===============================

def run_destroy_worker(project_id, job_id, blueprint, credentials=None, triggered_by=None, plan_job_id=None):
    """
    plan_job_id = ROOT LOG STREAM (if destroy is chained)
    """

    try:
        update_job_status(job_id, "RUNNING")

        # 🔥 Use PLAN LOG STREAM if available
        log_job_id = plan_job_id or job_id

        result = terraform_destroy(
            project_id,
            blueprint,
            credentials=credentials,
            job_id=log_job_id,   # ✅ FIXED
        )

        if isinstance(result, dict) and "error" in result:
            update_job_status(job_id, "FAILED", result.get("error"))

            create_notification_for_user(
                project_id=project_id,
                user_id=triggered_by,
                title="Destroy failed",
                message=f"Infrastructure destruction failed for project {project_id}",
                type="ERROR",
                metadata={
                    "job_id": log_job_id,   # ✅ FIXED
                    "job_type": "DESTROY"
                },
            )

            _fire_ai_analysis(project_id, log_job_id, "DESTROY")

        else:
            update_job_status(job_id, "COMPLETED", result)

            create_notification_for_user(
                project_id=project_id,
                user_id=triggered_by,
                title="Destroy completed",
                message=f"Infrastructure destroyed for project {project_id}",
                type="SUCCESS",
                metadata={
                    "job_id": log_job_id,   # ✅ FIXED
                    "job_type": "DESTROY"
                },
            )

    except Exception as e:
        update_job_status(job_id, "FAILED", str(e))

        create_notification_for_user(
            project_id=project_id,
            user_id=triggered_by,
            title="Destroy failed",
            message=f"Infrastructure destruction crashed for project {project_id}.",
            type="ERROR",
            metadata={
                "job_id": log_job_id,   # ✅ FIXED
                "job_type": "DESTROY"
            },
        )

        _fire_ai_analysis(project_id, log_job_id, "DESTROY")

def post_bot_chat_message(project_id: str, message_text: str, job_id: str = None):
    """Save a CloudCrafter bot message to the project chat feed."""
    try:
        insert_data = {
            "project_id":   project_id,
            "sender":       "BOT",
            "sender_name":  "CloudCrafter",
            "sender_role":  "bot",
            "message_text": message_text,
        }
        if job_id:
            insert_data["job_id"] = job_id
        supabase.table("chat_messages").insert(insert_data).execute()
    except Exception as e:
        print(f"⚠️ Failed to post bot chat message: {e}")

# --- NOTIFICATION MODELS ---

class NotificationCreate(BaseModel):
    user_id: str
    title: str
    message: str
    type: Optional[str] = "INFO"
    metadata: Optional[Dict[str, Any]] = None


# --- ROUTES ---

@app.post("/notifications")
def create_notification(payload: NotificationCreate, user=Depends(get_current_user)):
    try:
        if str(user.id) != str(payload.user_id):
            raise HTTPException(status_code=403, detail="Unauthorized access")

        metadata = payload.metadata or {}
        notification_key = build_notification_key(payload.title, metadata)

        insert_payload = {
            "user_id": payload.user_id,
            "title": payload.title,
            "message": payload.message,
            "type": payload.type or "INFO",
            "is_read": False,
            "notification_key": notification_key,
            "metadata": metadata,
        }

        res = (
            supabase.table("notifications")
            .upsert(insert_payload, on_conflict="user_id,notification_key")
            .execute()
        )

        return {"success": True, "notification": res.data[0] if res.data else None}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create notification: {str(e)}")


@app.get("/notifications")
def get_notifications(user=Depends(get_current_user)):
    try:
        res = (
            supabase.table("notifications")
            .select("id, user_id, project_id, job_id, title, message, type, is_read, is_deleted, created_at, metadata, notification_key")
            .eq("user_id", str(user.id))
            .eq("is_deleted", False)
            .order("created_at", desc=True)
            .execute()
        )

        return {"success": True, "notifications": res.data or []}

    except Exception as e:
        print(f"❌ /notifications failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch notifications")

@app.get("/notifications/unread-count")
def get_unread_count(user=Depends(get_current_user)):
    try:
        res = (
            supabase.table("notifications")
            .select("id")
            .eq("user_id", str(user.id))
            .eq("is_read", False)
            .eq("is_deleted", False)
            .execute()
        )

        return {"success": True, "unread_count": len(res.data or [])}

    except Exception as e:
        print(f"❌ unread-count failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch unread count")


@app.put("/notifications/read-all")
def mark_all_notifications_read(user=Depends(get_current_user)):
    supabase.table("notifications")\
        .update({"is_read": True})\
        .eq("user_id", str(user.id))\
        .eq("is_deleted", False)\
        .execute()

    return {"success": True}

@app.delete("/notifications/clear-all")
def clear_all_notifications(user=Depends(get_current_user)):
    supabase.table("notifications")\
        .update({"is_deleted": True})\
        .eq("user_id", str(user.id))\
        .execute()

    return {"success": True}

@app.delete("/notifications/{notification_id}")
def delete_notification(notification_id: str, user=Depends(get_current_user)):
    try:
        existing = (
            supabase.table("notifications")
            .select("id, user_id, is_deleted")
            .eq("id", notification_id)
            .execute()
        )

        if not existing.data:
            return {"success": True}

        if str(existing.data[0]["user_id"]) != str(user.id):
            raise HTTPException(status_code=403, detail="Unauthorized access")

        supabase.table("notifications").update({"is_deleted": True}).eq("id", notification_id).execute()
        print(f"🗑️ Soft-deleted notification {notification_id}")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ delete notification failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to delete notification: {str(e)}")


@app.put("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: str, user=Depends(get_current_user)):
    try:
        existing = (
            supabase.table("notifications")
            .select("id, user_id, is_deleted")
            .eq("id", notification_id)
            .execute()
        )

        if not existing.data:
            raise HTTPException(status_code=404, detail="Notification not found")

        notification = existing.data[0]

        if str(notification["user_id"]) != str(user.id):
            raise HTTPException(status_code=403, detail="Unauthorized access")

        if notification.get("is_deleted"):
            return {"success": True, "notification": None}

        res = (
            supabase.table("notifications")
            .update({"is_read": True})
            .eq("id", notification_id)
            .execute()
        )

        return {"success": True, "notification": res.data[0] if res.data else None}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ /notifications/{{id}}/read failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to mark notification as read: {str(e)}")


@app.post("/signup")
def signup(payload: dict):
    email       = payload.get("email")
    password    = payload.get("password")
    role_arn    = payload.get("role_arn")
    external_id = payload.get("external_id")
    full_name   = payload.get("full_name", "")

    requested_role = payload.get("role", "admin")
    if requested_role not in ["admin", "cloud_architect"]:
        requested_role = "admin"

    if not email or not password or not role_arn:
        raise HTTPException(status_code=400, detail="email, password, and role_arn required")

    # 🔥 One-shot auth client — never touches the shared service-role client
    auth_client = make_auth_client()

    try:
        auth_response = auth_client.auth.sign_up({
            "email": email,
            "password": password,
            "options": {"data": {"full_name": full_name}},
        })
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not auth_response.user:
        raise HTTPException(status_code=400, detail="Signup failed. Please try again.")

    user_id = auth_response.user.id

    # Validate the IAM role ARN against AWS STS
    try:
        access_key = os.environ.get("AWS_ACCESS_KEY_ID", "").replace('"', "").replace("'", "").strip()
        secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "").replace('"', "").replace("'", "").strip()
        region     = os.environ.get("AWS_DEFAULT_REGION", "us-east-1").replace('"', "").replace("'", "").strip()

        sts = boto3.client(
            "sts",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region
        )
        assume_params = {"RoleArn": role_arn, "RoleSessionName": "SignupValidation"}
        if external_id:
            assume_params["ExternalId"] = external_id
        sts.assume_role(**assume_params)

    except Exception as e:
        # Roll back the created auth user before raising
        try:
            supabase.auth.admin.delete_user(user_id)
        except Exception as rollback_err:
            print(f"⚠️ Could not delete user {user_id} during rollback: {rollback_err}")
        raise HTTPException(status_code=400, detail=f"IAM Role validation failed: {str(e)}")

    # Persist credentials and profile using the service-role client
    try:
        supabase.table("aws_credentials").insert({
            "user_id":     str(user_id),
            "role_arn":    role_arn,
            "external_id": external_id,
        }).execute()

        supabase.table("user_profiles").insert({
            "user_id":   str(user_id),
            "email":     email,
            "full_name": full_name,
            "role":      requested_role,
        }).execute()

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Database storage failed: {str(e)}")

    return {
        "message": "Signup successful! Please check your email to verify your account.",
        "user_id": str(user_id),
    }

@app.post("/login")
def login(payload: dict):
    email    = payload.get("email")
    password = payload.get("password")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password required")

    # 🔥 One-shot auth client — sign_in never touches the shared service-role client
    auth_client = make_auth_client()

    try:
        auth_response = auth_client.auth.sign_in_with_password({
            "email": email,
            "password": password,
        })
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not auth_response.user or not auth_response.session:
        raise HTTPException(status_code=400, detail="Login failed. Please check your credentials.")

    user_id = str(auth_response.user.id)

    # ── Role extraction ────────────────────────────────────────────────────
    user_role = ""
    try:
        profile_res = supabase.table("user_profiles")\
            .select("role")\
            .eq("user_id", user_id)\
            .execute()
        if profile_res.data and len(profile_res.data) > 0:
            user_role = profile_res.data[0].get("role", "")
    except Exception as e:
        print(f"⚠️ Profile extraction failed: {e}")

    # ── AWS credentials extraction ─────────────────────────────────────────
    aws_account_id = ""
    role_arn       = ""
    external_id    = ""

    try:
        aws_res = supabase.table("aws_credentials")\
            .select("role_arn, external_id")\
            .eq("user_id", user_id)\
            .execute()
        if aws_res.data and len(aws_res.data) > 0:
            first = aws_res.data[0]
            role_arn    = first.get("role_arn", "")
            external_id = first.get("external_id", "")
            if role_arn and ":" in role_arn:
                parts = role_arn.split(":")
                if len(parts) >= 5:
                    aws_account_id = parts[4]
    except Exception as e:
        print(f"⚠️ AWS credentials extraction failed: {e}")

    # ── Return payload ─────────────────────────────────────────────────────
    try:
        return {
            "access_token":  auth_response.session.access_token,
            "user_id":       user_id,
            "email":         auth_response.user.email,
            "full_name":     auth_response.user.user_metadata.get("full_name", "")
                             if auth_response.user.user_metadata else "",
            "role":          user_role,
            "aws_account_id": aws_account_id,
            "aws_region":    "us-east-1",
            "role_arn":      role_arn,
            "external_id":   external_id,
        }
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to format login response.")


@app.post("/projects/create")  # 🔥 NOTICE THE NEW URL!
async def create_project_v2(request: Request, payload: dict, background_tasks: BackgroundTasks):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    
    token = auth_header.replace("Bearer ", "").strip()
    
    try:
        user_res = supabase.auth.get_user(token)
        admin_id = user_res.user.id
        admin_email = user_res.user.email
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid session")

    project_name = payload.get("project_name")
    environment = payload.get("environment", "development")
    invite_emails = payload.get("invite_emails") or []

    print(f"📦 Project creation started: name={project_name}, emails={invite_emails}")  # ADD

    if not project_name:
        raise HTTPException(status_code=400, detail="Project name is required")

    # 1. CREATE PROJECT
    try:
        proj_res = supabase.table("projects").insert({
            "project_name": project_name,
            "environment": environment,
            "user_id": admin_id 
        }).execute()
        
        # 100% BULLETPROOF EXTRACTION
        inserted_data = proj_res.data
        new_project = {}
        
        # Loop through the list to guarantee we find the dictionary
        for item in inserted_data:
            if isinstance(item, dict):
                new_project = item
                break
                
        project_id = new_project.get("project_id") or new_project.get("id")
        
        if not project_id:
            raise ValueError("Could not extract project ID from database response.")
        print(f"✅ Project created: project_id={project_id}")  # ADD
    except Exception as e:
        print(f"🚨 Failed to create project: {e}")
        raise HTTPException(status_code=500, detail="Failed to create project")

    print(f"📧 invite_emails received: {invite_emails}")  # ADD

    # 2. HANDLE INVITATIONS
    successful_invites = []
    
    if invite_emails:
        for email in invite_emails:
            email = email.strip()
            if not email: continue
            
            print(f"🔄 Processing invite for: {email}")  # ADD

            try:
                invite_res = supabase.table("project_invitations").insert({
                    "project_id": project_id,
                    "email": email,
                    "invited_by": admin_id,
                    "status": "pending"
                }).execute()
                
                print(f"📬 invite_res.data = {invite_res.data}")  # ADD — critical

                # Safe extraction for invitations
                invite_data = invite_res.data
                invite_token = None
                for item in invite_data:
                    if isinstance(item, dict):
                        invite_token = item.get("token")
                        break
                print(f"🔑 invite_token extracted: {invite_token}")  # ADD

                if invite_token:
                    print(f"📤 Queuing background email to {email}")  # ADD
                    background_tasks.add_task(
                        send_invitation_email, email, project_name, admin_email, invite_token
                    )
                    successful_invites.append(email)
                    
            except Exception as e:
                print(f"⚠️ Invitation failed for {email}: {e}")

    print(f"🏁 Done. successful_invites={successful_invites}")  # ADD
    # 3. RETURN DATA
    return {
        "project_id": project_id,
        "project_name": new_project.get("project_name", project_name),
        "environment": new_project.get("environment", environment),
        "created_at": new_project.get("created_at"),
        "invite_status": f"Successfully created. Emailed {len(successful_invites)} invites." if successful_invites else "Successfully created."
    }

@app.post("/projects/{project_id}/invite")
async def invite_architect(project_id: str, request: Request, payload: dict, background_tasks: BackgroundTasks):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    
    token = auth_header.split(" ").strip()
    
    try:
        user_res = supabase.auth.get_user(token)
        admin_id = user_res.user.id
        admin_email = user_res.user.email
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid session")

    # SECURITY CHECK
    proj_res = supabase.table("projects").select("user_id, project_name").eq("project_id", project_id).execute()
    if not proj_res.data or len(proj_res.data) == 0:
        raise HTTPException(status_code=404, detail="Project not found.")
    
    if proj_res.data.get("user_id") != admin_id:
        raise HTTPException(status_code=403, detail="Only the project Admin can invite members.")

    project_name = proj_res.data.get("project_name")
    invite_email = payload.get("email", "").strip()
    
    if not invite_email:
        raise HTTPException(status_code=400, detail="Email is required.")

    try:
        # Check if they are already in the project
        profile_res = supabase.table("user_profiles").select("user_id").eq("email", invite_email).execute()
        
        if hasattr(profile_res, 'data') and len(profile_res.data) > 0:
            invited_user_id = profile_res.data.get("user_id")
            existing_member = supabase.table("project_members").select("id").eq("project_id", project_id).eq("user_id", invited_user_id).execute()
            
            if hasattr(existing_member, 'data') and len(existing_member.data) > 0:
                return {"message": f"{invite_email} is already a member of this project."}

        # Create the Pending Invitation
        invite_res = supabase.table("project_invitations").insert({
            "project_id": project_id,
            "email": invite_email,
            "invited_by": admin_id,
            "status": "pending"
        }).execute()
        
        invite_token = invite_res.data.get("token")

        # QUEUE THE EMAIL!
        background_tasks.add_task(
            send_invitation_email, 
            invite_email, 
            project_name, 
            admin_email, 
            invite_token
        )
        
        return {"message": f"Successfully sent an invitation email to {invite_email}."}

    except HTTPException:
        raise
    except Exception as e:
        print(f"🚨 Invite Error: {e}")
        raise HTTPException(status_code=500, detail="Failed to send invitation.")

@app.post("/invitations/{token}/accept")
async def accept_invitation(token: str, request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or "Bearer " not in auth_header:
        raise HTTPException(status_code=401, detail="Please log in to accept this invite.")
    
    access_token = auth_header.replace("Bearer ", "").strip()
    
    try:
        user_res = supabase.auth.get_user(access_token)
        architect_id = user_res.user.id
        architect_email = user_res.user.email
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid session. Please log in again.")

    try:
        # 1. Fetch the invitation
        invite_res = supabase.table("project_invitations").select("*").eq("token", token).eq("status", "pending").execute()
        
        # 2. 100% BULLETPROOF DICTIONARY EXTRACTION
        invitation = {}
        raw_data = invite_res.data
        
        if not raw_data or str(raw_data) == "[]":
             raise HTTPException(status_code=404, detail="Invitation is invalid, expired, or already accepted.")

        # Search through whatever Supabase handed back to find the actual dictionary
        if isinstance(raw_data, dict):
            invitation = raw_data
        elif isinstance(raw_data, list):
            for item in raw_data:
                if isinstance(item, dict):
                    invitation = item
                    break
                # Handle double-nested lists just in case
                elif isinstance(item, list) and len(item) > 0 and isinstance(item, dict):
                    invitation = item
                    break

        if not invitation:
            raise ValueError(f"Could not parse database response. Raw data: {raw_data}")

        # 3. Safe extraction without using .get()
        inv_email = invitation["email"] if "email" in invitation else ""
        inv_project_id = invitation["project_id"] if "project_id" in invitation else ""
        inv_invited_by = invitation["invited_by"] if "invited_by" in invitation else ""

        if architect_email.lower() != inv_email.lower():
            raise HTTPException(status_code=403, detail="This invite was sent to a different email address.")

        # 4. Move them into the actual project
        supabase.table("project_members").insert({
            "project_id": inv_project_id,
            "user_id": architect_id,
            "added_by": inv_invited_by
        }).execute()

        # 5. Mark the invitation as accepted
        supabase.table("project_invitations").update({"status": "accepted"}).eq("token", token).execute()

        return {"message": "Successfully joined the project!"}

    except HTTPException:
        raise
    except Exception as e:
        print(f"🚨 Accept Error: {e}")
        raise HTTPException(status_code=500, detail="Failed to process invitation.")
    
@app.post("/lex-webhook")
async def handle_lex(request: Request):
    event = await request.json()
    intent_name = event.get("sessionState", {}).get("intent", {}).get("name", "Unknown")
    print(f"🚀 WEBHOOK HIT! Intent: {intent_name}")
    return await run_in_threadpool(lex_webhook, event)


@app.post("/plan")
def plan_infra(payload: dict):
    project_id = payload.get("project_id")
    blueprint = payload.get("infra_blueprint") or payload
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id required")

    credentials = get_project_credentials(project_id)
    env = blueprint.get("environment", "dev")
    job_id = f"plan-{uuid.uuid4()}"

    # FIX E: initialise log_chunks so LogPanel never gets null on first poll
    supabase.table("jobs").insert({
        "job_id": job_id,
        "project_id": project_id,
        "job_type": "PLAN",
        "status": "RUNNING",
        "env": env,
        "log_chunks": [],
    }).execute()

    create_notification_for_user(
        project_id=project_id,
        user_id=payload.get("user_id"),
        title="PLAN started",
        message=f"PLAN job has started in {env}.",
        type="INFO",
        metadata={"job_id": job_id, "job_type": "PLAN"},
    )

    threading.Thread(
        target=run_plan_worker,
        args=(project_id, job_id, blueprint, credentials),
        kwargs={"triggered_by": payload.get("user_id")},
        daemon=True
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
        "project_id": project_id,
        "job_type": "COST",
        "status": "PENDING",
        "env": blueprint.get("environment"),
        "run_id": run_id,
        "log_chunks": [],
    }).execute()

    create_notification_for_user(
        project_id=project_id,
        user_id=payload.get("user_id"),
        title="Cost estimation started",
        message=f"Cost estimation has started for project {project_id}.",
        type="INFO",
        metadata={"job_id": job_id, "job_type": "COST", "run_id": run_id},
    )
    
    threading.Thread(
        target=run_cost_worker,
        args=(project_id, job_id, run_id, blueprint, credentials),
        kwargs={"triggered_by": payload.get("user_id")},
        daemon=True,
    ).start()

    return {"status": "accepted", "job_id": job_id, "run_id": run_id}


@app.post("/apply")
def apply_infra(payload: dict):
    project_id = payload.get("project_id")
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id required")

    plan_job_id = payload.get("job_id")
    blueprint = payload.get("infra_blueprint")   # intentionally optional

    if not plan_job_id:
        raise HTTPException(status_code=400, detail="Plan Job ID required")

    # FIX G: blueprint is optional — the workspace already exists from the plan step.
    # Removing the hard 400 that broke applies when the frontend omits the blueprint.

    credentials = get_project_credentials(project_id)
    job_id = f"apply-{uuid.uuid4()}"

    supabase.table("jobs").insert({
        "job_id": job_id,
        "project_id": project_id,
        "job_type": "APPLY",
        "status": "RUNNING",
        "env": blueprint.get("environment") if blueprint else None,
        "plan_ref": plan_job_id,
        "log_chunks": [],
    }).execute()

    create_notification_for_user(
        project_id=project_id,
        user_id=payload.get("user_id"),
        title="Deployment started",
        message=f"Deployment has started for project {project_id}.",
        type="INFO",
        metadata={"job_id": job_id, "job_type": "APPLY", "plan_job_id": plan_job_id},
    )

    threading.Thread(
        target=run_apply_worker,
        args=(project_id, job_id, plan_job_id, blueprint, credentials),
        kwargs={"triggered_by": payload.get("user_id")},
        daemon=True,
    ).start()

    return {"status": "accepted", "apply_job_id": job_id}

@app.post("/destroy")
def destroy_infra(payload: dict, request: Request):
    project_id = payload.get("project_id")
    blueprint  = payload.get("infra_blueprint") or {}

    if not project_id:
        raise HTTPException(status_code=400, detail="project_id required")

    credentials = get_project_credentials(project_id)

    caller_user_id = payload.get("caller_user_id")
    caller_role    = payload.get("caller_role", "admin")

    # ── Architect path → approval request ─────────────────────────────────
    if caller_role == "cloud_architect" and caller_user_id:

        # Fetch architect profile with proper fallbacks
        architect_name  = "Cloud Architect"
        architect_email = ""

        try:
            profile_res = supabase.table("user_profiles")\
                .select("full_name, email")\
                .eq("user_id", caller_user_id)\
                .execute()

            if profile_res.data:
                architect_name  = profile_res.data[0].get("full_name") or "Cloud Architect"
                architect_email = profile_res.data[0].get("email") or ""

        except Exception as e:
            print(f"⚠️ Could not fetch architect profile: {e}")

        # Fallback to auth table if email still missing
        if not architect_email:
            try:
                auth_user = supabase.auth.admin.get_user_by_id(caller_user_id)
                architect_email = auth_user.user.email or ""
                if architect_name == "Cloud Architect" and auth_user.user.user_metadata:
                    architect_name = auth_user.user.user_metadata.get("full_name") or architect_email
            except Exception as e:
                print(f"⚠️ Could not fetch architect from auth: {e}")

        print(f"👷 Architect: name={architect_name}, email={architect_email}")

        # Fetch project name + admin user_id
        project_name  = project_id
        admin_user_id = None

        try:
            proj_res = supabase.table("projects")\
                .select("project_name, user_id")\
                .eq("project_id", project_id)\
                .execute()

            if proj_res.data:
                project_name  = proj_res.data[0].get("project_name") or project_id
                admin_user_id = proj_res.data[0].get("user_id")

        except Exception as e:
            print(f"⚠️ Could not fetch project info: {e}")

        if not admin_user_id:
            raise HTTPException(status_code=404, detail="Could not find project admin.")

        # Fetch admin profile with proper fallbacks
        admin_name  = "Admin"
        admin_email = ""

        try:
            admin_profile_res = supabase.table("user_profiles")\
                .select("full_name, email")\
                .eq("user_id", admin_user_id)\
                .execute()

            if admin_profile_res.data:
                admin_name  = admin_profile_res.data[0].get("full_name") or "Admin"
                admin_email = admin_profile_res.data[0].get("email") or ""

        except Exception as e:
            print(f"⚠️ Could not fetch admin profile: {e}")

        # Fallback to auth table if admin email still missing
        if not admin_email:
            try:
                auth_admin = supabase.auth.admin.get_user_by_id(admin_user_id)
                admin_email = auth_admin.user.email or ""
                if admin_name == "Admin" and auth_admin.user.user_metadata:
                    admin_name = auth_admin.user.user_metadata.get("full_name") or admin_email
            except Exception as e:
                print(f"⚠️ Could not fetch admin from auth: {e}")

        print(f"👑 Admin: name={admin_name}, email={admin_email}")

        if not admin_email:
            raise HTTPException(
                status_code=500,
                detail="Could not resolve admin email. Cannot send approval request."
            )

        scope = payload.get("scope", "ALL")

        # Create the approval record
        try:
            approval_res = supabase.table("destroy_approvals").insert({
                "project_id":         project_id,
                "requested_by":       caller_user_id,
                "requested_by_email": architect_email,
                "requested_by_name":  architect_name,
                "admin_id":           str(admin_user_id),
                "admin_email":        admin_email,
                "blueprint":          blueprint or None,
                "scope":              scope,
                "status":             "pending",
            }).execute()
        except Exception as e:
            print(f"❌ Failed to create destroy approval record: {e}")
            raise HTTPException(status_code=500, detail="Failed to create approval request.")

        approval_token = approval_res.data[0]["token"] if approval_res.data else None

        if not approval_token:
            raise HTTPException(status_code=500, detail="Approval token was not generated.")

        print(f"🔑 Approval token created: {approval_token}")

        # Send email to admin in background
        threading.Thread(
            target=send_destroy_approval_email,
            args=(
                admin_email,
                admin_name,
                project_name,
                architect_name,
                architect_email,
                scope,
                str(approval_token),
            ),
            daemon=True,
        ).start()

        # Post to chat feed so both users see the request was made
        post_bot_chat_message(
            project_id=project_id,
            message_text=(
                f"⏳ {architect_name} has requested to destroy infrastructure "
                f"({'entire environment' if scope == 'ALL' else 'selected resources'}). "
                f"Waiting for admin approval."
            ),
        )

        # Notify the architect
        create_notification_for_user(
            project_id=project_id,
            user_id=caller_user_id,
            title="Destroy approval requested",
            message=f"Your request to destroy infrastructure in '{project_name}' has been sent to {admin_name} for approval.",
            type="INFO",
            metadata={"project_id": project_id, "job_type": "DESTROY_APPROVAL"},
        )

        return {
            "status":          "pending_approval",
            "message":         "Destroy request sent to admin for approval.",
            "approval_token":  str(approval_token),
        }

    # ── Admin path → existing flow ─────────────────────────────────────────
    job_id = f"destroy-{uuid.uuid4()}"

    supabase.table("jobs").insert({
        "job_id":     job_id,
        "project_id": project_id,
        "job_type":   "DESTROY",
        "status":     "RUNNING",
        "env":        blueprint.get("environment") if blueprint else None,
        "log_chunks": [],
    }).execute()

    print(f"🔥 Destroy request accepted for project_id={project_id}, job_id={job_id}")

    create_notification_for_user(
        project_id=project_id,
        user_id=caller_user_id,
        title="Destroy started",
        message=f"Infrastructure destruction has started for project {project_id}.",
        type="INFO",
        metadata={"job_id": job_id, "job_type": "DESTROY"},
    )

    threading.Thread(
        target=run_destroy_worker,
        args=(project_id, job_id, blueprint, credentials),
        kwargs={"triggered_by": caller_user_id},
        daemon=True,
    ).start()

    return {"status": "accepted", "job_id": job_id}

@app.get("/destroy-approvals/{token}")
async def get_destroy_approval(token: str, request: Request):
    """Returns approval request details for the ApproveDestroy page."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")

    access_token = auth_header.split(" ")[1].strip()
    try:
        user_res = supabase.auth.get_user(access_token)
        caller_id = str(user_res.user.id)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid session")

    res = supabase.table("destroy_approvals")\
        .select("*")\
        .eq("token", token)\
        .execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="Approval request not found or expired.")

    approval = res.data[0]

    # Only the project admin can view/action this
    if str(approval["admin_id"]) != caller_id:
        raise HTTPException(status_code=403, detail="Only the project admin can review this request.")

    # Fetch project name for display
    proj_res = supabase.table("projects")\
        .select("project_name")\
        .eq("project_id", approval["project_id"])\
        .execute()
    project_name = proj_res.data[0]["project_name"] if proj_res.data else approval["project_id"]

    return {
        "token":                str(approval["token"]),
        "project_id":           approval["project_id"],
        "project_name":         project_name,
        "requested_by_name":    approval["requested_by_name"],
        "requested_by_email":   approval["requested_by_email"],
        "scope":                approval["scope"],
        "status":               approval["status"],
        "created_at":           approval["created_at"],
        "expires_at":           approval["expires_at"],
    }


@app.post("/destroy-approvals/{token}/approve")
async def approve_destroy(token: str, request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")

    access_token = auth_header.split(" ")[1].strip()
    try:
        user_res  = supabase.auth.get_user(access_token)
        caller_id = str(user_res.user.id)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid session")

    res = supabase.table("destroy_approvals")\
        .select("*")\
        .eq("token", token)\
        .eq("status", "pending")\
        .execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="Approval request not found, already actioned, or expired.")

    approval = res.data[0]

    if str(approval["admin_id"]) != caller_id:
        raise HTTPException(status_code=403, detail="Only the project admin can approve this request.")

    # Fetch admin name for the chat message
    try:
        admin_profile = supabase.table("user_profiles")\
            .select("full_name")\
            .eq("user_id", caller_id)\
            .execute()
        admin_name = admin_profile.data[0].get("full_name", "Admin") if admin_profile.data else "Admin"
    except Exception:
        admin_name = "Admin"

    supabase.table("destroy_approvals")\
        .update({"status": "approved"})\
        .eq("token", token)\
        .execute()

    project_id  = approval["project_id"]
    blueprint   = approval.get("blueprint") or {}
    credentials = get_project_credentials(project_id)
    job_id      = f"destroy-{uuid.uuid4()}"

    supabase.table("jobs").insert({
        "job_id":     job_id,
        "project_id": project_id,
        "job_type":   "DESTROY",
        "status":     "RUNNING",
        "env":        blueprint.get("environment") if blueprint else None,
        "log_chunks": [],
    }).execute()

    # ── Post to chat feed ──────────────────────────────────────────────────
    post_bot_chat_message(
        project_id=project_id,
        message_text=(
            f"🔴 Destroy request from {approval['requested_by_name']} has been "
            f"approved by {admin_name}. Infrastructure destruction is now in progress."
        ),
        job_id=job_id,
    )

    create_notification_for_user(
        project_id=project_id,
        user_id=str(approval["requested_by"]),
        title="Destroy request approved",
        message=f"The admin approved your destroy request. Infrastructure is being destroyed.",
        type="WARNING",
        metadata={"project_id": project_id, "job_id": job_id, "job_type": "DESTROY"},
    )

    threading.Thread(
        target=run_destroy_worker,
        args=(project_id, job_id, blueprint, credentials),
        daemon=True,
    ).start()

    return {"status": "accepted", "job_id": job_id}

@app.post("/destroy-approvals/{token}/reject")
async def reject_destroy(token: str, request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")

    access_token = auth_header.split(" ")[1].strip()
    try:
        user_res  = supabase.auth.get_user(access_token)
        caller_id = str(user_res.user.id)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid session")

    res = supabase.table("destroy_approvals")\
        .select("*")\
        .eq("token", token)\
        .eq("status", "pending")\
        .execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="Approval request not found, already actioned, or expired.")

    approval = res.data[0]

    if str(approval["admin_id"]) != caller_id:
        raise HTTPException(status_code=403, detail="Only the project admin can reject this request.")

    # Fetch admin name for the chat message
    try:
        admin_profile = supabase.table("user_profiles")\
            .select("full_name")\
            .eq("user_id", caller_id)\
            .execute()
        admin_name = admin_profile.data[0].get("full_name", "Admin") if admin_profile.data else "Admin"
    except Exception:
        admin_name = "Admin"

    supabase.table("destroy_approvals")\
        .update({"status": "rejected"})\
        .eq("token", token)\
        .execute()

    project_id = approval["project_id"]

    # ── Post to chat feed ──────────────────────────────────────────────────
    post_bot_chat_message(
        project_id=project_id,
        message_text=(
            f"🛡️ Destroy request from {approval['requested_by_name']} has been "
            f"rejected by {admin_name}. No infrastructure changes were made."
        ),
    )

    create_notification_for_user(
        project_id=project_id,
        user_id=str(approval["requested_by"]),
        title="Destroy request rejected",
        message=f"The admin rejected your request to destroy infrastructure in this project.",
        type="ERROR",
        metadata={"project_id": project_id, "job_type": "DESTROY"},
    )

    return {"status": "rejected"}

# =========================================================
# FIX D: /status must return the shaped response the frontend expects.
#
# The new version returned `return job` (raw DB row).  The frontend
# pollStatus() reads these fields that are NOT top-level on a raw row:
#
#   data.structured_plan  → lives at job.result (PLAN jobs)
#   data.outputs          → lives at job.result.outputs (APPLY jobs)
#   data.access           → lives at job.result.access
#   data.cost_summary     → lives at job.result.cost_summary (COST jobs)
#   data.error            → lives at job.error_message
#
# Restoring the shaped response from the previous working version.
# =========================================================

@app.get("/status/{task_id}")
def get_status(task_id: str):
    try:
        res = (
            supabase.table("jobs")
            .select("*")
            .eq("job_id", task_id)
            .execute()
        )

        if not res.data:
            raise HTTPException(status_code=404, detail="Task not found")

        job = res.data[0]

        response = {
            "job_id": job["job_id"],
            "status": job["status"],
            "type": job.get("job_type"),
            "created_at": job.get("created_at"),
        }

        if job["status"] == "COMPLETED" and job.get("result"):
            if job.get("job_type") == "PLAN":
                structured_plan = job["result"]
                resource_changes = structured_plan.get("resource_changes", [])
                resources = [
                    {
                        "address": r.get("address"),
                        "type": r.get("type"),
                        "actions": r.get("change", {}).get("actions", []),
                    }
                    for r in resource_changes
                ]
                response["resources"] = resources
                response["structured_plan"] = structured_plan
                response["infra_blueprint"] = job.get("infra_blueprint")

            elif job.get("job_type") == "APPLY":
                response["outputs"] = job["result"].get("outputs", {})
                response["access"] = job["result"].get("access", [])

            elif job.get("job_type") == "COST":
                response["cost_summary"] = job["result"].get("cost_summary", {})

        if job["status"] == "FAILED":
            response["error"] = job.get("error_message")
            # Include AI analysis when available so frontend can render it
            # directly in DeploymentFailureView without a separate fetch
            if job.get("ai_analysis"):
                response["ai_analysis"] = job["ai_analysis"]

        return response

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ /status/{task_id} failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch task status: {str(e)}")

@app.get("/logs/project/{project_id}")
def get_project_logs(project_id: str):
    # Fetch all jobs for this project, ordered by creation time
    res = (
        supabase.table("jobs")
        .select("job_id, job_type, status, log_chunks, created_at")
        .eq("project_id", project_id)
        .order("created_at", desc=False)
        .execute()
    )

    if not res.data:
        return {"project_id": project_id, "chunks": [], "status": "EMPTY"}

    all_chunks = []
    
    # Iterate through every job (init, plan, apply, destroy)
    for job in res.data:
        job_chunks = job.get("log_chunks") or []
        # Each chunk in your DB is already {stage, stream, text}
        # We just collect them all into one big array
        for chunk in job_chunks:
            # We add a timestamp from the job if the chunk doesn't have one
            # to help the frontend maintain order
            chunk["job_id"] = job["job_id"]
            all_chunks.append(chunk)

    # Return the flattened list of all logs across all jobs
    return {
        "project_id": project_id,
        "chunks": all_chunks,
        "status": res.data[-1]["status"] if res.data else "UNKNOWN"
    }

@app.get("/projects")
async def get_projects(request: Request):
    # ---------------- AUTH ----------------
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")

    token = auth_header.split(" ")[1]

    try:
        user_res = supabase.auth.get_user(token)
        user_id = str(user_res.user.id)
        print("✅ LOGGED IN USER:", user_id)
    except Exception as e:
        print(f"🚨 SUPABASE REJECTED TOKEN: {e}")
        raise HTTPException(status_code=401, detail="Invalid session")

    projects_map = {}  # 🔥 Use dict to avoid duplicates

    try:
        # ---------------- BATCH 1: OWNED PROJECTS ----------------
        owned_res = supabase.table("projects")\
            .select("*")\
            .eq("user_id", user_id)\
            .execute()

        if isinstance(owned_res.data, list):
            for proj in owned_res.data:
                pid = str(proj.get("project_id"))
                if not pid:
                    continue

                proj["access_level"] = "admin"
                projects_map[pid] = proj

        # ---------------- BATCH 2: MEMBER PROJECTS ----------------
        member_res = supabase.table("project_members")\
            .select("project_id")\
            .eq("user_id", user_id)\
            .execute()

        if isinstance(member_res.data, list) and len(member_res.data) > 0:

            invited_project_ids = [
                str(m.get("project_id"))
                for m in member_res.data
                if m.get("project_id")
            ]

            print("📌 MEMBER PROJECT IDS:", invited_project_ids)

            # 🔥 SAFE FETCH (NO .in_())
            for pid in invited_project_ids:
                if not pid:
                    continue

                # Skip if already added as owner
                if pid in projects_map:
                    continue

                res = supabase.table("projects")\
                    .select("*")\
                    .eq("project_id", pid)\
                    .execute()

                if isinstance(res.data, list) and len(res.data) > 0:
                    proj = res.data[0]
                    proj["access_level"] = "cloud_architect"
                    projects_map[pid] = proj
                else:
                    print(f"⚠️ Project not found for project_id={pid}")

        # ---------------- FINAL LIST ----------------
        projects_list = list(projects_map.values())

        # Sort newest first
        projects_list.sort(
            key=lambda x: x.get("created_at", ""),
            reverse=True
        )

        print(f"✅ TOTAL PROJECTS RETURNED: {len(projects_list)}")

        return {"projects": projects_list}

    except Exception as e:
        print(f"🚨 Error fetching projects: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch projects")  
    
@app.post("/projects/{project_id}/members/invite")
def invite_members(project_id: str, payload: dict, user=Depends(get_current_user)):
    emails = payload.get("emails", [])

    if not emails:
        raise HTTPException(status_code=400, detail="No emails provided")

    # 🔐 Ensure only project owner (admin) can invite
    project_res = supabase.table("projects")\
        .select("project_name, user_id")\
        .eq("project_id", project_id)\
        .execute()

    if not project_res.data:
        raise HTTPException(status_code=404, detail="Project not found")

    project = project_res.data[0]

    if str(project["user_id"]) != str(user.id):
        raise HTTPException(status_code=403, detail="Only admin can invite members")

    project_name = project["project_name"]

    for email in emails:
        try:
            # 🔥 Prevent duplicate accepted users
            existing = supabase.table("project_invitations")\
                .select("status")\
                .eq("project_id", project_id)\
                .eq("email", email)\
                .execute()

            if existing.data and existing.data[0]["status"] == "accepted":
                continue

            invite_token = str(uuid.uuid4())

            # Upsert invite
            supabase.table("project_invitations").upsert({
                "token": invite_token,
                "project_id": project_id,
                "email": email,
                "invited_by": str(user.id),
                "status": "pending"
            }, on_conflict="project_id,email").execute()

            # Send email
            send_invitation_email(
                target_email=email,
                project_name=project_name,
                inviter_email=user.email,
                invite_token=invite_token
            )

        except Exception as e:
            print(f"❌ Invite failed for {email}: {e}")

    return {"success": True}

@app.get("/projects/{project_id}/members")
def get_members(project_id: str, user=Depends(get_current_user)):

    res = supabase.table("project_members")\
        .select("""
            user_id,
            added_at,
            user_profiles (
                full_name,
                email
            )
        """)\
        .eq("project_id", project_id)\
        .execute()

    members = []

    for m in res.data or []:
        profile = m.get("user_profiles") or {}
        members.append({
            "user_id": m["user_id"],
            "name": profile.get("full_name"),
            "email": profile.get("email"),
            "added_at": m["added_at"]
        })

    return {"members": members}

@app.post("/projects/{project_id}/members")
def add_member(project_id: str, payload: dict, user=Depends(get_current_user)):
    new_user_id = payload.get("user_id")
    role = payload.get("role", "cloud_architect")

    supabase.table("project_members").insert({
        "project_id": project_id,
        "user_id": new_user_id,
        "role": role
    }).execute()

    return {"success": True}


@app.delete("/projects/{project_id}/members/{user_id}")
def remove_member(project_id: str, user_id: str, user=Depends(get_current_user)):

    # 1. Check project exists
    project_res = supabase.table("projects")\
        .select("user_id")\
        .eq("project_id", project_id)\
        .execute()

    if not project_res.data:
        raise HTTPException(status_code=404, detail="Project not found")

    project_owner = project_res.data[0]["user_id"]

    # 2. Only admin can remove
    if str(project_owner) != str(user.id):
        raise HTTPException(status_code=403, detail="Only admin can remove members")

    # 3. Prevent self removal (optional but recommended)
    if str(user_id) == str(user.id):
        raise HTTPException(status_code=400, detail="Cannot remove yourself")

    # 4. Delete member
    supabase.table("project_members")\
        .delete()\
        .eq("project_id", project_id)\
        .eq("user_id", user_id)\
        .execute()

    return {"success": True}

@app.get("/chats/{project_id}")
def get_chat_history(project_id: str, user=Depends(get_current_user)):
    project_res = (
        supabase.table("projects")
        .select("project_id, user_id")
        .eq("project_id", project_id)
        .execute()
    )

    if not project_res.data:
        raise HTTPException(status_code=404, detail="Project not found")

    is_authorized = False

    if str(project_res.data[0]["user_id"]) == str(user.id):  # ✅ fixed [0]
        is_authorized = True
    else:
        member_res = (
            supabase.table("project_members")
            .select("id")
            .eq("project_id", project_id)
            .eq("user_id", user.id)
            .execute()
        )
        if member_res.data and len(member_res.data) > 0:
            is_authorized = True

    if not is_authorized:
        raise HTTPException(
            status_code=403,
            detail="Unauthorized access. Must be the project owner or an invited member."
        )

    messages_res = (
        supabase.table("chat_messages")
        .select("message_id, sender, sender_name, sender_role, message_text, created_at, job_id")  # ✅ new columns
        .eq("project_id", project_id)
        .order("created_at", desc=False)
        .execute()
    )
    messages = messages_res.data

    jobs_res  = supabase.table("jobs").select("*").eq("project_id", project_id).execute()
    jobs_list = jobs_res.data
    jobs_map  = {job["job_id"]: job for job in jobs_list}

    cost_map = {}
    for job in jobs_list:
        if job["job_type"] == "COST" and job["status"] == "COMPLETED" and job.get("run_id"):
            cost_map[job["run_id"]] = job.get("result", {}).get("cost_summary")

    for msg in messages:
        if msg.get("job_id") and msg["job_id"] in jobs_map:
            msg["job_details"] = jobs_map[msg["job_id"]]
            if msg["job_details"]["job_type"] == "PLAN" and msg["job_id"] in cost_map:
                msg["job_details"]["cost_summary"] = cost_map[msg["job_id"]]

    return {"messages": messages}


@app.post("/chats")
def save_chat_message(payload: dict, user=Depends(get_current_user)):
    project_id = payload.get("project_id")
    sender = payload.get("sender")
    message_text = payload.get("message_text")
    job_id = payload.get("job_id")

    if not project_id or not sender or not message_text:
        raise HTTPException(status_code=400, detail="Missing chat data")

    project_res = (
        supabase.table("projects").select("project_id, user_id")
        .eq("project_id", project_id).execute()
    )
    if not project_res.data:
        raise HTTPException(status_code=404, detail="Project not found")

    is_authorized = False
    if str(project_res.data[0]["user_id"]) == str(user.id):
        is_authorized = True
    else:
        member_res = supabase.table("project_members").select("id")\
            .eq("project_id", project_id).eq("user_id", user.id).execute()
        if member_res.data and len(member_res.data) > 0:
            is_authorized = True

    if not is_authorized:
        raise HTTPException(status_code=403, detail="Unauthorized access")

    # Fetch sender's name + role for group chat display
    sender_name = "CloudCrafter"
    sender_role = "bot"
    if sender.upper() == "USER":
        try:
            profile_res = supabase.table("user_profiles")\
                .select("full_name, role").eq("user_id", str(user.id)).execute()
            if profile_res.data:
                sender_name = profile_res.data[0].get("full_name") or user.email or "User"
                sender_role = profile_res.data[0].get("role") or "admin"
        except Exception as e:
            print(f"⚠️ Could not fetch sender profile: {e}")
            sender_name = user.email or "User"
            sender_role = "admin"

    insert_data = {
        "project_id": project_id,
        "sender": sender.upper(),
        "sender_name": sender_name,
        "sender_role": sender_role,
        "message_text": message_text,
    }
    if job_id:
        insert_data["job_id"] = job_id

    supabase.table("chat_messages").insert(insert_data).execute()
    return {"status": "success"}

@app.post("/jobs/{job_id}/discard")
def discard_job(job_id: str, user=Depends(get_current_user)):
    job_res = (
        supabase.table("jobs")
        .select("job_id, project_id, job_type")
        .eq("job_id", job_id)
        .execute()
    )

    if not job_res.data:
        raise HTTPException(status_code=404, detail="Job not found")

    job = job_res.data[0]

    project_res = (
        supabase.table("projects")
        .select("project_id, user_id")
        .eq("project_id", job["project_id"])
        .execute()
    )

    if not project_res.data:
        raise HTTPException(status_code=404, detail="Project not found")

    if str(project_res.data[0]["user_id"]) != str(user.id):
        raise HTTPException(status_code=403, detail="Unauthorized access")

    supabase.table("jobs").update({"status": "DISCARDED"}).eq("job_id", job_id).execute()

    create_notification_for_user(
        user_id=str(user.id),
        title="Job discarded",
        message=f"{job['job_type']} job was discarded for project {job['project_id']}.",
        type="WARNING",
        metadata={
            "job_id": job["job_id"],
            "job_type": job["job_type"],
            "project_id": job["project_id"],
        },
    )

    return {"status": "success"}