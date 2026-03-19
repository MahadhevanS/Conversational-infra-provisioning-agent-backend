# # import subprocess
# # import os
# # import json
# # import shutil
# # import platform
# # import threading
# # from backend.tfvars_generator import generate_tfvars
# # from backend.terraform.executor import TerraformExecutor
# # import boto3

# # # --- PATHS --- 
# # PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# # JOBS_BASE_DIR = os.path.join(PROJECT_ROOT, "persistent_jobs")
# # os.makedirs(JOBS_BASE_DIR, exist_ok=True)

# # # --- GLOBAL LOCKS ---
# # ENV_LOCKS = {}

# # ENV_MAP = {
# #     "production": "prod",
# #     "prod": "prod",
# #     "development": "dev",
# #     "dev": "dev"
# # }

# # def get_env_lock(project_id, env_name):
# #     key = f"{project_id}-{env_name}"
# #     if key not in ENV_LOCKS:
# #         ENV_LOCKS[key] = threading.Lock()
# #     return ENV_LOCKS[key]

# # # --- HELPERS ---
# # def assume_role(role_arn, external_id=None):
# #     sts = boto3.client(
# #         "sts",
# #         region_name="us-east-1"  # or from blueprint
# #     )

# #     assume_params = {
# #         "RoleArn": role_arn,
# #         "RoleSessionName": "CloudCrafterSession"
# #     }

# #     if external_id:
# #         assume_params["ExternalId"] = external_id

# #     response = sts.assume_role(**assume_params)

# #     creds = response["Credentials"]

# #     return {
# #         "AWS_ACCESS_KEY_ID": creds["AccessKeyId"],
# #         "AWS_SECRET_ACCESS_KEY": creds["SecretAccessKey"],
# #         "AWS_SESSION_TOKEN": creds["SessionToken"]
# #     }

# # def get_workspace_path(project_id, job_id, env_name):
# #     env_folder = "prod" if "prod" in env_name.lower() else "dev"
# #     return os.path.join(JOBS_BASE_DIR, project_id, job_id, "terraform", "envs", env_folder)

# # def find_latest_state(project_id, env_name):
# #     env_folder = "prod" if "prod" in env_name.lower() else "dev"

# #     project_path = os.path.join(JOBS_BASE_DIR, project_id)

# #     if not os.path.exists(project_path):
# #         return None

# #     job_ids = sorted(
# #         os.listdir(project_path),
# #         key=lambda x: os.path.getctime(os.path.join(project_path, x)),
# #         reverse=True
# #     )

# #     for job_id in job_ids:
# #         state_path = os.path.join(
# #             project_path,
# #             job_id,
# #             "terraform",
# #             "envs",
# #             env_folder,
# #             "terraform.tfstate"
# #         )

# #         if os.path.exists(state_path):
# #             return state_path

# #     return None

# # def format_access_points(outputs):
# #     access = []

# #     if "alb_dns_name" in outputs:
# #         access.append({
# #             "service": "Web Application",
# #             "url": f"http://{outputs['alb_dns_name']['value']}"
# #         })

# #     if "ec2_public_ip" in outputs:
# #         access.append({
# #             "service": "EC2 Server",
# #             "url": f"http://{outputs['ec2_public_ip']['value']}"
# #         })

# #     if "s3_website_url" in outputs:
# #         access.append({
# #             "service": "Static Website",
# #             "url": f"http://{outputs['s3_website_url']['value']}"
# #         })

# #     if "rds_endpoint" in outputs:
# #         access.append({
# #             "service": "Database",
# #             "endpoint": outputs["rds_endpoint"]["value"]
# #         })

# #     return access

# # # --- CORE LOGIC ---

# # def terraform_plan(project_id, blueprint, job_id, credentials):
# #     try:
# #         env = blueprint.get("environment", "development").lower()
# #         workspace_path = get_workspace_path(project_id, job_id, env)
        
# #         # 1. Prepare Directory Structure
# #         os.makedirs(workspace_path, exist_ok=True)
        
# #         # 2. Sync Source Files
# #         source_env = "prod" if "prod" in env else "dev"
# #         source_dir = os.path.join(PROJECT_ROOT, "terraform", "envs", source_env)
        
# #         for item in os.listdir(source_dir):
# #             s = os.path.join(source_dir, item)
# #             d = os.path.join(workspace_path, item)
# #             if os.path.isfile(s) and s.endswith(".tf"):
# #                 shutil.copy2(s, d)

# #         # 2. Inject Latest State (Crucial for Modify/Terminate)
# #         latest_state = find_latest_state(project_id, env)
# #         if latest_state:
# #             shutil.copy2(latest_state, os.path.join(workspace_path, "terraform.tfstate"))

# #         # 3. Handle Modules (Symlink)
# #         modules_src = os.path.join(PROJECT_ROOT, "terraform", "modules")
# #         modules_dst = os.path.join(
# #             JOBS_BASE_DIR,
# #             project_id,
# #             job_id,
# #             "terraform",
# #             "modules"
# #         )
# #         os.makedirs(os.path.dirname(modules_dst), exist_ok=True)
        
# #         if not os.path.exists(modules_dst):
# #             if platform.system() == "Windows":
# #                 subprocess.run(['cmd', '/c', 'mklink', '/j', modules_dst, modules_src], check=True)
# #             else:
# #                 os.symlink(modules_src, modules_dst)

# #         aws_env = os.environ.copy()

# #         if credentials:
# #             temp_creds = assume_role(
# #                 credentials["role_arn"],
# #                 credentials.get("external_id")
# #             )
# #             aws_env.update(temp_creds)

# #         # 4. Execute Plan with Locking
# #         with get_env_lock(project_id, env):
# #             tfvars_path = generate_tfvars(blueprint, workspace_path)
            
# #             # Init
# #             subprocess.run(["terraform", "init", "-no-color", "-input=false"], 
# #                            cwd=workspace_path, env=aws_env, capture_output=True, check=True)
            
# #             # Plan
# #             plan_proc = subprocess.run(
# #                 ["terraform", "plan", f"-var-file={tfvars_path}", "-out=tfplan", "-no-color"],
# #                 cwd=workspace_path, env=aws_env, capture_output=True, text=True
# #             )
            
# #             if plan_proc.returncode != 0:
# #                 return {"error": plan_proc.stderr}

