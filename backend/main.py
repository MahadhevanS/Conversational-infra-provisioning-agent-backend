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

from backend.db import supabase
from backend.orchestrator import terraform_plan, terraform_apply, terraform_destroy, terraform_cost
from backend.lex import lex_webhook
from backend.ai_analyser import analyse_failure

from backend.email_service import send_invitation_email

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


def create_notification_for_user(user_id, title, message, type="INFO", metadata=None):
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


def create_notification_for_project(project_id, title, message, type="INFO", metadata=None):
    try:
        user_id = get_project_user_id(project_id)
        if not user_id:
            print(f"⚠️ No user found for project {project_id}, skipping notification")
            return
        create_notification_for_user(user_id=user_id, title=title, message=message,
                                     type=type, metadata=metadata)
    except Exception as e:
        print(f"❌ Failed to create project notification: {e}")


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


def run_plan_worker(project_id, job_id, blueprint, credentials=None):
    try:
        update_job_status(job_id, "RUNNING")
        result = terraform_plan(project_id, blueprint, job_id, credentials=credentials)

        if "error" in result:
            print(f"❌ TERRAFORM ERROR: {result['error']}")
            update_job_status(job_id, "FAILED", result["error"])
            create_notification_for_project(
                project_id, "PLAN failed",
                f"PLAN job failed for project {project_id}.", "ERROR",
                {"job_id": job_id, "job_type": "PLAN"},
            )
            _fire_ai_analysis(project_id, job_id, "PLAN")
        else:
            update_job_status(job_id, "COMPLETED", result["structured_plan"])
            create_notification_for_project(
                project_id, "PLAN completed",
                f"PLAN job completed successfully for project {project_id}.", "SUCCESS",
                {"job_id": job_id, "job_type": "PLAN"},
            )
    except Exception as e:
        print(f"❌ WORKER CRASHED: {str(e)}")
        update_job_status(job_id, "FAILED", str(e))
        create_notification_for_project(
            project_id, "PLAN failed",
            f"PLAN job crashed for project {project_id}.", "ERROR",
            {"job_id": job_id, "job_type": "PLAN"},
        )
        _fire_ai_analysis(project_id, job_id, "PLAN")


def run_apply_worker(project_id, job_id, plan_job_id, blueprint, credentials=None):
    """
    job_id      = the APPLY job's own id (status tracking AND log storage)
    plan_job_id = the preceding plan job (locates the workspace on disk)
    """
    try:
        update_job_status(job_id, "RUNNING")

        # FIX B: pass apply_job_id so orchestrator stores logs on the apply job,
        # not the plan job.  Previously this arg was missing → logs were invisible
        # in the LogPanel because the frontend polls apply_job_id.
        result = terraform_apply(
            project_id,
            plan_job_id,
            blueprint,
            credentials=credentials,
            apply_job_id=job_id,
        )

        if "error" in result:
            update_job_status(job_id, "FAILED", result)
            create_notification_for_project(
                project_id, "Deployment failed",
                f"Infrastructure deployment failed for project {project_id}.", "ERROR",
                {"job_id": job_id, "job_type": "APPLY", "plan_job_id": plan_job_id},
            )
            _fire_ai_analysis(project_id, job_id, "APPLY")
        else:
            update_job_status(job_id, "COMPLETED", result)
            create_notification_for_project(
                project_id, "Deployment completed",
                f"Infrastructure deployed successfully for project {project_id}.", "SUCCESS",
                {"job_id": job_id, "job_type": "APPLY", "plan_job_id": plan_job_id},
            )

    except Exception as e:
        update_job_status(job_id, "FAILED", str(e))
        create_notification_for_project(
            project_id, "Deployment failed",
            f"Infrastructure deployment crashed for project {project_id}.", "ERROR",
            {"job_id": job_id, "job_type": "APPLY", "plan_job_id": plan_job_id},
        )
        _fire_ai_analysis(project_id, job_id, "APPLY")


def run_cost_worker(project_id, job_id, run_id, blueprint, credentials=None):
    try:
        update_job_status(job_id, "RUNNING")
        result = terraform_cost(project_id, run_id, blueprint, credentials=credentials)

        if "error" in result:
            update_job_status(job_id, "FAILED", result)
            create_notification_for_project(
                project_id, "Cost estimation failed",
                f"Cost estimation failed for project {project_id}.", "ERROR",
                {"job_id": job_id, "job_type": "COST", "run_id": run_id},
            )
        else:
            update_job_status(job_id, "COMPLETED", result)
            create_notification_for_project(
                project_id, "Cost estimation ready",
                f"Cost estimate is ready for project {project_id}.", "SUCCESS",
                {"job_id": job_id, "job_type": "COST", "run_id": run_id},
            )
    except Exception as e:
        update_job_status(job_id, "FAILED", str(e))
        create_notification_for_project(
            project_id, "Cost estimation failed",
            f"Cost estimation crashed for project {project_id}.", "ERROR",
            {"job_id": job_id, "job_type": "COST", "run_id": run_id},
        )


