import subprocess
import os
import json
import shutil
import platform
import threading
from tfvars_generator import generate_tfvars
from terraform.executor import TerraformExecutor
import boto3

# --- PATHS --- 
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS_BASE_DIR = os.path.join(PROJECT_ROOT, "persistent_jobs")
os.makedirs(JOBS_BASE_DIR, exist_ok=True)

# --- GLOBAL LOCKS ---
ENV_LOCKS = {}

ENV_MAP = {
    "production": "prod",
    "prod": "prod",
    "development": "dev",
    "dev": "dev"
}

def get_env_lock(project_id, env_name):
    key = f"{project_id}-{env_name}"
    if key not in ENV_LOCKS:
        ENV_LOCKS[key] = threading.Lock()
    return ENV_LOCKS[key]

# --- HELPERS ---
def assume_role(role_arn, external_id=None):
    sts = boto3.client(
        "sts",
        region_name="us-east-1"  # or from blueprint
    )

    assume_params = {
        "RoleArn": role_arn,
        "RoleSessionName": "CloudCrafterSession"
    }

    if external_id:
        assume_params["ExternalId"] = external_id

    response = sts.assume_role(**assume_params)

    creds = response["Credentials"]

    return {
        "AWS_ACCESS_KEY_ID": creds["AccessKeyId"],
        "AWS_SECRET_ACCESS_KEY": creds["SecretAccessKey"],
        "AWS_SESSION_TOKEN": creds["SessionToken"]
    }

def get_workspace_path(project_id, job_id, env_name):
    env_folder = "prod" if "prod" in env_name.lower() else "dev"
    return os.path.join(JOBS_BASE_DIR, project_id, job_id, "terraform", "envs", env_folder)

def find_latest_state(project_id, env_name):
    env_folder = "prod" if "prod" in env_name.lower() else "dev"

    project_path = os.path.join(JOBS_BASE_DIR, project_id)

    if not os.path.exists(project_path):
        return None

    job_ids = sorted(
        os.listdir(project_path),
        key=lambda x: os.path.getctime(os.path.join(project_path, x)),
        reverse=True
    )

    for job_id in job_ids:
        state_path = os.path.join(
            project_path,
            job_id,
            "terraform",
            "envs",
            env_folder,
            "terraform.tfstate"
        )

        if os.path.exists(state_path):
            return state_path

    return None

def format_access_points(outputs):
    access = []

    if "alb_dns_name" in outputs:
        access.append({
            "service": "Web Application",
            "url": f"http://{outputs['alb_dns_name']['value']}"
        })

    if "ec2_public_ip" in outputs:
        access.append({
            "service": "EC2 Server",
            "url": f"http://{outputs['ec2_public_ip']['value']}"
        })

    if "s3_website_url" in outputs:
        access.append({
            "service": "Static Website",
            "url": f"http://{outputs['s3_website_url']['value']}"
        })

    if "rds_endpoint" in outputs:
        access.append({
            "service": "Database",
            "endpoint": outputs["rds_endpoint"]["value"]
        })

    return access

# --- CORE LOGIC ---

