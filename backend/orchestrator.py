import os
import json
import shutil
import subprocess
import platform
import threading
import uuid

from .tfvars_generator import generate_tfvars

# ---------------------------------------------------------
# PATHS
# ---------------------------------------------------------
# __file__ = .../project/backend/orchestrator.py
# PROJECT_ROOT should be: .../project
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

RUNTIME_BASE_DIR = os.path.join(PROJECT_ROOT, "runtime")
os.makedirs(RUNTIME_BASE_DIR, exist_ok=True)

TERRAFORM_ROOT = os.path.join(PROJECT_ROOT, "terraform")
TERRAFORM_ENVS_DIR = os.path.join(TERRAFORM_ROOT, "envs")
TERRAFORM_MODULES_DIR = os.path.join(TERRAFORM_ROOT, "modules")

# ---------------------------------------------------------
# GLOBAL LOCKS (per env)
# ---------------------------------------------------------
ENV_LOCKS = {}

def get_env_lock(env_name: str):
    env_name = (env_name or "dev").lower()
    if env_name not in ENV_LOCKS:
        ENV_LOCKS[env_name] = threading.Lock()
    return ENV_LOCKS[env_name]

# ---------------------------------------------------------
# HELPERS
# ---------------------------------------------------------
def _env_folder_from_blueprint(blueprint: dict) -> str:
    env = blueprint.get("environment", "development").lower()
    return "prod" if "prod" in env else "dev"

def _run_dir(run_id: str) -> str:
    return os.path.join(RUNTIME_BASE_DIR, run_id)

def _paths(run_id: str, env_folder: str = "dev"):
    """
    runtime/<run_id>/
      terraform/
        envs/<env_folder>/     <-- terraform runs here
        modules/               <-- junction/symlink to repo terraform/modules
      artifacts/
      logs/
      status.json
      result.json
    """
    root = _run_dir(run_id)
    terraform_root = os.path.join(root, "terraform")
    env_dir = os.path.join(terraform_root, "envs", env_folder)
    return {
        "root": root,
        "terraform_root": terraform_root,
        "env_dir": env_dir,
        "modules_dir": os.path.join(terraform_root, "modules"),
        "artifacts": os.path.join(root, "artifacts"),
        "logs": os.path.join(root, "logs"),
        "status": os.path.join(root, "status.json"),
        "result": os.path.join(root, "result.json"),
        "tfplan": os.path.join(root, "artifacts", "tfplan"),
        "plan_json": os.path.join(root, "artifacts", "plan.json"),
        "infracost_json": os.path.join(root, "artifacts", "infracost.json"),
        "plan_log": os.path.join(root, "logs", "plan.log"),
        "cost_log": os.path.join(root, "logs", "cost.log"),
        "apply_log": os.path.join(root, "logs", "apply.log"),
    }

def _write_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _update_status(run_id: str, status: str, step: str, message: str):
    env_folder = "dev"
    p = _paths(run_id, env_folder)
    _write_json(p["status"], {
        "run_id": run_id,
        "status": status,
        "step": step,
        "message": message
    })

def _append_result(run_id: str, patch: dict):
    env_folder = "dev"
    p = _paths(run_id, env_folder)
    data = _read_json(p["result"])
    data.update(patch)
    _write_json(p["result"], data)

def _ensure_symlink_modules(modules_dst: str):
    """
    Make runtime/<run_id>/terraform/modules -> <repo>/terraform/modules
    """
    os.makedirs(os.path.dirname(modules_dst), exist_ok=True)

    if os.path.exists(modules_dst):
        return

    if not os.path.exists(TERRAFORM_MODULES_DIR):
        raise FileNotFoundError(f"Repo modules not found: {TERRAFORM_MODULES_DIR}")

    if platform.system() == "Windows":
        # junction
        subprocess.run(["cmd", "/c", "mklink", "/J", modules_dst, TERRAFORM_MODULES_DIR], check=True)
    else:
        os.symlink(TERRAFORM_MODULES_DIR, modules_dst)