def run_destroy_worker(project_id, job_id, blueprint, credentials=None):
    try:
        print(f"🚀 Destroy worker started for project_id={project_id}, job_id={job_id}")
        update_job_status(job_id, "RUNNING")

        # FIX C: pass job_id so orchestrator stores destroy logs on the destroy
        # job row, not a fallback "destroy-unknown-*" id that the frontend never polls.
        result = terraform_destroy(
            project_id,
            blueprint,
            credentials=credentials,
            job_id=job_id,
        )
        print(f"🧾 Destroy result for {job_id}: {result}")

        if isinstance(result, dict) and "error" in result:
            update_job_status(job_id, "FAILED", result.get("error"))
            create_notification_for_project(
                project_id, "Destroy failed",
                f"Infrastructure destruction failed for project {project_id}.", "ERROR",
                {"job_id": job_id, "job_type": "DESTROY"},
            )
            _fire_ai_analysis(project_id, job_id, "DESTROY")
            print(f"❌ Destroy failed notification sent for job_id={job_id}")
        else:
            update_job_status(job_id, "COMPLETED", result)
            create_notification_for_project(
                project_id, "Destroy completed",
                f"All infrastructure destroyed successfully for project {project_id}.", "SUCCESS",
                {"job_id": job_id, "job_type": "DESTROY"},
            )
            print(f"✅ Destroy completed notification sent for job_id={job_id}")

    except Exception as e:
        print(f"❌ Destroy worker crashed for job_id={job_id}: {str(e)}")
        update_job_status(job_id, "FAILED", str(e))
        create_notification_for_project(
            project_id, "Destroy failed",
            f"Destroy process crashed for project {project_id}.", "ERROR",
            {"job_id": job_id, "job_type": "DESTROY"},
        )
        _fire_ai_analysis(project_id, job_id, "DESTROY")


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
def get_notifications(user_id: str, user=Depends(get_current_user)):
    try:
        if str(user.id) != str(user_id):
            raise HTTPException(status_code=403, detail="Unauthorized access")

        res = (
            supabase.table("notifications")
            .select("id, user_id, title, message, type, is_read, is_deleted, created_at, metadata, notification_key")
            .eq("user_id", user_id)
            .eq("is_deleted", False)
            .order("created_at", desc=True)
            .execute()
        )

        return {"success": True, "notifications": res.data or []}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ /notifications failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch notifications: {str(e)}")


@app.get("/notifications/unread-count")
def get_unread_count(user_id: str, user=Depends(get_current_user)):
    try:
        if str(user.id) != str(user_id):
            raise HTTPException(status_code=403, detail="Unauthorized access")

        res = (
            supabase.table("notifications")
            .select("id")
            .eq("user_id", user_id)
            .eq("is_read", False)
            .eq("is_deleted", False)
            .execute()
        )

        return {"success": True, "unread_count": len(res.data or [])}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ /notifications/unread-count failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch unread count: {str(e)}")


@app.put("/notifications/read-all")
def mark_all_notifications_read(user_id: str, user=Depends(get_current_user)):
    try:
        if str(user.id) != str(user_id):
            raise HTTPException(status_code=403, detail="Unauthorized access")

        res = (
            supabase.table("notifications")
            .update({"is_read": True})
            .eq("user_id", user_id)
            .eq("is_read", False)
            .eq("is_deleted", False)
            .execute()
        )

        return {"success": True, "updated": len(res.data) if res.data else 0}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to mark all notifications as read: {str(e)}")