def terraform_plan(project_id, blueprint, job_id, credentials):
    try:
        env = blueprint.get("environment", "development").lower()
        workspace_path = get_workspace_path(project_id, job_id, env)
        
        # 1. Prepare Directory Structure
        os.makedirs(workspace_path, exist_ok=True)
        
        # 2. Sync Source Files
        source_env = "prod" if "prod" in env else "dev"
        source_dir = os.path.join(PROJECT_ROOT, "terraform", "envs", source_env)
        
        for item in os.listdir(source_dir):
            s = os.path.join(source_dir, item)
            d = os.path.join(workspace_path, item)
            if os.path.isfile(s) and s.endswith(".tf"):
                shutil.copy2(s, d)

        # 2. Inject Latest State (Crucial for Modify/Terminate)
        latest_state = find_latest_state(project_id, env)
        if latest_state:
            shutil.copy2(latest_state, os.path.join(workspace_path, "terraform.tfstate"))

        # 3. Handle Modules (Symlink)
        modules_src = os.path.join(PROJECT_ROOT, "terraform", "modules")
        modules_dst = os.path.join(
            JOBS_BASE_DIR,
            project_id,
            job_id,
            "terraform",
            "modules"
        )
        os.makedirs(os.path.dirname(modules_dst), exist_ok=True)
        
        if not os.path.exists(modules_dst):
            if platform.system() == "Windows":
                subprocess.run(['cmd', '/c', 'mklink', '/j', modules_dst, modules_src], check=True)
            else:
                os.symlink(modules_src, modules_dst)

        aws_env = os.environ.copy()

        if credentials:
            temp_creds = assume_role(
                credentials["role_arn"],
                credentials.get("external_id")
            )
            aws_env.update(temp_creds)

        # 4. Execute Plan with Locking
        with get_env_lock(project_id, env):
            tfvars_path = generate_tfvars(blueprint, workspace_path)
            
            # Init
            subprocess.run(["terraform", "init", "-no-color", "-input=false"], 
                           cwd=workspace_path, env=aws_env, capture_output=True, check=True)
            
            # Plan
            plan_proc = subprocess.run(
                ["terraform", "plan", f"-var-file={tfvars_path}", "-out=tfplan", "-no-color"],
                cwd=workspace_path, env=aws_env, capture_output=True, text=True
            )
            
            if plan_proc.returncode != 0:
                return {"error": plan_proc.stderr}

            # Show Structured JSON
            show_json = subprocess.run(["terraform", "show", "-json", "tfplan"], 
                                       cwd=workspace_path, env=aws_env, capture_output=True, text=True)
            
            structured_plan = json.loads(show_json.stdout) if show_json.returncode == 0 else {}
            plan_json_path = os.path.join(workspace_path, "plan.json")
            with open(plan_json_path, "w") as f:
                json.dump(structured_plan, f)
                
            return {
                "raw_plan": plan_proc.stdout,
                "structured_plan": structured_plan
            }

    except Exception as e:
        return {"error": f"Orchestrator Plan Error: {str(e)}"}

    

def terraform_apply(project_id, plan_job_id, blueprint, credentials=None):
    """
    Orchestrates the transition from credentials to execution.
    """
    try:
        if not credentials or "role_arn" not in credentials:
            return {"error": "No AWS Role ARN provided for this project."}

        # 1. Assume the Role of the Tenant (Multi-tenancy)
        sts_client = boto3.client('sts')
        # Use ExternalId if provided by the user during signup
        assume_params = {
            "RoleArn": credentials["role_arn"],
            "RoleSessionName": f"CloudCrafter-Apply-{project_id[:8]}"
        }
        if credentials.get("external_id"):
            assume_params["ExternalId"] = credentials["external_id"]

        assumed_role = sts_client.assume_role(**assume_params)
        
        # 2. Extract Temporary Security Tokens
        temp_creds = {
            "AccessKeyId": assumed_role['Credentials']['AccessKeyId'],
            "SecretAccessKey": assumed_role['Credentials']['SecretAccessKey'],
            "SessionToken": assumed_role['Credentials']['SessionToken']
        }

        # 3. 🔥 FIX: Initialize Executor targeting the persistent job directory
        env = blueprint.get("environment", "development").lower()
        working_dir = get_workspace_path(project_id, plan_job_id, env)
        
        # Safety check
        if not os.path.exists(working_dir):
            return {"error": f"Workspace directory not found: {working_dir}"}

        executor = TerraformExecutor(
            working_dir=working_dir, 
            temp_aws_credentials=temp_creds
        )

        # 4. Run the Apply
        result = executor.safe_apply()
        
        if result["status"] == "FAILED":
            return {"error": result["logs"]["stderr"]}
            
        return result

    except Exception as e:
        print(f"❌ Orchestrator Error: {str(e)}")
        return {"error": str(e)}

# =====================================================
# COST ESTIMATION
# =====================================================