# #             # Show Structured JSON
# #             show_json = subprocess.run(["terraform", "show", "-json", "tfplan"], 
# #                                        cwd=workspace_path, env=aws_env, capture_output=True, text=True)
            
# #             structured_plan = json.loads(show_json.stdout) if show_json.returncode == 0 else {}
# #             plan_json_path = os.path.join(workspace_path, "plan.json")
# #             with open(plan_json_path, "w") as f:
# #                 json.dump(structured_plan, f)
                
# #             return {
# #                 "raw_plan": plan_proc.stdout,
# #                 "structured_plan": structured_plan
# #             }

# #     except Exception as e:
# #         return {"error": f"Orchestrator Plan Error: {str(e)}"}

    

# # def terraform_apply(project_id, plan_job_id, blueprint, credentials=None):
# #     """
# #     Orchestrates the transition from credentials to execution.
# #     """
# #     try:
# #         if not credentials or "role_arn" not in credentials:
# #             return {"error": "No AWS Role ARN provided for this project."}

# #         # 1. Assume the Role of the Tenant (Multi-tenancy)
# #         sts_client = boto3.client('sts')
# #         # Use ExternalId if provided by the user during signup
# #         assume_params = {
# #             "RoleArn": credentials["role_arn"],
# #             "RoleSessionName": f"CloudCrafter-Apply-{project_id[:8]}"
# #         }
# #         if credentials.get("external_id"):
# #             assume_params["ExternalId"] = credentials["external_id"]

# #         assumed_role = sts_client.assume_role(**assume_params)
        
# #         # 2. Extract Temporary Security Tokens
# #         temp_creds = {
# #             "AccessKeyId": assumed_role['Credentials']['AccessKeyId'],
# #             "SecretAccessKey": assumed_role['Credentials']['SecretAccessKey'],
# #             "SessionToken": assumed_role['Credentials']['SessionToken']
# #         }

# #         # 3. 🔥 FIX: Initialize Executor targeting the persistent job directory
# #         env = blueprint.get("environment", "development").lower()
# #         working_dir = get_workspace_path(project_id, plan_job_id, env)
        
# #         # Safety check
# #         if not os.path.exists(working_dir):
# #             return {"error": f"Workspace directory not found: {working_dir}"}

# #         executor = TerraformExecutor(
# #             working_dir=working_dir, 
# #             temp_aws_credentials=temp_creds
# #         )

# #         # 4. Run the Apply
# #         result = executor.safe_apply()
        
# #         if result["status"] == "FAILED":
# #             return {"error": result["logs"]["stderr"]}
            
# #         return result

# #     except Exception as e:
# #         print(f"❌ Orchestrator Error: {str(e)}")
# #         return {"error": str(e)}

# # # =====================================================
# # # COST ESTIMATION
# # # =====================================================

# # def terraform_cost(project_id,plan_job_id, blueprint, credentials=None):
# #     try:
# #         env = blueprint.get("environment", "development").lower()
# #         workspace_path = get_workspace_path(project_id, plan_job_id, env)

# #         plan_json_path = os.path.join(workspace_path, "plan.json")

# #         # Ensure structured plan exists
# #         if not os.path.exists(plan_json_path):
# #             return {"error": "plan.json not found. Run plan first."}

# #         aws_env = os.environ.copy()
# #         if credentials:
# #             temp_creds = assume_role(
# #                 credentials["role_arn"],
# #                 credentials.get("external_id")
# #             )
# #             aws_env.update(temp_creds)

# #         with get_env_lock(project_id, env):

# #             # Run Infracost breakdown
# #             cost_proc = subprocess.run(
# #                 [
# #                     "infracost",
# #                     "breakdown",
# #                     "--path", plan_json_path,
# #                     "--format", "json"
# #                 ],
# #                 cwd=workspace_path,
# #                 env=aws_env,
# #                 capture_output=True,
# #                 text=True
# #             )

# #             if cost_proc.returncode != 0:
# #                 return {"error": cost_proc.stderr}

# #             cost_data = json.loads(cost_proc.stdout)

# #             # Extract Summary
# #             total_monthly = cost_data.get("totalMonthlyCost")
# #             currency = cost_data.get("currency", "USD")

# #             summary = {}
# #             if total_monthly is not None:
# #                 summary = {
# #                     "monthly_cost": float(total_monthly),
# #                     "currency": currency
# #                 }

# #             return {
# #                 "cost_summary": summary,
# #                 "raw_cost": cost_data
# #             }

# #     except Exception as e:
# #         return {"error": f"Orchestrator Cost Error: {str(e)}"}
    
# # # =====================================================
# # # DESTROY
# # # =====================================================

# # def terraform_destroy(project_id, blueprint, credentials=None):   
# #     try:
# #         env = blueprint.get("environment", "development").lower()
# #         env_folder = ENV_MAP.get(env, "dev")

# #         project_path = os.path.join(JOBS_BASE_DIR, project_id)

# #         aws_env = os.environ.copy()
# #         if credentials:
# #             temp_creds = assume_role(
# #                 credentials["role_arn"],
# #                 credentials.get("external_id")
# #             )
# #             aws_env.update(temp_creds)

# #         # Find latest workspace for this environment
# #         for job_id in os.listdir(project_path):
# #             candidate = os.path.join(
# #                 JOBS_BASE_DIR,
# #                 project_id,
# #                 job_id,
# #                 "terraform",
# #                 "envs",
# #                 env_folder
# #             )
# #             if os.path.exists(candidate):
# #                 workspace_path = candidate
# #                 break
# #         else:
# #             return {"error": "No deployed environment found."}

# #         lock = get_env_lock(project_id, env)

# #         with lock:

# #             destroy_proc = subprocess.run(
# #                 ["terraform", "destroy", "-auto-approve", "-no-color"],
# #                 cwd=workspace_path,
# #                 env=aws_env,
# #                 capture_output=True,
# #                 text=True
# #             )

# #             if destroy_proc.returncode != 0:
# #                 return {"error": destroy_proc.stderr}

# #             return {"message": "Environment destroyed successfully."}

# #     except Exception as e:
# #         return {"error": f"DESTROY CRASH: {str(e)}"}

# # # =====================================================
# # # STATUS
# # # =====================================================