@app.delete("/notifications/clear-all")
def clear_all_notifications(user_id: str, user=Depends(get_current_user)):
    try:
        if str(user.id) != str(user_id):
            raise HTTPException(status_code=403, detail="Unauthorized access")

        res = (
            supabase.table("notifications")
            .update({"is_deleted": True})
            .eq("user_id", user_id)
            .eq("is_deleted", False)
            .execute()
        )

        print(f"🗑️ Soft-cleared all notifications for user {user_id}: {res.data}")
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ clear-all failed: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to clear notifications: {str(e)}")


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
    email = payload.get("email")
    password = payload.get("password")
    role_arn = payload.get("role_arn")
    external_id = payload.get("external_id")
    full_name = payload.get("full_name", "")
    
    # 🔥 RBAC: Get the role from the frontend, default to 'admin' if empty
    requested_role = payload.get("role", "admin")
    if requested_role not in ["admin", "cloud_architect"]:
        requested_role = "admin"

    if not email or not password or not role_arn:
        raise HTTPException(status_code=400, detail="email, password, and role_arn required")

    try:
        auth_response = supabase.auth.sign_up({
            "email": email,
            "password": password,
            "options": {"data": {"full_name": full_name}},
        })
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    user_id = auth_response.user.id

    try:
        access_key = os.environ.get("AWS_ACCESS_KEY_ID", "").replace('"', "").replace("'", "").strip()
        secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "").replace('"', "").replace("'", "").strip()
        region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1").replace('"', "").replace("'", "").strip()

        sts = boto3.client("sts", aws_access_key_id=access_key,
                           aws_secret_access_key=secret_key, region_name=region)
        assume_params = {"RoleArn": role_arn, "RoleSessionName": "SignupValidation"}
        if external_id:
            assume_params["ExternalId"] = external_id
        sts.assume_role(**assume_params)

    except Exception as e:
        try:
            supabase.auth.admin.delete_user(user_id)
        except Exception as rollback_error:
            print(f"⚠️ Warning: Could not delete user {user_id}. Error: {rollback_error}")
        raise HTTPException(status_code=400, detail=f"IAM Role validation failed: {str(e)}")

    try:
        # Save AWS Credentials
        supabase.table("aws_credentials").insert({
            "user_id": user_id,
            "role_arn": role_arn,
            "external_id": external_id,
        }).execute()
        
        # 🔥 RBAC: Save the user's role to the new profiles table!
        supabase.table("user_profiles").insert({
            "user_id": user_id,
            "email": email,
            "full_name": full_name,
            "role": requested_role
        }).execute()
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Database storage failed: {str(e)}")

    return {
        "message": "Signup successful! Please check your email to verify your account.",
        "user_id": user_id,
    }

@app.post("/login")
def login(payload: dict):
    email = payload.get("email")
    password = payload.get("password")

    if not email or not password:
        raise HTTPException(status_code=400, detail="Email and password required")

    # 1. Authenticate with Supabase
    try:
        auth_response = supabase.auth.sign_in_with_password({
            "email": email,
            "password": password,
        })
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    user_id = auth_response.user.id

    # 2. Role Extraction
    user_role = ""
    try:
        profile_res = supabase.table("user_profiles").select("role").eq("user_id", user_id).execute()

        if hasattr(profile_res, 'data') and isinstance(profile_res.data, list) and len(profile_res.data) > 0:
            first_row = profile_res.data[0]  # ✅ FIX: added [0]
            if isinstance(first_row, dict):
                user_role = first_row.get("role", "")

    except Exception as e:
        print(f"⚠️ Warning: Profile extraction failed. Error: {e}")

    # 3. AWS Credentials Extraction
    aws_account_id = ""
    role_arn = ""
    external_id = ""

    try:
        aws_res = supabase.table("aws_credentials").select("*").eq("user_id", user_id).execute()

        if hasattr(aws_res, 'data') and isinstance(aws_res.data, list) and len(aws_res.data) > 0:
            first_aws = aws_res.data[0]  # ✅ FIX: added [0]
            if isinstance(first_aws, dict):
                role_arn = first_aws.get("role_arn", "")
                external_id = first_aws.get("external_id", "")

                if role_arn and ":" in role_arn:
                    arn_parts = role_arn.split(":")
                    if len(arn_parts) >= 5:
                        aws_account_id = arn_parts[4]  # ✅ FIX: added [4]

    except Exception as e:
        print(f"⚠️ Warning: AWS extraction failed. Error: {e}")

    # 4. Return Payload
    try:
        return {
            "access_token": auth_response.session.access_token,
            "email": auth_response.user.email,
            "full_name": auth_response.user.user_metadata.get("full_name", "") if auth_response.user.user_metadata else "",
            "role": user_role,
            "aws_account_id": aws_account_id,
            "aws_region": "us-east-1",
            "role_arn": role_arn,
            "external_id": external_id
        }
    except Exception as e:
        import traceback
        print("🚨 CRITICAL ERROR formatting login return payload:")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Failed to format login response data.")


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
            
    except Exception as e:
        print(f"🚨 Failed to create project: {e}")
        raise HTTPException(status_code=500, detail="Failed to create project")

    # 2. HANDLE INVITATIONS
    successful_invites = []
    
    if invite_emails:
        for email in invite_emails:
            email = email.strip()
            if not email: continue
            
            try:
                invite_res = supabase.table("project_invitations").insert({
                    "project_id": project_id,
                    "email": email,
                    "invited_by": admin_id,
                    "status": "pending"
                }).execute()
                
                # Safe extraction for invitations
                invite_data = invite_res.data
                invite_token = None
                for item in invite_data:
                    if isinstance(item, dict):
                        invite_token = item.get("token")
                        break

                if invite_token:
                    background_tasks.add_task(
                        send_invitation_email, email, project_name, admin_email, invite_token
                    )
                    successful_invites.append(email)
                    
            except Exception as e:
                print(f"⚠️ Invitation failed for {email}: {e}")

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