def terraform_cost(project_id,plan_job_id, blueprint, credentials=None):
    try:
        env = blueprint.get("environment", "development").lower()
        workspace_path = get_workspace_path(project_id, plan_job_id, env)

        plan_json_path = os.path.join(workspace_path, "plan.json")

        # Ensure structured plan exists
        if not os.path.exists(plan_json_path):
            return {"error": "plan.json not found. Run plan first."}

        aws_env = os.environ.copy()
        if credentials:
            temp_creds = assume_role(
                credentials["role_arn"],
                credentials.get("external_id")
            )
            aws_env.update(temp_creds)

        with get_env_lock(project_id, env):

            # Run Infracost breakdown
            cost_proc = subprocess.run(
                [
                    "infracost",
                    "breakdown",
                    "--path", plan_json_path,
                    "--format", "json"
                ],
                cwd=workspace_path,
                env=aws_env,
                capture_output=True,
                text=True
            )

            if cost_proc.returncode != 0:
                return {"error": cost_proc.stderr}

            cost_data = json.loads(cost_proc.stdout)

            # Extract Summary
            total_monthly = cost_data.get("totalMonthlyCost")
            currency = cost_data.get("currency", "USD")

            summary = {}
            if total_monthly is not None:
                summary = {
                    "monthly_cost": float(total_monthly),
                    "currency": currency
                }

            return {
                "cost_summary": summary,
                "raw_cost": cost_data
            }

    except Exception as e:
        return {"error": f"Orchestrator Cost Error: {str(e)}"}
    
# =====================================================
# DESTROY
# =====================================================

def terraform_destroy(project_id, blueprint, credentials=None):   
    try:
        env = blueprint.get("environment", "development").lower()
        env_folder = ENV_MAP.get(env, "dev")

        project_path = os.path.join(JOBS_BASE_DIR, project_id)

        aws_env = os.environ.copy()
        if credentials:
            temp_creds = assume_role(
                credentials["role_arn"],
                credentials.get("external_id")
            )
            aws_env.update(temp_creds)

        # Find latest workspace for this environment
        for job_id in os.listdir(project_path):
            candidate = os.path.join(
                JOBS_BASE_DIR,
                project_id,
                job_id,
                "terraform",
                "envs",
                env_folder
            )
            if os.path.exists(candidate):
                workspace_path = candidate
                break
        else:
            return {"error": "No deployed environment found."}

        lock = get_env_lock(project_id, env)

        with lock:

            destroy_proc = subprocess.run(
                ["terraform", "destroy", "-auto-approve", "-no-color"],
                cwd=workspace_path,
                env=aws_env,
                capture_output=True,
                text=True
            )

            if destroy_proc.returncode != 0:
                return {"error": destroy_proc.stderr}

            return {"message": "Environment destroyed successfully."}

    except Exception as e:
        return {"error": f"DESTROY CRASH: {str(e)}"}

# =====================================================
# STATUS
# =====================================================

def terraform_status(project_id, environment):

    env = environment.lower()
    env_folder = ENV_MAP.get(env, "dev")

    project_path = os.path.join(JOBS_BASE_DIR, project_id)

    if not os.path.exists(project_path):
        return {"status": "NOT_DEPLOYED"}

    for job_id in os.listdir(project_path):
        candidate = os.path.join(
            JOBS_BASE_DIR,
            project_id,
            job_id,
            "terraform",
            "envs",
            env_folder
        )   
        if os.path.exists(candidate):
            workspace_path = candidate
            break
    else:
        return {"status": "NOT_DEPLOYED"}

    state_file = os.path.join(workspace_path, "terraform.tfstate")

    if not os.path.exists(state_file):
        return {"status": "PLANNED"}

    try:
        state_data = json.load(open(state_file))
        resources = [
            r["type"]
            for r in state_data.get("resources", [])
        ]

        return {
            "status": "DEPLOYED",
            "resources": resources
        }

    except Exception:
        return {"status": "UNKNOWN"}