import os
import subprocess
from .plan_parser import is_plan_safe

class TerraformExecutor:
    # Accept temporary AWS credentials (AccessKeyId, SecretAccessKey, SessionToken)
    def __init__(self, working_dir, temp_aws_credentials=None):
        self.working_dir = working_dir
        self.temp_aws_credentials = temp_aws_credentials 

    def run(self, command):
        # Block destroy completely
        if "destroy" in command:
            raise Exception("terraform destroy is disabled by policy.")

        print(f"🔄 Executing: {' '.join(command)}")

        # Copy the current server environment variables
        custom_env = os.environ.copy()

        # 🔥 MULTI-TENANCY INJECTION
        # If we have temporary keys from the tenant's Role, use them!
        if self.temp_aws_credentials:
            print("🔑 Using temporary tenant credentials for this operation...")
            custom_env["AWS_ACCESS_KEY_ID"] = self.temp_aws_credentials["AccessKeyId"]
            custom_env["AWS_SECRET_ACCESS_KEY"] = self.temp_aws_credentials["SecretAccessKey"]
            custom_env["AWS_SESSION_TOKEN"] = self.temp_aws_credentials["SessionToken"]
            # Force Terraform to ignore any local AWS profiles
            custom_env["AWS_PROFILE"] = "" 
        else:
            print("⚠️ Warning: No temporary credentials provided. Using backend default identity.")

        result = subprocess.run(
            command,
            cwd=self.working_dir,
            capture_output=True,
            text=True,
            env=custom_env 
        )

        # Print errors to terminal for debugging
        if result.returncode != 0:
            print("❌ TERRAFORM ERROR DETECTED ❌")
            print(f"STDOUT: {result.stdout}")
            print(f"STDERR: {result.stderr}")

        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode
        }

    def safe_apply(self):
        # 1. INIT
        init = self.run(["terraform", "init"])
        if init["exit_code"] != 0:
            return {"status": "FAILED", "stage": "init", "logs": init}

        # 2. PLAN (Save to file)
        plan = self.run(["terraform", "plan", "-out=tfplan"])
        if plan["exit_code"] != 0:
            return {"status": "FAILED", "stage": "plan", "logs": plan}

        # 3. SAFETY CHECK (Your custom logic)
        if not is_plan_safe(self.working_dir):
            return {
                "status": "BLOCKED",
                "reason": "Destructive changes detected"
            }

        # 4. APPLY (Execute the saved plan)
        # We don't use -auto-approve here because 'tfplan' is a positional argument
        apply = self.run(["terraform", "apply", "tfplan"])
        
        if apply["exit_code"] != 0:
            return {"status": "FAILED", "stage": "apply", "logs": apply}

        return {"status": "SUCCESS", "logs": apply}