# # def terraform_status(project_id, environment):

# #     env = environment.lower()
# #     env_folder = ENV_MAP.get(env, "dev")

# #     project_path = os.path.join(JOBS_BASE_DIR, project_id)

# #     if not os.path.exists(project_path):
# #         return {"status": "NOT_DEPLOYED"}

# #     for job_id in os.listdir(project_path):
# #         candidate = os.path.join(
# #             JOBS_BASE_DIR,
# #             project_id,
# #             job_id,
# #             "terraform",
# #             "envs",
# #             env_folder
# #         )   
# #         if os.path.exists(candidate):
# #             workspace_path = candidate
# #             break
# #     else:
# #         return {"status": "NOT_DEPLOYED"}

# #     state_file = os.path.join(workspace_path, "terraform.tfstate")

# #     if not os.path.exists(state_file):
# #         return {"status": "PLANNED"}

# #     try:
# #         state_data = json.load(open(state_file))
# #         resources = [
# #             r["type"]
# #             for r in state_data.get("resources", [])
# #         ]

# #         return {
# #             "status": "DEPLOYED",
# #             "resources": resources
# #         }

# #     except Exception:
# #         return {"status": "UNKNOWN"}


# import subprocess
# import os
# import json
# import shutil
# import platform
# import threading
# from backend.tfvars_generator import generate_tfvars
# from backend.terraform.executor import TerraformExecutor
# from backend.db import supabase
# import boto3

# # --- PATHS ---
# PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# JOBS_BASE_DIR = os.path.join(PROJECT_ROOT, "persistent_jobs")
# os.makedirs(JOBS_BASE_DIR, exist_ok=True)

# # --- GLOBAL LOCKS ---
# ENV_LOCKS = {}

# ENV_MAP = {
#     "production": "prod",
#     "prod": "prod",
#     "development": "dev",
#     "dev": "dev"
# }

# def get_env_lock(project_id, env_name):
#     key = f"{project_id}-{env_name}"
#     if key not in ENV_LOCKS:
#         ENV_LOCKS[key] = threading.Lock()
#     return ENV_LOCKS[key]

# # =========================================================
# # LOG HELPERS
# # =========================================================

# # Batch size — flush to Supabase after this many lines.
# # Keeps the panel feeling live without hammering the DB with one insert per line.
# LOG_BATCH_SIZE = 10

# def _flush_batch(batch):
#     """Insert a list of log row dicts to Supabase in one request."""
#     if not batch:
#         return
#     try:
#         supabase.table("job_logs").insert(batch).execute()
#         print(f"✅ Flushed {len(batch)} log rows to Supabase")
#     except Exception as e:
#         print(f"⚠️  Log flush failed: {e}")


# def save_logs(job_id, stage, stdout="", stderr=""):
#     """
#     Persist a captured stdout/stderr blob to job_logs in one insert.
#     Used for non-streaming paths (show, cost) and as a safety-net bulk save.
#     """
#     rows = []
#     if stdout and stdout.strip():
#         rows.append({
#             "job_id": job_id, "stage": stage,
#             "stream": "stdout", "log_text": stdout.strip()
#         })
#     if stderr and stderr.strip():
#         rows.append({
#             "job_id": job_id, "stage": stage,
#             "stream": "stderr", "log_text": stderr.strip()
#         })
#     _flush_batch(rows)


# def run_streaming(command, cwd, env, job_id, stage):
#     """
#     Run a subprocess via Popen, collect output line by line, and flush to
#     job_logs in batches of LOG_BATCH_SIZE lines.  A final flush after the
#     process exits ensures no lines are lost.

#     Returns {"stdout": str, "stderr": str, "returncode": int}.
#     """
#     stdout_lines = []
#     stderr_lines = []

#     proc = subprocess.Popen(
#         command,
#         cwd=cwd,
#         env=env,
#         stdout=subprocess.PIPE,
#         stderr=subprocess.PIPE,
#         text=True,
#         bufsize=1
#     )

#     def read_stream(pipe, stream_name, collector):
#         batch = []
#         for raw_line in pipe:
#             line = raw_line.rstrip("\n").rstrip("\r")
#             collector.append(raw_line)
#             if not line.strip():
#                 continue
#             batch.append({
#                 "job_id": job_id, "stage": stage,
#                 "stream": stream_name, "log_text": line
#             })
#             if len(batch) >= LOG_BATCH_SIZE:
#                 _flush_batch(batch)
#                 batch = []
#         # Flush any remaining lines
#         _flush_batch(batch)
#         pipe.close()

#     t_out = threading.Thread(
#         target=read_stream, args=(proc.stdout, "stdout", stdout_lines), daemon=True
#     )
#     t_err = threading.Thread(
#         target=read_stream, args=(proc.stderr, "stderr", stderr_lines), daemon=True
#     )
#     t_out.start()
#     t_err.start()
#     t_out.join()
#     t_err.join()
#     proc.wait()

#     return {
#         "stdout": "".join(stdout_lines),
#         "stderr": "".join(stderr_lines),
#         "returncode": proc.returncode
#     }

# # =========================================================
# # HELPERS
# # =========================================================

# def assume_role(role_arn, external_id=None):
#     sts = boto3.client("sts", region_name="us-east-1")
#     assume_params = {
#         "RoleArn": role_arn,
#         "RoleSessionName": "CloudCrafterSession"
#     }
#     if external_id:
#         assume_params["ExternalId"] = external_id
#     response = sts.assume_role(**assume_params)
#     creds = response["Credentials"]
#     return {
#         "AWS_ACCESS_KEY_ID": creds["AccessKeyId"],
#         "AWS_SECRET_ACCESS_KEY": creds["SecretAccessKey"],
#         "AWS_SESSION_TOKEN": creds["SessionToken"]
#     }

# def get_workspace_path(project_id, job_id, env_name):
#     env_folder = "prod" if "prod" in env_name.lower() else "dev"
#     return os.path.join(JOBS_BASE_DIR, project_id, job_id, "terraform", "envs", env_folder)

