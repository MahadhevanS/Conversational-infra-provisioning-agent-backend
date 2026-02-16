import subprocess
import os
import json
import shutil
import platform
from tfvars_generator import generate_tfvars
from terraform.executor import TerraformExecutor
from terraform.workspace import generate_tf_from_blueprint

ENV_MAP = {
    "development": "dev",
    "dev": "dev",
    "production": "prod",
    "prod": "prod",
    "test": "test"
}

JOBS_BASE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "persistent_jobs")
os.makedirs(JOBS_BASE_DIR, exist_ok=True)

def terraform_plan(blueprint, job_id): # Add job_id parameter
    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    MODULES_PATH = os.path.join(PROJECT_ROOT, "terraform", "modules")
    
    # Define a specific persistent path for THIS job
    job_dir = os.path.join(JOBS_BASE_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        env = blueprint.get("environment", "development").lower()
        env_folder = ENV_MAP.get(env, "dev")
        source_dir = os.path.join(PROJECT_ROOT, "terraform", "envs", env_folder)

        fake_env_path = os.path.join(job_dir, "terraform", "envs", env_folder)
        fake_modules_path = os.path.join(job_dir, "terraform", "modules")
        
        # Ensure the parent of the modules path exists (the 'terraform' folder)
        os.makedirs(os.path.dirname(fake_modules_path), exist_ok=True)
        os.makedirs(fake_env_path, exist_ok=True)

        # 2. Symlink/Junction Modules
        if os.path.exists(MODULES_PATH) and not os.path.exists(fake_modules_path): # Check if exists
            if platform.system() == "Windows":
                # mklink /j requires the destination to NOT exist yet
                subprocess.run(['cmd', '/c', 'mklink', '/j', fake_modules_path, MODULES_PATH], check=True)
            else:
                os.symlink(MODULES_PATH, fake_modules_path)

        # 3. Copy .tf files
        for item in os.listdir(source_dir):
            s = os.path.join(source_dir, item)
            d = os.path.join(fake_env_path, item)
            if os.path.isfile(s) and s.endswith(".tf"):
                shutil.copy2(s, d)

        # 4. Generate vars and Run Plan
        tfvars_path = generate_tfvars(blueprint, fake_env_path)
        
        subprocess.run(["terraform", "init", "-no-color", "-input=false"], cwd=fake_env_path, capture_output=True)
        
        plan = subprocess.run(
            ["terraform", "plan", f"-var-file={tfvars_path}", "-out=tfplan", "-no-color"],
            cwd=fake_env_path, capture_output=True, text=True
        )

        show_json = subprocess.run(["terraform", "show", "-json", "tfplan"], cwd=fake_env_path, capture_output=True, text=True)

        return {"raw": plan.stdout, "structured": show_json.stdout}
    except Exception as e:
        return f"ORCHESTRATOR CRASH: {str(e)}"

def terraform_apply(job_id, blueprint):
    # Find the folder created during the plan phase
    env = blueprint.get("environment", "development").lower()
    env_folder = ENV_MAP.get(env, "dev")
    job_dir = os.path.join(JOBS_BASE_DIR, job_id, "terraform", "envs", env_folder)
    
    plan_path = os.path.join(job_dir, "tfplan")
    if not os.path.exists(job_dir):
        return "ERROR: Planned file 'tfplan' not found. You must plan before applying."

    # Use the existing tfplan file
    executor = TerraformExecutor(job_dir)
    apply_result = executor.safe_apply() # Ensure safe_apply runs 'terraform apply tfplan'

    output_proc = subprocess.run(
        ["terraform", "output", "-json"],
        cwd=job_dir, capture_output=True, text=True
    )
    
    return {
        "logs": apply_result,
        "outputs": json.loads(output_proc.stdout) if output_proc.returncode == 0 else {}
    }
