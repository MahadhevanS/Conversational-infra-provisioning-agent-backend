import os
import json
import uuid
import shutil
from dataclasses import dataclass

BASE_DIR = os.path.join("backend", "runtime")   # ✅ runtime base
os.makedirs(BASE_DIR, exist_ok=True)

@dataclass
class Workspace:
    run_id: str
    root: str
    env_dir: str
    artifacts_dir: str
    logs_dir: str
    status_path: str
    result_path: str
    tfplan_path: str
    plan_json_path: str
    infracost_json_path: str
    plan_log: str
    cost_log: str
    apply_log: str

def _write_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

def create_workspace(env: str) -> Workspace:
    """
    env: dev / prod (folder exists in backend/terraform/envs/<env>)
    """
    run_id = uuid.uuid4().hex[:8]
    root = os.path.join(BASE_DIR, run_id)

    env_src = os.path.join("backend", "terraform", "envs", env)
    env_dir = os.path.join(root, "env")

    artifacts_dir = os.path.join(root, "artifacts")
    logs_dir = os.path.join(root, "logs")

    os.makedirs(artifacts_dir, exist_ok=True)
    os.makedirs(logs_dir, exist_ok=True)

    # ✅ Copy terraform env folder into runtime workspace
    if os.path.exists(env_dir):
        shutil.rmtree(env_dir)
    shutil.copytree(env_src, env_dir)

    ws = Workspace(
        run_id=run_id,
        root=root,
        env_dir=env_dir,
        artifacts_dir=artifacts_dir,
        logs_dir=logs_dir,
        status_path=os.path.join(root, "status.json"),
        result_path=os.path.join(root, "result.json"),
        tfplan_path=os.path.join(artifacts_dir, "tfplan"),
        plan_json_path=os.path.join(artifacts_dir, "plan.json"),
        infracost_json_path=os.path.join(artifacts_dir, "infracost.json"),
        plan_log=os.path.join(logs_dir, "plan.log"),
        cost_log=os.path.join(logs_dir, "cost.log"),
        apply_log=os.path.join(logs_dir, "apply.log"),
    )

    _write_json(ws.status_path, {
        "run_id": run_id,
        "step": "idle",
        "status": "IDLE",
        "message": "Workspace created"
    })
    _write_json(ws.result_path, {})

    return ws