# def find_latest_state(project_id, env_name):
#     env_folder = "prod" if "prod" in env_name.lower() else "dev"
#     project_path = os.path.join(JOBS_BASE_DIR, project_id)
#     if not os.path.exists(project_path):
#         return None
#     job_ids = sorted(
#         os.listdir(project_path),
#         key=lambda x: os.path.getctime(os.path.join(project_path, x)),
#         reverse=True
#     )
#     for job_id in job_ids:
#         state_path = os.path.join(
#             project_path, job_id, "terraform", "envs", env_folder, "terraform.tfstate"
#         )
#         if os.path.exists(state_path):
#             return state_path
#     return None

# def format_access_points(outputs):
#     access = []
#     if "alb_dns_name" in outputs:
#         access.append({"service": "Web Application", "url": f"http://{outputs['alb_dns_name']['value']}"})
#     if "ec2_public_ip" in outputs:
#         access.append({"service": "EC2 Server", "url": f"http://{outputs['ec2_public_ip']['value']}"})
#     if "s3_website_url" in outputs:
#         access.append({"service": "Static Website", "url": f"http://{outputs['s3_website_url']['value']}"})
#     if "rds_endpoint" in outputs:
#         access.append({"service": "Database", "endpoint": outputs["rds_endpoint"]["value"]})
#     return access

# # =========================================================
# # PLAN
# # =========================================================

# def terraform_plan(project_id, blueprint, job_id, credentials):
#     try:
#         env = blueprint.get("environment", "development").lower()
#         workspace_path = get_workspace_path(project_id, job_id, env)
#         os.makedirs(workspace_path, exist_ok=True)

#         # Sync source .tf files
#         source_env = "prod" if "prod" in env else "dev"
#         source_dir = os.path.join(PROJECT_ROOT, "terraform", "envs", source_env)
#         for item in os.listdir(source_dir):
#             s = os.path.join(source_dir, item)
#             d = os.path.join(workspace_path, item)
#             if os.path.isfile(s) and s.endswith(".tf"):
#                 shutil.copy2(s, d)

#         # Inject latest state
#         latest_state = find_latest_state(project_id, env)
#         if latest_state:
#             shutil.copy2(latest_state, os.path.join(workspace_path, "terraform.tfstate"))

#         # Symlink modules
#         modules_src = os.path.join(PROJECT_ROOT, "terraform", "modules")
#         modules_dst = os.path.join(JOBS_BASE_DIR, project_id, job_id, "terraform", "modules")
#         os.makedirs(os.path.dirname(modules_dst), exist_ok=True)
#         if not os.path.exists(modules_dst):
#             if platform.system() == "Windows":
#                 subprocess.run(['cmd', '/c', 'mklink', '/j', modules_dst, modules_src], check=True)
#             else:
#                 os.symlink(modules_src, modules_dst)

#         aws_env = os.environ.copy()
#         if credentials:
#             temp_creds = assume_role(credentials["role_arn"], credentials.get("external_id"))
#             aws_env.update(temp_creds)

#         with get_env_lock(project_id, env):
#             tfvars_path = generate_tfvars(blueprint, workspace_path)

#             # INIT — streamed line by line to job_logs
#             init_proc = run_streaming(
#                 ["terraform", "init", "-no-color", "-input=false"],
#                 cwd=workspace_path, env=aws_env, job_id=job_id, stage="init"
#             )
#             if init_proc["returncode"] != 0:
#                 return {"error": init_proc["stderr"]}

#             # PLAN — streamed line by line to job_logs
#             plan_proc = run_streaming(
#                 ["terraform", "plan", f"-var-file={tfvars_path}", "-out=tfplan", "-no-color"],
#                 cwd=workspace_path, env=aws_env, job_id=job_id, stage="plan"
#             )
#             if plan_proc["returncode"] != 0:
#                 return {"error": plan_proc["stderr"]}

#             # SHOW — capture only (stdout is JSON, not streamed)
#             show_proc = subprocess.run(
#                 ["terraform", "show", "-json", "tfplan"],
#                 cwd=workspace_path, env=aws_env, capture_output=True, text=True
#             )
#             save_logs(job_id, "show", "", show_proc.stderr)

#             structured_plan = json.loads(show_proc.stdout) if show_proc.returncode == 0 else {}
#             plan_json_path = os.path.join(workspace_path, "plan.json")
#             with open(plan_json_path, "w") as f:
#                 json.dump(structured_plan, f)

#             return {"raw_plan": plan_proc["stdout"], "structured_plan": structured_plan}

#     except Exception as e:
#         save_logs(job_id, "error", "", f"Orchestrator Plan Error: {str(e)}")
#         return {"error": f"Orchestrator Plan Error: {str(e)}"}

# # =========================================================
# # TERRAFORM EXECUTOR WITH LOG CAPTURE
# # Thin wrapper around TerraformExecutor that saves stdout/stderr
# # for every subprocess stage to the job_logs table.
# # We wrap rather than subclass to avoid __init__ signature conflicts.
# # =========================================================

# class TerraformExecutorWithLogs:
#     """
#     Replacement for TerraformExecutor that streams every subprocess line
#     to job_logs in real time via run_streaming(), rather than capturing
#     all output and saving it only after the process finishes.
#     """
#     def __init__(self, working_dir, temp_aws_credentials=None, job_id=None):
#         self.working_dir = working_dir
#         self.temp_aws_credentials = temp_aws_credentials
#         self.job_id = job_id
#         # Build the environment once so every stage uses it
#         self._env = os.environ.copy()
#         if temp_aws_credentials:
#             self._env["AWS_ACCESS_KEY_ID"]     = temp_aws_credentials["AccessKeyId"]
#             self._env["AWS_SECRET_ACCESS_KEY"] = temp_aws_credentials["SecretAccessKey"]
#             self._env["AWS_SESSION_TOKEN"]     = temp_aws_credentials["SessionToken"]
#             self._env["AWS_PROFILE"]           = ""

#     def _stage_from_command(self, command):
#         for stage in ["init", "plan", "apply", "show", "destroy"]:
#             if stage in command:
#                 return stage
#         return "terraform"

#     def run(self, command):
#         """
#         Stream command output line by line to job_logs, then return
#         the same dict shape as TerraformExecutor.run() for compatibility.
#         """
#         # Block destroy just like the original executor does
#         if "destroy" in command:
#             raise Exception("terraform destroy is disabled via executor policy.")