def _prepare_workspace(run_id: str, blueprint: dict):
    """
    Copies terraform/envs/<dev|prod> -> runtime/<run_id>/terraform/envs/<dev|prod>
    Creates junction runtime/<run_id>/terraform/modules
    """
    env_folder = _env_folder_from_blueprint(blueprint)
    p = _paths(run_id, env_folder)

    os.makedirs(p["artifacts"], exist_ok=True)
    os.makedirs(p["logs"], exist_ok=True)

    source_env_dir = os.path.join(TERRAFORM_ENVS_DIR, env_folder)
    if not os.path.exists(source_env_dir):
        raise FileNotFoundError(f"Terraform env folder not found: {source_env_dir}")

    # Copy env folder into runtime terraform/envs/<env>
    if os.path.exists(p["env_dir"]):
        shutil.rmtree(p["env_dir"])
    os.makedirs(os.path.dirname(p["env_dir"]), exist_ok=True)
    shutil.copytree(source_env_dir, p["env_dir"])

    # Create modules junction/symlink where ../../modules will resolve
    _ensure_symlink_modules(p["modules_dir"])

    # Initialize status + result
    _write_json(p["result"], {})
    _write_json(p["status"], {
        "run_id": run_id,
        "status": "IDLE",
        "step": "idle",
        "message": "Workspace ready"
    })

    return p, env_folder

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

# ---------------------------------------------------------
# 1) PLAN
# ---------------------------------------------------------
def terraform_plan(blueprint: dict, run_id: str | None = None):
    run_id = run_id or uuid.uuid4().hex[:8]
    p, env_folder = _prepare_workspace(run_id, blueprint)

    lock = get_env_lock(env_folder)

    def _worker():
        try:
            with lock:
                # generate tfvars inside env dir
                tfvars_path = generate_tfvars(blueprint, p["env_dir"])

                open(p["plan_log"], "w", encoding="utf-8").close()

                _write_json(p["status"], {"run_id": run_id, "status": "PLANNING", "step": "plan", "message": "terraform init..."})
                with open(p["plan_log"], "a", encoding="utf-8") as log:
                    init = subprocess.run(
                        ["terraform", "init", "-no-color", "-input=false"],
                        cwd=p["env_dir"],
                        stdout=log, stderr=log, text=True
                    )
                if init.returncode != 0:
                    _write_json(p["status"], {"run_id": run_id, "status": "PLAN_FAILED", "step": "plan", "message": "terraform init failed"})
                    return

                _write_json(p["status"], {"run_id": run_id, "status": "PLANNING", "step": "plan", "message": "terraform plan..."})
                with open(p["plan_log"], "a", encoding="utf-8") as log:
                    plan = subprocess.run(
                        ["terraform", "plan", f"-var-file={tfvars_path}", f"-out={p['tfplan']}", "-no-color"],
                        cwd=p["env_dir"],
                        stdout=log, stderr=log, text=True
                    )
                if plan.returncode != 0:
                    _write_json(p["status"], {"run_id": run_id, "status": "PLAN_FAILED", "step": "plan", "message": "terraform plan failed"})
                    return

                _write_json(p["status"], {"run_id": run_id, "status": "PLANNING", "step": "plan", "message": "terraform show (json)..."})
                show = subprocess.run(
                    ["terraform", "show", "-json", p["tfplan"]],
                    cwd=p["env_dir"],
                    capture_output=True,
                    text=True
                )
                if show.returncode != 0:
                    with open(p["plan_log"], "a", encoding="utf-8") as log:
                        log.write("\n[terraform show error]\n" + (show.stderr or "") + "\n")
                    _write_json(p["status"], {"run_id": run_id, "status": "PLAN_FAILED", "step": "plan", "message": "terraform show failed"})
                    return

                with open(p["plan_json"], "w", encoding="utf-8") as f:
                    f.write(show.stdout)

                _write_json(p["status"], {"run_id": run_id, "status": "PLAN_DONE", "step": "plan", "message": "Plan generated successfully"})
                _append_result(run_id, {"plan": {"status": "done"}})

        except Exception as e:
            _write_json(p["status"], {"run_id": run_id, "status": "PLAN_FAILED", "step": "plan", "message": str(e)})

    threading.Thread(target=_worker, daemon=True).start()
    return {"run_id": run_id, "status": "PLANNING"}