@app.delete("/projects/{project_id}/members/{email}")
async def remove_architect(project_id: str, email: str, request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    
    # 🔥 FIXED: Added.strip()
    token = auth_header.split(" ").strip()
    
    try:
        user_res = supabase.auth.get_user(token)
        admin_id = user_res.user.id
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid session")

    # 2. SECURITY CHECK
    # 🔥 FIXED: Changed "owner_id" to "user_id"
    proj_res = supabase.table("projects").select("user_id").eq("project_id", project_id).execute()
    if not proj_res.data or len(proj_res.data) == 0:
        raise HTTPException(status_code=404, detail="Project not found.")
    
    # 🔥 FIXED: Added
    if proj_res.data.get("user_id") != admin_id:
        raise HTTPException(status_code=403, detail="Only the project Admin can remove members.")

    try:
        profile_res = supabase.table("user_profiles").select("user_id").eq("email", email).execute()
        
        if not (hasattr(profile_res, 'data') and isinstance(profile_res.data, list) and len(profile_res.data) > 0):
            raise HTTPException(status_code=404, detail=f"No account found for {email}.")
            
        # 🔥 FIXED: Added
        target_user_id = profile_res.data.get("user_id")

        delete_res = supabase.table("project_members").delete().eq("project_id", project_id).eq("user_id", target_user_id).execute()
        
        if not delete_res.data or len(delete_res.data) == 0:
            return {"message": f"{email} is not a member of this project."}

        return {"message": f"Successfully removed {email} from the project."}

    except HTTPException:
        raise 
    except Exception as e:
        print(f"🚨 Removal Error: {e}")
        raise HTTPException(status_code=500, detail="Failed to remove member.")

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

    create_notification_for_project(
        project_id, "PLAN started", f"PLAN job has started in {env}.", "INFO",
        {"job_id": job_id, "job_type": "PLAN"},
    )

    threading.Thread(
        target=run_plan_worker, args=(project_id, job_id, blueprint, credentials), daemon=True
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

    create_notification_for_project(
        project_id, "Cost estimation started",
        f"Cost estimation started for project {project_id}.", "INFO",
        {"job_id": job_id, "job_type": "COST", "run_id": run_id},
    )

    threading.Thread(
        target=run_cost_worker, args=(project_id, job_id, run_id, blueprint, credentials), daemon=True
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

    create_notification_for_project(
        project_id, "Deployment started",
        f"Deployment has started for project {project_id}.", "INFO",
        {"job_id": job_id, "job_type": "APPLY", "plan_job_id": plan_job_id},
    )

    threading.Thread(
        target=run_apply_worker,
        args=(project_id, job_id, plan_job_id, blueprint, credentials),
        daemon=True,
    ).start()

    return {"status": "accepted", "apply_job_id": job_id}


@app.post("/destroy")
def destroy_infra(payload: dict):
    project_id = payload.get("project_id")
    blueprint = payload.get("infra_blueprint") or {}

    if not project_id:
        raise HTTPException(status_code=400, detail="project_id required")

    # No user auth header here — /destroy is called server-to-server from
    # lex.py (urllib.request, no Authorization header).  Security is provided
    # by get_project_credentials: it verifies the project exists in the DB and
    # that AWS credentials are on file before any Terraform work starts.
    # The Lex session only ever holds the user's own project_id, so there is
    # no cross-tenant risk on this internal path.
    credentials = get_project_credentials(project_id)
    job_id = f"destroy-{uuid.uuid4()}"

    supabase.table("jobs").insert({
        "job_id": job_id,
        "project_id": project_id,
        "job_type": "DESTROY",
        "status": "RUNNING",
        "env": blueprint.get("environment") if blueprint else None,
        "log_chunks": [],
    }).execute()

    print(f"🔥 Destroy request accepted for project_id={project_id}, job_id={job_id}")

    create_notification_for_project(
        project_id, "Destroy started",
        f"Infrastructure destruction has started for project {project_id}.", "INFO",
        {"job_id": job_id, "job_type": "DESTROY"},
    )

    threading.Thread(
        target=run_destroy_worker,
        args=(project_id, job_id, blueprint, credentials),
        daemon=True,
    ).start()

    return {"status": "accepted", "job_id": job_id}


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


@app.get("/logs/{job_id}")
def get_job_logs(job_id: str):
    res = (
        supabase.table("jobs")
        .select("job_id, job_type, status, log_chunks")
        .eq("job_id", job_id)
        .single()
        .execute()
    )

    if not res.data:
        return {"job_id": job_id, "chunks": [], "status": "NOT_FOUND"}

    job = res.data
    chunks = job.get("log_chunks") or []

    return {
        "job_id": job["job_id"],
        "job_type": job.get("job_type"),
        "status": job.get("status"),
        "chunks": chunks,
    }


@app.get("/projects")
async def get_projects(request: Request):
    # 1. Authenticate the user
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    
    token = auth_header.split(" ")[1]  # ✅ FIX: grab the actual token string
    
    try:
        user_res = supabase.auth.get_user(token)
        user_id = user_res.user.id
    except Exception as e:
        print(f"🚨 SUPABASE REJECTED TOKEN: {e}")
        raise HTTPException(status_code=401, detail="Invalid session")

    projects_list = []
    
    try:
        # BATCH 1: Fetch projects the user OWNS
        owned_res = supabase.table("projects").select("*").eq("user_id", user_id).execute()
        
        if hasattr(owned_res, 'data') and isinstance(owned_res.data, list):
            for proj in owned_res.data:
                proj["access_level"] = "admin"
                projects_list.append(proj)

        # BATCH 2: Fetch projects the user is INVITED TO
        member_res = supabase.table("project_members").select("project_id").eq("user_id", user_id).execute()
        
        if hasattr(member_res, 'data') and isinstance(member_res.data, list) and len(member_res.data) > 0:
            invited_project_ids = [m.get("project_id") for m in member_res.data if m.get("project_id")]
            
            if invited_project_ids:
                invited_projects_res = supabase.table("projects").select("*").in_("project_id", invited_project_ids).execute()
                
                if hasattr(invited_projects_res, 'data') and isinstance(invited_projects_res.data, list):
                    for proj in invited_projects_res.data:
                        proj["access_level"] = "cloud_architect"
                        projects_list.append(proj)

        # Sort by newest first
        projects_list.sort(key=lambda x: x.get("created_at", ""), reverse=True)

        return {"projects": projects_list}

    except Exception as e:
        print(f"🚨 Error fetching projects: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch projects")
    

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

    # --- TEAM-FRIENDLY SECURITY CHECK ---
    is_authorized = False
    
    # 1. Are they the Project Owner?
    # 🔥 FIXED: Added right after .data
    if str(project_res.data["user_id"]) == str(user.id):
        is_authorized = True
    else:
        # 2. Are they an invited Cloud Architect?
        member_res = supabase.table("project_members").select("id").eq("project_id", project_id).eq("user_id", user.id).execute()
        if member_res.data and len(member_res.data) > 0:
            is_authorized = True

    if not is_authorized:
        raise HTTPException(status_code=403, detail="Unauthorized access. Must be the project owner or an invited member.")
    # ------------------------------------

    messages_res = (
        supabase.table("chat_messages")
        .select("message_id, sender, message_text, created_at, job_id")
        .eq("project_id", project_id)
        .order("created_at", desc=False)
        .execute()
    )
    messages = messages_res.data

    jobs_res = supabase.table("jobs").select("*").eq("project_id", project_id).execute()
    jobs_list = jobs_res.data
    jobs_map = {job["job_id"]: job for job in jobs_list}

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
        supabase.table("projects")
        .select("project_id, user_id")
        .eq("project_id", project_id)
        .execute()
    )

    if not project_res.data:
        raise HTTPException(status_code=404, detail="Project not found")

    # --- TEAM-FRIENDLY SECURITY CHECK ---
    is_authorized = False
    
    # 1. Are they the Project Owner?
    # 🔥 FIXED: Added right after .data
    if str(project_res.data["user_id"]) == str(user.id):
        is_authorized = True
    else:
        # 2. Are they an invited Cloud Architect?
        member_res = supabase.table("project_members").select("id").eq("project_id", project_id).eq("user_id", user.id).execute()
        if member_res.data and len(member_res.data) > 0:
            is_authorized = True

    if not is_authorized:
        raise HTTPException(status_code=403, detail="Unauthorized access. Must be the project owner or an invited member.")
    # ------------------------------------

    insert_data = {
        "project_id": project_id,
        "sender": sender.upper(),
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