#         stage = self._stage_from_command(command)
#         result = run_streaming(command, cwd=self.working_dir, env=self._env,
#                                job_id=self.job_id, stage=stage)
#         return {
#             "stdout":    result["stdout"],
#             "stderr":    result["stderr"],
#             "exit_code": result["returncode"]
#         }

#     def safe_apply(self):
#         from backend.terraform.plan_parser import is_plan_safe

#         # 1. INIT
#         init = self.run(["terraform", "init", "-no-color", "-input=false"])
#         if init["exit_code"] != 0:
#             return {"status": "FAILED", "stage": "init", "logs": init}

#         # 2. PLAN (save to file for apply step)
#         plan = self.run(["terraform", "plan", "-no-color", "-out=tfplan"])
#         if plan["exit_code"] != 0:
#             return {"status": "FAILED", "stage": "plan", "logs": plan}

#         # 3. SAFETY CHECK
#         if not is_plan_safe(self.working_dir):
#             return {"status": "BLOCKED", "reason": "Destructive changes detected"}

#         # 4. APPLY
#         apply = self.run(["terraform", "apply", "-no-color", "tfplan"])
#         if apply["exit_code"] != 0:
#             return {"status": "FAILED", "stage": "apply", "logs": apply}

#         # 5. Read outputs
#         output_proc = subprocess.run(
#             ["terraform", "output", "-json"],
#             cwd=self.working_dir, env=self._env,
#             capture_output=True, text=True
#         )
#         try:
#             outputs = json.loads(output_proc.stdout) if output_proc.returncode == 0 else {}
#         except Exception:
#             outputs = {}

#         access = format_access_points(outputs)
#         return {"status": "SUCCESS", "logs": apply, "outputs": outputs, "access": access}


# # =========================================================
# # APPLY
# # =========================================================

# def terraform_apply(project_id, plan_job_id, blueprint, credentials=None):
#     try:
#         if not credentials or "role_arn" not in credentials:
#             return {"error": "No AWS Role ARN provided for this project."}

#         sts_client = boto3.client("sts")
#         assume_params = {
#             "RoleArn": credentials["role_arn"],
#             "RoleSessionName": f"CloudCrafter-Apply-{project_id[:8]}"
#         }
#         if credentials.get("external_id"):
#             assume_params["ExternalId"] = credentials["external_id"]

#         assumed_role = sts_client.assume_role(**assume_params)
#         temp_creds = {
#             "AccessKeyId": assumed_role["Credentials"]["AccessKeyId"],
#             "SecretAccessKey": assumed_role["Credentials"]["SecretAccessKey"],
#             "SessionToken": assumed_role["Credentials"]["SessionToken"]
#         }

#         env = blueprint.get("environment", "development").lower()
#         working_dir = get_workspace_path(project_id, plan_job_id, env)

#         if not os.path.exists(working_dir):
#             return {"error": f"Workspace directory not found: {working_dir}"}

#         # Use logging-aware subclass so every stage is saved to job_logs
#         executor = TerraformExecutorWithLogs(
#             working_dir=working_dir,
#             temp_aws_credentials=temp_creds,
#             job_id=plan_job_id
#         )

#         result = executor.safe_apply()

#         if result["status"] == "FAILED":
#             return {"error": result["logs"].get("stderr", str(result.get("logs", "Apply failed.")))}

#         return result

#     except Exception as e:
#         save_logs(plan_job_id, "error", "", f"Orchestrator Apply Error: {str(e)}")
#         print(f"Orchestrator Error: {str(e)}")
#         return {"error": str(e)}

# # =========================================================
# # COST
# # =========================================================

# def terraform_cost(project_id, plan_job_id, blueprint, credentials=None):
#     try:
#         env = blueprint.get("environment", "development").lower()
#         workspace_path = get_workspace_path(project_id, plan_job_id, env)
#         plan_json_path = os.path.join(workspace_path, "plan.json")

#         if not os.path.exists(plan_json_path):
#             return {"error": "plan.json not found. Run plan first."}

#         aws_env = os.environ.copy()
#         if credentials:
#             temp_creds = assume_role(credentials["role_arn"], credentials.get("external_id"))
#             aws_env.update(temp_creds)

#         with get_env_lock(project_id, env):
#             cost_proc = subprocess.run(
#                 ["infracost", "breakdown", "--path", plan_json_path, "--format", "json"],
#                 cwd=workspace_path, env=aws_env, capture_output=True, text=True
#             )
#             save_logs(plan_job_id, "cost", "", cost_proc.stderr)  # stdout is JSON

#             if cost_proc.returncode != 0:
#                 return {"error": cost_proc.stderr}

#             cost_data = json.loads(cost_proc.stdout)
#             total_monthly = cost_data.get("totalMonthlyCost")
#             currency = cost_data.get("currency", "USD")
#             summary = {}
#             if total_monthly is not None:
#                 summary = {"monthly_cost": float(total_monthly), "currency": currency}

#             return {"cost_summary": summary, "raw_cost": cost_data}

#     except Exception as e:
#         save_logs(plan_job_id, "error", "", f"Orchestrator Cost Error: {str(e)}")
#         return {"error": f"Orchestrator Cost Error: {str(e)}"}

# # =========================================================
# # DESTROY
# # =========================================================

# def terraform_destroy(project_id, blueprint, credentials=None, job_id=None):
#     try:
#         env = blueprint.get("environment", "development").lower()
#         env_folder = ENV_MAP.get(env, "dev")
#         project_path = os.path.join(JOBS_BASE_DIR, project_id)

#         aws_env = os.environ.copy()
#         if credentials:
#             temp_creds = assume_role(credentials["role_arn"], credentials.get("external_id"))
#             aws_env.update(temp_creds)

#         # Find latest workspace for this environment
#         workspace_path = None
#         for jid in os.listdir(project_path):
#             candidate = os.path.join(
#                 JOBS_BASE_DIR, project_id, jid, "terraform", "envs", env_folder
#             )
#             if os.path.exists(candidate):
#                 workspace_path = candidate
#                 break

#         if not workspace_path:
#             return {"error": "No deployed environment found."}

#         log_job_id = job_id or f"destroy-unknown-{project_id}"

#         with get_env_lock(project_id, env):
#             destroy_proc = run_streaming(
#                 ["terraform", "destroy", "-auto-approve", "-no-color"],
#                 cwd=workspace_path, env=aws_env, job_id=log_job_id, stage="destroy"
#             )