# ---------------------------------------------------------
# 2) COST
# ---------------------------------------------------------
def infracost_run(run_id: str, env_folder: str = "dev"):
    p = _paths(run_id, env_folder)

    if not os.path.exists(p["plan_json"]):
        _write_json(p["status"], {"run_id": run_id, "status": "COST_FAILED", "step": "cost", "message": "plan.json missing. Run plan first."})
        return {"run_id": run_id, "status": "COST_FAILED"}

    def _worker():
        try:
            open(p["cost_log"], "w", encoding="utf-8").close()

            _write_json(p["status"], {"run_id": run_id, "status": "COSTING", "step": "cost", "message": "Running infracost..."})
            with open(p["cost_log"], "a", encoding="utf-8") as log:
                proc = subprocess.run(
                    ["infracost", "breakdown", "--path", p["plan_json"], "--format", "json", "--out-file", p["infracost_json"]],
                    cwd=p["env_dir"],
                    stdout=log, stderr=log, text=True
                )

            if proc.returncode != 0:
                _write_json(p["status"], {"run_id": run_id, "status": "COST_FAILED", "step": "cost", "message": "Infracost failed (check cost.log)"})
                return

            summary = {}
            try:
                data = _read_json(p["infracost_json"])
                total = data.get("totalMonthlyCost")
                currency = data.get("currency")
                if total is not None:
                    summary = {"monthly_cost": float(total), "currency": currency or "USD"}
            except Exception:
                summary = {}

            _append_result(run_id, {"cost": summary})
            _write_json(p["status"], {"run_id": run_id, "status": "COST_DONE", "step": "cost", "message": "Cost estimation completed"})

        except Exception as e:
            _write_json(p["status"], {"run_id": run_id, "status": "COST_FAILED", "step": "cost", "message": str(e)})

    threading.Thread(target=_worker, daemon=True).start()
    return {"run_id": run_id, "status": "COSTING"}

# ---------------------------------------------------------
# 3) APPLY
# ---------------------------------------------------------
def terraform_apply(run_id: str, blueprint: dict):
    env_folder = _env_folder_from_blueprint(blueprint)
    p = _paths(run_id, env_folder)

    if not os.path.exists(p["tfplan"]):
        _write_json(p["status"], {"run_id": run_id, "status": "APPLY_FAILED", "step": "apply", "message": "tfplan missing. Run plan first."})
        return {"run_id": run_id, "status": "APPLY_FAILED"}

    lock = get_env_lock(env_folder)

    def _worker():
        try:
            with lock:
                open(p["apply_log"], "w", encoding="utf-8").close()

                _write_json(p["status"], {"run_id": run_id, "status": "APPLYING", "step": "apply", "message": "terraform apply..."})
                with open(p["apply_log"], "a", encoding="utf-8") as log:
                    apply_proc = subprocess.run(
                        ["terraform", "apply", "-auto-approve", "-no-color", p["tfplan"]],
                        cwd=p["env_dir"],
                        stdout=log, stderr=log, text=True
                    )

                if apply_proc.returncode != 0:
                    _write_json(p["status"], {"run_id": run_id, "status": "APPLY_FAILED", "step": "apply", "message": "terraform apply failed (check apply.log)"})
                    return

                out_proc = subprocess.run(
                    ["terraform", "output", "-json"],
                    cwd=p["env_dir"],
                    capture_output=True,
                    text=True
                )
                outputs = json.loads(out_proc.stdout) if out_proc.returncode == 0 and out_proc.stdout else {}
                access = format_access_points(outputs)

                _append_result(run_id, {"apply": {"status": "done"}, "outputs": outputs, "access": access})
                _write_json(p["status"], {"run_id": run_id, "status": "APPLY_DONE", "step": "apply", "message": "Apply completed successfully"})

        except Exception as e:
            _write_json(p["status"], {"run_id": run_id, "status": "APPLY_FAILED", "step": "apply", "message": str(e)})

    threading.Thread(target=_worker, daemon=True).start()
    return {"run_id": run_id, "status": "APPLYING"}

# ---------------------------------------------------------
# 4) STATUS / RESULT
# ---------------------------------------------------------
def run_status(run_id: str):
    # read status without needing env
    status_path = os.path.join(_run_dir(run_id), "status.json")
    if not os.path.exists(status_path):
        return {"run_id": run_id, "status": "UNKNOWN", "step": "unknown", "message": "status.json missing"}
    return _read_json(status_path)

def run_result(run_id: str):
    return _read_json(os.path.join(_run_dir(run_id), "result.json"))