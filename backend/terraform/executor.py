# backend/terraform/executor.py
import os
import json
import subprocess
from typing import Optional

# Optional safety check (keep if you already have it)
try:
    from .plan_parser import is_plan_safe
except Exception:
    is_plan_safe = None


def _write_json(path: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def update_status(ws, status: str, step: str, message: str):
    _write_json(ws.status_path, {
        "run_id": ws.run_id,
        "status": status,
        "step": step,
        "message": message
    })


def append_result(ws, patch: dict):
    data = _read_json(ws.result_path)
    data.update(patch)
    _write_json(ws.result_path, data)


class TerraformExecutor:
    def __init__(self, working_dir: str):
        self.working_dir = working_dir  # this will be ws.env_dir

    def run(self, command: list[str], log_file: Optional[str] = None):
        # Block destroy completely
        if any("destroy" in x for x in command):
            raise Exception("terraform destroy is disabled by policy.")

        if log_file:
            os.makedirs(os.path.dirname(log_file), exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as log:
                log.write("\n$ " + " ".join(command) + "\n")
                result = subprocess.run(
                    command,
                    cwd=self.working_dir,
                    stdout=log,
                    stderr=log,
                    text=True
                )
            return {"exit_code": result.returncode}

        result = subprocess.run(
            command,
            cwd=self.working_dir,
            capture_output=True,
            text=True
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode
        }

    # ---------------------------
    # 1) PLAN ONLY
    # ---------------------------
    def plan_only(self, ws):
        """
        Creates:
          artifacts/tfplan
          artifacts/plan.json
          logs/plan.log
          status: PLAN_DONE / PLAN_FAILED
        """
        try:
            # clear old log
            open(ws.plan_log, "w").close()

            update_status(ws, "PLANNING", "plan", "Running terraform init...")
            init = self.run(["terraform", "init"], log_file=ws.plan_log)
            if init["exit_code"] != 0:
                update_status(ws, "PLAN_FAILED", "plan", "Terraform init failed")
                return

            update_status(ws, "PLANNING", "plan", "Running terraform plan...")
            plan = self.run(["terraform", "plan", f"-out={ws.tfplan_path}"], log_file=ws.plan_log)
            if plan["exit_code"] != 0:
                update_status(ws, "PLAN_FAILED", "plan", "Terraform plan failed")
                return

            update_status(ws, "PLANNING", "plan", "Exporting plan to JSON...")
            # terraform show -json tfplan -> plan.json
            with open(ws.plan_json_path, "w", encoding="utf-8") as out:
                show = subprocess.run(
                    ["terraform", "show", "-json", ws.tfplan_path],
                    cwd=self.working_dir,
                    stdout=out,
                    stderr=subprocess.PIPE,
                    text=True
                )
            if show.returncode != 0:
                # append stderr to plan.log
                with open(ws.plan_log, "a", encoding="utf-8") as log:
                    log.write("\n[terraform show error]\n" + (show.stderr or "") + "\n")
                update_status(ws, "PLAN_FAILED", "plan", "Terraform show failed")
                return

            # Optional safety check (non-destructive)
            if is_plan_safe:
                update_status(ws, "PLANNING", "plan", "Running safety check...")
                if not is_plan_safe(ws.env_dir):
                    update_status(ws, "PLAN_BLOCKED", "plan", "Blocked: destructive changes detected")
                    return

            update_status(ws, "PLAN_DONE", "plan", "Plan generated successfully")
            append_result(ws, {"plan": {"status": "done"}})

        except Exception as e:
            update_status(ws, "PLAN_FAILED", "plan", str(e))

    # ---------------------------
    # 2) COST ONLY (INFRACOST)
    # ---------------------------
    def cost_only(self, ws):
        """
        Requires:
          artifacts/plan.json
        Creates:
          artifacts/infracost.json
          logs/cost.log
          status: COST_DONE / COST_FAILED
        """
        try:
            open(ws.cost_log, "w").close()

            if not os.path.exists(ws.plan_json_path):
                update_status(ws, "COST_FAILED", "cost", "Missing plan.json. Run plan first.")
                return

            update_status(ws, "COSTING", "cost", "Running infracost breakdown...")
            # NOTE: infracost runs from anywhere; we call it from env_dir for consistency
            with open(ws.cost_log, "a", encoding="utf-8") as log:
                log.write("\n$ infracost breakdown ...\n")
                result = subprocess.run(
                    [
                        "infracost",
                        "breakdown",
                        "--path", ws.plan_json_path,
                        "--format", "json",
                        "--out-file", ws.infracost_json_path
                    ],
                    cwd=self.working_dir,
                    stdout=log,
                    stderr=log,
                    text=True
                )

            if result.returncode != 0:
                update_status(ws, "COST_FAILED", "cost", "Infracost failed (check cost.log)")
                return

            # Extract a simple summary for UI
            summary = {}
            try:
                data = _read_json(ws.infracost_json_path)
                # Common infracost structure: totalMonthlyCost might exist under "totalMonthlyCost"
                # If not present, just store full file.
                total = data.get("totalMonthlyCost")
                currency = data.get("currency")
                if total is not None:
                    summary = {"monthly_cost": float(total), "currency": currency or "USD"}
            except Exception:
                summary = {}

            update_status(ws, "COST_DONE", "cost", "Cost estimation completed")
            append_result(ws, {"cost": summary})

        except Exception as e:
            update_status(ws, "COST_FAILED", "cost", str(e))

    # ---------------------------
    # 3) APPLY ONLY
    # ---------------------------
    def apply_only(self, ws):
        """
        Requires:
          artifacts/tfplan
        Creates:
          logs/apply.log
          status: APPLY_DONE / APPLY_FAILED
        """
        try:
            open(ws.apply_log, "w").close()

            if not os.path.exists(ws.tfplan_path):
                update_status(ws, "APPLY_FAILED", "apply", "Missing tfplan. Run plan first.")
                return

            update_status(ws, "APPLYING", "apply", "Running terraform apply...")
            apply = self.run(
                ["terraform", "apply", "-auto-approve", ws.tfplan_path],
                log_file=ws.apply_log
            )

            if apply["exit_code"] != 0:
                update_status(ws, "APPLY_FAILED", "apply", "Terraform apply failed (check apply.log)")
                return

            update_status(ws, "APPLY_DONE", "apply", "Infrastructure applied successfully")
            append_result(ws, {"apply": {"status": "done"}})

        except Exception as e:
            update_status(ws, "APPLY_FAILED", "apply", str(e))