#             if destroy_proc["returncode"] != 0:
#                 return {"error": destroy_proc["stderr"]}

#             return {"message": "Environment destroyed successfully."}

#     except Exception as e:
#         if job_id:
#             save_logs(job_id, "error", "", f"DESTROY CRASH: {str(e)}")
#         return {"error": f"DESTROY CRASH: {str(e)}"}

# # =========================================================
# # STATUS
# # =========================================================

# def terraform_status(project_id, environment):
#     env = environment.lower()
#     env_folder = ENV_MAP.get(env, "dev")
#     project_path = os.path.join(JOBS_BASE_DIR, project_id)

#     if not os.path.exists(project_path):
#         return {"status": "NOT_DEPLOYED"}

#     workspace_path = None
#     for job_id in os.listdir(project_path):
#         candidate = os.path.join(
#             JOBS_BASE_DIR, project_id, job_id, "terraform", "envs", env_folder
#         )
#         if os.path.exists(candidate):
#             workspace_path = candidate
#             break

#     if not workspace_path:
#         return {"status": "NOT_DEPLOYED"}

#     state_file = os.path.join(workspace_path, "terraform.tfstate")
#     if not os.path.exists(state_file):
#         return {"status": "PLANNED"}

#     try:
#         state_data = json.load(open(state_file))
#         resources = [r["type"] for r in state_data.get("resources", [])]
#         return {"status": "DEPLOYED", "resources": resources}
#     except Exception:
#         return {"status": "UNKNOWN"}

import subprocess
import os
import json
import shutil
import platform
import threading
from backend.tfvars_generator import generate_tfvars
from backend.terraform.executor import TerraformExecutor
from backend.db import supabase
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

# =========================================================
# LOG HELPERS  — store logs as JSONB on the jobs row itself.
#
# Schema addition required (one-time migration):
#   ALTER TABLE jobs ADD COLUMN IF NOT EXISTS log_chunks JSONB DEFAULT '[]';
#
# Each element of log_chunks is:
#   { "stage": "init"|"plan"|"apply"|"destroy"|...,
#     "stream": "stdout"|"stderr",
#     "text": "<accumulated lines>" }
#
# We keep one entry per (stage, stream) pair and append lines to it.
# A single upsert writes the whole array back — zero extra DB rows.
# =========================================================

# In-memory accumulator: { job_id: [ {stage, stream, text} ] }
_log_state: dict[str, list[dict]] = {}
_log_lock = threading.Lock()

# Flush to Supabase after this many lines (lower = more responsive; higher = fewer writes)
LOG_FLUSH_LINES = 15


def _get_or_create_chunk(job_id: str, stage: str, stream: str) -> dict:
    """Return the existing chunk for (stage, stream), or create it."""
    chunks = _log_state.setdefault(job_id, [])
    for chunk in chunks:
        if chunk["stage"] == stage and chunk["stream"] == stream:
            return chunk
    chunk = {"stage": stage, "stream": stream, "text": ""}
    chunks.append(chunk)
    return chunk


def _flush_job_logs(job_id: str):
    """Write the current in-memory log state to jobs.log_chunks in Supabase."""
    chunks = _log_state.get(job_id)
    if not chunks:
        return
    try:
        supabase.table("jobs") \
            .update({"log_chunks": chunks}) \
            .eq("job_id", job_id) \
            .execute()
    except Exception as e:
        print(f"⚠️  Log flush failed for {job_id}: {e}")


def save_logs(job_id: str, stage: str, stdout: str = "", stderr: str = ""):
    """Append a captured stdout/stderr blob and flush immediately."""
    if not job_id:
        return
    with _log_lock:
        if stdout and stdout.strip():
            chunk = _get_or_create_chunk(job_id, stage, "stdout")
            chunk["text"] += stdout.rstrip("\n") + "\n"
        if stderr and stderr.strip():
            chunk = _get_or_create_chunk(job_id, stage, "stderr")
            chunk["text"] += stderr.rstrip("\n") + "\n"
    _flush_job_logs(job_id)


def run_streaming(command, cwd, env, job_id: str, stage: str):
    """
    Run a subprocess via Popen, accumulate output line by line, and flush to
    jobs.log_chunks every LOG_FLUSH_LINES lines.  A final flush after the
    process exits ensures no lines are lost.

    Returns {"stdout": str, "stderr": str, "returncode": int}.
    """
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1
    )

    def read_stream(pipe, stream_name: str, collector: list[str]):
        pending = 0
        for raw_line in pipe:
            line = raw_line.rstrip("\n").rstrip("\r")
            collector.append(raw_line)
            with _log_lock:
                chunk = _get_or_create_chunk(job_id, stage, stream_name)
                chunk["text"] += line + "\n"
                pending += 1
            if pending >= LOG_FLUSH_LINES:
                _flush_job_logs(job_id)
                pending = 0
        # Final flush for this stream
        _flush_job_logs(job_id)
        pipe.close()

    t_out = threading.Thread(
        target=read_stream, args=(proc.stdout, "stdout", stdout_lines), daemon=True
    )
    t_err = threading.Thread(
        target=read_stream, args=(proc.stderr, "stderr", stderr_lines), daemon=True
    )
    t_out.start()
    t_err.start()
    t_out.join()
    t_err.join()
    proc.wait()

    return {
        "stdout": "".join(stdout_lines),
        "stderr": "".join(stderr_lines),
        "returncode": proc.returncode
    }

# =========================================================
# HELPERS
# =========================================================

def assume_role(role_arn, external_id=None):
    sts = boto3.client("sts", region_name="us-east-1")
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
            project_path, job_id, "terraform", "envs", env_folder, "terraform.tfstate"
        )
        if os.path.exists(state_path):
            return state_path
    return None

def format_access_points(outputs):
    access = []
    if "alb_dns_name" in outputs:
        access.append({"service": "Web Application", "url": f"http://{outputs['alb_dns_name']['value']}"})
    if "ec2_public_ip" in outputs:
        access.append({"service": "EC2 Server", "url": f"http://{outputs['ec2_public_ip']['value']}"})
    if "s3_website_url" in outputs:
        access.append({"service": "Static Website", "url": f"http://{outputs['s3_website_url']['value']}"})
    if "rds_endpoint" in outputs:
        access.append({"service": "Database", "endpoint": outputs["rds_endpoint"]["value"]})
    return access

