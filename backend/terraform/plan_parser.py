import subprocess
import json


def is_plan_safe(working_dir):

    result = subprocess.run(
        ["terraform", "show", "-json", "tfplan"],
        cwd=working_dir,
        capture_output=True,
        text=True
    )

    plan_json = json.loads(result.stdout)

    for change in plan_json.get("resource_changes", []):

        actions = change["change"]["actions"]

        if "delete" in actions:
            return False

        if actions == ["create", "delete"]:
            return False

    return True
