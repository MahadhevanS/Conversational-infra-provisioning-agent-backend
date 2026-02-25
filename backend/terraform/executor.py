import subprocess
from .plan_parser import is_plan_safe


class TerraformExecutor:

    def __init__(self, working_dir):
        self.working_dir = working_dir

    def run(self, command):

        # Block destroy completely
        if "destroy" in command:
            raise Exception("terraform destroy is disabled by policy.")

        result = subprocess.run(
            command,
            cwd=self.working_dir,
            capture_output=True,
            text=True,
        )

        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode
        }

    def safe_apply(self):

        # INIT
        init = self.run(["terraform", "init"])
        if init["exit_code"] != 0:
            return {"status": "FAILED", "stage": "init", "logs": init}

        # PLAN
        plan = self.run(["terraform", "plan", "-out=tfplan"])
        if plan["exit_code"] != 0:
            return {"status": "FAILED", "stage": "plan", "logs": plan}

        # SAFETY CHECK
        if not is_plan_safe(self.working_dir):
            return {
                "status": "BLOCKED",
                "reason": "Destructive changes detected"
            }

        # APPLY
        apply = self.run(["terraform", "apply", "-auto-approve", "tfplan"])
        if apply["exit_code"] != 0:
            return {"status": "FAILED", "stage": "apply", "logs": apply}

        return {"status": "SUCCESS", "logs": apply}