# =========================================================
# PLAN
# =========================================================

def terraform_plan(project_id, blueprint, job_id, credentials):
    try:
        env = blueprint.get("environment", "development").lower()
        workspace_path = get_workspace_path(project_id, job_id, env)
        os.makedirs(workspace_path, exist_ok=True)

        # Sync source .tf files
        source_env = "prod" if "prod" in env else "dev"
        source_dir = os.path.join(PROJECT_ROOT, "terraform", "envs", source_env)
        for item in os.listdir(source_dir):
            s = os.path.join(source_dir, item)
            d = os.path.join(workspace_path, item)
            if os.path.isfile(s) and s.endswith(".tf"):
                shutil.copy2(s, d)

        # Inject latest state
        latest_state = find_latest_state(project_id, env)
        if latest_state:
            shutil.copy2(latest_state, os.path.join(workspace_path, "terraform.tfstate"))

        # Symlink modules
        modules_src = os.path.join(PROJECT_ROOT, "terraform", "modules")
        modules_dst = os.path.join(JOBS_BASE_DIR, project_id, job_id, "terraform", "modules")
        os.makedirs(os.path.dirname(modules_dst), exist_ok=True)
        if not os.path.exists(modules_dst):
            if platform.system() == "Windows":
                subprocess.run(['cmd', '/c', 'mklink', '/j', modules_dst, modules_src], check=True)
            else:
                os.symlink(modules_src, modules_dst)

        aws_env = os.environ.copy()
        if credentials:
            temp_creds = assume_role(credentials["role_arn"], credentials.get("external_id"))
            aws_env.update(temp_creds)

        with get_env_lock(project_id, env):
            tfvars_path = generate_tfvars(blueprint, workspace_path)

            # INIT
            init_proc = run_streaming(
                ["terraform", "init", "-no-color", "-input=false"],
                cwd=workspace_path, env=aws_env, job_id=job_id, stage="init"
            )
            if init_proc["returncode"] != 0:
                return {"error": init_proc["stderr"]}

            # PLAN
            plan_proc = run_streaming(
                ["terraform", "plan", f"-var-file={tfvars_path}", "-out=tfplan", "-no-color"],
                cwd=workspace_path, env=aws_env, job_id=job_id, stage="plan"
            )
            if plan_proc["returncode"] != 0:
                return {"error": plan_proc["stderr"]}

            # SHOW — stdout is JSON so we don't stream it; capture stderr only
            show_proc = subprocess.run(
                ["terraform", "show", "-json", "tfplan"],
                cwd=workspace_path, env=aws_env, capture_output=True, text=True
            )
            if show_proc.stderr.strip():
                save_logs(job_id, "show", stderr=show_proc.stderr)

            structured_plan = json.loads(show_proc.stdout) if show_proc.returncode == 0 else {}
            plan_json_path = os.path.join(workspace_path, "plan.json")
            with open(plan_json_path, "w") as f:
                json.dump(structured_plan, f)

            return {"raw_plan": plan_proc["stdout"], "structured_plan": structured_plan}

    except Exception as e:
        save_logs(job_id, "error", stderr=f"Orchestrator Plan Error: {str(e)}")
        return {"error": f"Orchestrator Plan Error: {str(e)}"}

# =========================================================
# TERRAFORM EXECUTOR WITH LOG CAPTURE
# =========================================================

class TerraformExecutorWithLogs:
    """
    Drop-in replacement for TerraformExecutor that streams every subprocess
    line to jobs.log_chunks via run_streaming().

    IMPORTANT: job_id must be the apply/destroy job's own id, NOT the plan_job_id.
    """
    def __init__(self, working_dir, temp_aws_credentials=None, job_id=None):
        self.working_dir = working_dir
        self.temp_aws_credentials = temp_aws_credentials
        self.job_id = job_id
        self._env = os.environ.copy()
        if temp_aws_credentials:
            self._env["AWS_ACCESS_KEY_ID"]     = temp_aws_credentials["AccessKeyId"]
            self._env["AWS_SECRET_ACCESS_KEY"] = temp_aws_credentials["SecretAccessKey"]
            self._env["AWS_SESSION_TOKEN"]     = temp_aws_credentials["SessionToken"]
            self._env["AWS_PROFILE"]           = ""

    def _stage_from_command(self, command):
        for stage in ["init", "plan", "apply", "show", "destroy"]:
            if stage in command:
                return stage
        return "terraform"

    def run(self, command):
        if "destroy" in command:
            raise Exception("terraform destroy is disabled via executor policy.")
        stage = self._stage_from_command(command)
        result = run_streaming(command, cwd=self.working_dir, env=self._env,
                               job_id=self.job_id, stage=stage)
        return {
            "stdout":    result["stdout"],
            "stderr":    result["stderr"],
            "exit_code": result["returncode"]
        }

    def safe_apply(self):
        from backend.terraform.plan_parser import is_plan_safe

        init = self.run(["terraform", "init", "-no-color", "-input=false"])
        if init["exit_code"] != 0:
            return {"status": "FAILED", "stage": "init", "logs": init}

        plan = self.run(["terraform", "plan", "-no-color", "-out=tfplan"])
        if plan["exit_code"] != 0:
            return {"status": "FAILED", "stage": "plan", "logs": plan}

        if not is_plan_safe(self.working_dir):
            return {"status": "BLOCKED", "reason": "Destructive changes detected"}

        apply = self.run(["terraform", "apply", "-no-color", "tfplan"])
        if apply["exit_code"] != 0:
            return {"status": "FAILED", "stage": "apply", "logs": apply}

        output_proc = subprocess.run(
            ["terraform", "output", "-json"],
            cwd=self.working_dir, env=self._env,
            capture_output=True, text=True
        )
        try:
            outputs = json.loads(output_proc.stdout) if output_proc.returncode == 0 else {}
        except Exception:
            outputs = {}

        access = format_access_points(outputs)
        return {"status": "SUCCESS", "logs": apply, "outputs": outputs, "access": access}


# =========================================================
# APPLY  — logs written against apply_job_id (the apply job's OWN id)
# =========================================================

def terraform_apply(project_id, plan_job_id, blueprint, credentials=None, apply_job_id=None):
    """
    apply_job_id is the id of the APPLY job (set by the caller in main.py).
    Logs are stored against apply_job_id so the frontend can retrieve them.
    """
    log_id = apply_job_id or plan_job_id   # fallback keeps old behaviour if caller omits it

    try:
        if not credentials or "role_arn" not in credentials:
            return {"error": "No AWS Role ARN provided for this project."}

        sts_client = boto3.client("sts")
        assume_params = {
            "RoleArn": credentials["role_arn"],
            "RoleSessionName": f"CloudCrafter-Apply-{project_id[:8]}"
        }
        if credentials.get("external_id"):
            assume_params["ExternalId"] = credentials["external_id"]

        assumed_role = sts_client.assume_role(**assume_params)
        temp_creds = {
            "AccessKeyId": assumed_role["Credentials"]["AccessKeyId"],
            "SecretAccessKey": assumed_role["Credentials"]["SecretAccessKey"],
            "SessionToken": assumed_role["Credentials"]["SessionToken"]
        }

        env = blueprint.get("environment", "development").lower()
        working_dir = get_workspace_path(project_id, plan_job_id, env)

        if not os.path.exists(working_dir):
            return {"error": f"Workspace directory not found: {working_dir}"}

        executor = TerraformExecutorWithLogs(
            working_dir=working_dir,
            temp_aws_credentials=temp_creds,
            job_id=log_id   # ← FIXED: use the apply job's own id
        )

        result = executor.safe_apply()

        if result["status"] == "FAILED":
            return {"error": result["logs"].get("stderr", str(result.get("logs", "Apply failed.")))}

        return result

    except Exception as e:
        save_logs(log_id, "error", stderr=f"Orchestrator Apply Error: {str(e)}")
        print(f"Orchestrator Error: {str(e)}")
        return {"error": str(e)}

# =========================================================
# COST
# =========================================================

def terraform_cost(project_id, plan_job_id, blueprint, credentials=None):
    try:
        env = blueprint.get("environment", "development").lower()
        workspace_path = get_workspace_path(project_id, plan_job_id, env)
        plan_json_path = os.path.join(workspace_path, "plan.json")

        if not os.path.exists(plan_json_path):
            return {"error": "plan.json not found. Run plan first."}

        aws_env = os.environ.copy()
        if credentials:
            temp_creds = assume_role(credentials["role_arn"], credentials.get("external_id"))
            aws_env.update(temp_creds)

        with get_env_lock(project_id, env):
            cost_proc = subprocess.run(
                ["infracost", "breakdown", "--path", plan_json_path, "--format", "json"],
                cwd=workspace_path, env=aws_env, capture_output=True, text=True
            )
            # Cost stderr is diagnostic info — save to plan job so it's browseable
            if cost_proc.stderr.strip():
                save_logs(plan_job_id, "cost", stderr=cost_proc.stderr)

            if cost_proc.returncode != 0:
                return {"error": cost_proc.stderr}

            cost_data = json.loads(cost_proc.stdout)
            total_monthly = cost_data.get("totalMonthlyCost")
            currency = cost_data.get("currency", "USD")
            summary = {}
            if total_monthly is not None:
                summary = {"monthly_cost": float(total_monthly), "currency": currency}

            return {"cost_summary": summary, "raw_cost": cost_data}

    except Exception as e:
        save_logs(plan_job_id, "error", stderr=f"Orchestrator Cost Error: {str(e)}")
        return {"error": f"Orchestrator Cost Error: {str(e)}"}

# =========================================================
# DESTROY  — logs written against the destroy job's own id
# =========================================================

def terraform_destroy(project_id, blueprint, credentials=None, job_id=None):
    try:
        env = blueprint.get("environment", "development").lower()
        env_folder = ENV_MAP.get(env, "dev")
        project_path = os.path.join(JOBS_BASE_DIR, project_id)

        aws_env = os.environ.copy()
        if credentials:
            temp_creds = assume_role(credentials["role_arn"], credentials.get("external_id"))
            aws_env.update(temp_creds)

        # Find latest workspace for this environment
        workspace_path = None
        for jid in sorted(
            os.listdir(project_path),
            key=lambda x: os.path.getctime(os.path.join(project_path, x)),
            reverse=True
        ):
            candidate = os.path.join(
                JOBS_BASE_DIR, project_id, jid, "terraform", "envs", env_folder
            )
            if os.path.exists(candidate):
                workspace_path = candidate
                break

        if not workspace_path:
            return {"error": "No deployed environment found."}

        log_job_id = job_id or f"destroy-unknown-{project_id}"

        with get_env_lock(project_id, env):
            destroy_proc = run_streaming(
                ["terraform", "destroy", "-auto-approve", "-no-color"],
                cwd=workspace_path, env=aws_env, job_id=log_job_id, stage="destroy"
            )

            if destroy_proc["returncode"] != 0:
                return {"error": destroy_proc["stderr"]}

            return {"message": "Environment destroyed successfully."}

    except Exception as e:
        if job_id:
            save_logs(job_id, "error", stderr=f"DESTROY CRASH: {str(e)}")
        return {"error": f"DESTROY CRASH: {str(e)}"}

# =========================================================
# STATUS
# =========================================================

def terraform_status(project_id, environment):
    env = environment.lower()
    env_folder = ENV_MAP.get(env, "dev")
    project_path = os.path.join(JOBS_BASE_DIR, project_id)

    if not os.path.exists(project_path):
        return {"status": "NOT_DEPLOYED"}

    workspace_path = None
    for job_id in os.listdir(project_path):
        candidate = os.path.join(
            JOBS_BASE_DIR, project_id, job_id, "terraform", "envs", env_folder
        )
        if os.path.exists(candidate):
            workspace_path = candidate
            break

    if not workspace_path:
        return {"status": "NOT_DEPLOYED"}

    state_file = os.path.join(workspace_path, "terraform.tfstate")
    if not os.path.exists(state_file):
        return {"status": "PLANNED"}

    try:
        state_data = json.load(open(state_file))
        resources = [r["type"] for r in state_data.get("resources", [])]
        return {"status": "DEPLOYED", "resources": resources}
    except Exception:
        return {"status": "UNKNOWN"}