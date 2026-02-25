import json
import logging
import urllib.request
import urllib.error
import uuid

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# =========================================================
# CONFIG
# =========================================================

DEFAULT_REGION = "us-east-1"
DEFAULT_ENV = "development"

BACK_PLAN_URL = "https://unmicrobial-suzie-unapprehendably.ngrok-free.dev/plan"
BACK_APPLY_URL = "https://unmicrobial-suzie-unapprehendably.ngrok-free.dev/apply"
BACK_DESTROY_URL = "https://unmicrobial-suzie-unapprehendably.ngrok-free.dev/destroy"
BACK_STATUS_URL = "https://unmicrobial-suzie-unapprehendably.ngrok-free.dev/status"

# =========================================================
# COST MODEL (Mock)
# =========================================================

COST = {
    "ec2": 10,
    "alb": 8,
    "rds": 15,
    "s3": 5,
    "eks": 40
}

# =========================================================
# STATE CONSTANTS
# =========================================================

STATE_IDLE = "IDLE"
STATE_PLAN_READY = "PLAN_READY"
STATE_AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
STATE_DEPLOYING = "DEPLOYING"
STATE_DEPLOYED = "DEPLOYED"
STATE_WAITING_PLAN_CONFIRM = "WAITING_PLAN_CONFIRM"
STATE_WAITING_PLAN_RESULT = "WAITING_PLAN_RESULT"
STATE_WAITING_APPLY_CONFIRM = "WAITING_APPLY_CONFIRM"


# =========================================================
# NLP PARSING
# =========================================================

def parse_environment(text):
    if "prod" in text:
        return "production"
    if "staging" in text:
        return "staging"
    return None


def parse_application_intent(text):
    text = text.lower()

    if "web app" in text:
        return "web_app"
    if "api" in text:
        return "api_backend"
    if "kubernetes" in text or "cluster" in text:
        return "k8s_cluster"
    if "database" in text:
        return "database"
    if "static" in text:
        return "static_website"

    return None


def parse_lifecycle_intent(intent_name):
    if intent_name == "CreateInfraIntent":
        return "CREATE"
    if intent_name == "UpdateInfraIntent":
        return "UPDATE"
    if intent_name == "DestroyInfraIntent":
        return "DESTROY"
    if intent_name == "StatusInfraIntent":
        return "STATUS"
    return None


# =========================================================
# BLUEPRINT GENERATION (Opinionated Defaults)
# =========================================================

def build_blueprint(app_type, env):

    bp = {
        "environment": env,
        "region": DEFAULT_REGION,
        "components": [
            {"type": "network", "service": "vpc"}
        ]
    }

    if app_type in ["web_app", "api_backend"]:
        bp["components"].extend([
            {
                "type": "compute",
                "service": "ec2",
                "compute": {"instance_type": "t3.micro"}
            },
            {
                "type": "traffic",
                "service": "alb"
            }
        ])

    elif app_type == "k8s_cluster":
        bp["components"].append({
            "type": "container",
            "service": "eks",
            "container": {"min_nodes": 1, "max_nodes": 2}
        })

    elif app_type == "database":
        bp["components"].append({
            "type": "database",
            "service": "rds",
            "database": {
                "engine": "postgres",
                "instance_type": "db.t3.micro"
            }
        })

    elif app_type == "static_website":
        bp["components"].append({
            "type": "storage",
            "service": "s3"
        })

    return bp


# =========================================================
# COST CALCULATION
# =========================================================

def calculate_cost(bp):
    total = 0
    for c in bp["components"]:
        svc = c.get("service")
        if svc in COST:
            total += COST[svc]
    return total


# =========================================================
# PLAN SUMMARY
# =========================================================
def generate_detailed_plan_summary(bp, cost):
    lines = []
    
    lines.append("📦 **Infrastructure Plan Summary**\n")
    lines.append(f"🌍 Environment: {bp['environment']}")
    lines.append(f"📍 Region: {bp['region']}\n")

    lines.append("🔧 Resources to be Created:\n")

    for c in bp["components"]:
        svc = c.get("service")

        if svc == "vpc":
            lines.append("• 🛜 VPC (Networking Layer)")

        elif svc == "ec2":
            instance = c.get("compute", {}).get("instance_type", "default")
            lines.append(f"• 🖥 EC2 Instance")
            lines.append(f"    ↳ Instance Type: {instance}")

        elif svc == "alb":
            lines.append("• 🌐 Application Load Balancer")

        elif svc == "rds":
            db = c.get("database", {})
            engine = db.get("engine", "postgres")
            instance = db.get("instance_type", "db.t3.micro")
            lines.append(f"• 🗄 RDS Database")
            lines.append(f"    ↳ Engine: {engine}")
            lines.append(f"    ↳ Instance Type: {instance}")

        elif svc == "s3":
            lines.append("• 🪣 S3 Bucket (Static Hosting)")

        elif svc == "eks":
            container = c.get("container", {})
            min_nodes = container.get("min_nodes", 1)
            max_nodes = container.get("max_nodes", 2)
            lines.append("• ☸️ EKS Cluster")
            lines.append(f"    ↳ Min Nodes: {min_nodes}")
            lines.append(f"    ↳ Max Nodes: {max_nodes}")

        lines.append("")  # spacing

    lines.append(f"💰 Estimated Monthly Cost: ${cost}/month\n")
    lines.append("Do you want to generate the Terraform plan?")

    return "\n".join(lines)


# =========================================================
# BACKEND CALLS
# =========================================================

def call_backend(url, payload):
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        response = urllib.request.urlopen(req, timeout=30)
        return json.loads(response.read().decode()), None
    except Exception as e:
        logger.error(str(e))
        return None, str(e)


# =========================================================
# RESPONSE HELPERS
# =========================================================

def build_response(intent_name, message, attrs, payload=None):
    """
    Helper to ensure ui_payload and sessionAttributes are synchronized.
    Does NOT overwrite existing ui_payload if already set.
    """

    # If payload explicitly provided → build fresh
    if payload:
        attrs["ui_payload"] = json.dumps(payload)

    # If ui_payload already exists (like PLAN_DISPLAY case) → keep it
    elif "ui_payload" not in attrs:
        attrs["ui_payload"] = json.dumps({
            "type": "MESSAGE",
            "message": message
        })

    return {
        "sessionState": {
            "sessionAttributes": attrs,
            "dialogAction": {"type": "Close"},
            "intent": {
                "name": intent_name,
                "state": "Fulfilled"
            }
        },
        "messages": [{"contentType": "PlainText", "content": message}]
    }

# =========================================================
# FORMAT TERRAFORM PLAN
# =========================================================

def format_terraform_plan(structured_plan):
    try:
        changes = structured_plan.get("resource_changes", [])
        if not changes:
            return "No changes."

        lines = []
        add = 0
        change = 0
        destroy = 0

        for r in changes:
            actions = r.get("change", {}).get("actions", [])
            name = r.get("type")

            if "create" in actions:
                add += 1
                lines.append(f"+ {name}")
            elif "update" in actions:
                change += 1
                lines.append(f"~ {name}")
            elif "delete" in actions:
                destroy += 1
                lines.append(f"- {name}")

        summary = f"\nPlan: {add} to add, {change} to change, {destroy} to destroy\n"
        return "Terraform Plan:\n\n" + "\n".join(lines) + "\n" + summary

    except Exception:
        return "Unable to parse Terraform plan output."

# =====================================================
# 🛠️ MODIFY & TERMINATE LOGIC
# =====================================================

def handle_modify_intent(text, attrs):
    if "infra_blueprint" not in attrs:
        return "❌ No active infrastructure found. Create one first."

    bp = json.loads(attrs["infra_blueprint"])
    changes = []
    text = text.lower()

    # Scaling Logic
    instance_types = ["t2.micro", "t2.small", "t3.micro", "t3.small", "t3.medium"]
    db_types = ["db.t3.micro", "db.t3.small", "db.m5.large"]

    # 1. Check for EC2 scaling
    for t in instance_types:
        if t in text:
            for c in bp["components"]:
                if c["service"] == "ec2":
                    c["compute"]["instance_type"] = t
                    changes.append(f"EC2 ➔ {t}")

    # 2. Check for RDS scaling
    for d in db_types:
        if d in text:
            for c in bp["components"]:
                if c["service"] == "rds":
                    c["database"]["instance_type"] = d
                    changes.append(f"RDS ➔ {d}")

    # 3. Check for EKS scaling
    if "nodes" in text:
        import re
        match = re.search(r'(\d+)\s*nodes', text)
        if match:
            new_count = int(match.group(1))
            for c in bp["components"]:
                if c["service"] == "eks":
                    c["container"]["min_nodes"] = new_count
                    c["container"]["max_nodes"] = new_count + 1
                    changes.append(f"EKS Nodes ➔ {new_count}")

    if not changes:
        return "❓ I heard modify, but I'm not sure what to change. Try 'upgrade ec2 to t3.small' or 'set 3 nodes'."

    attrs["infra_blueprint"] = json.dumps(bp)
    attrs["conversation_state"] = STATE_WAITING_PLAN_CONFIRM
    return f"✅ Update Summary: {', '.join(changes)}. Generate new plan?"


def handle_terminate_intent(text, attrs):
    if "infra_blueprint" not in attrs:
        return "❌ No active infrastructure found."

    bp = json.loads(attrs["infra_blueprint"])
    
    # Check if they want to destroy everything
    if any(word in text for word in ["all", "everything", "whole", "entire"]):
        bp["destroy_all"] = True
        attrs["infra_blueprint"] = json.dumps(bp)
        attrs["conversation_state"] = STATE_WAITING_PLAN_CONFIRM
        return "⚠️ This will destroy ALL resources in this environment. Proceed to plan?"

    target_services = []
    if any(x in text for x in ["ec2", "compute"]): target_services.append("ec2")
    if any(x in text for x in ["database", "rds"]): target_services.append("rds")
    if any(x in text for x in ["storage", "s3"]): target_services.append("s3")
    if any(x in text for x in ["eks", "kubernetes"]): target_services.append("eks")

    if not target_services:
        return "❓ Which service should I terminate? (e.g., 'terminate rds')"

    # Filter out the components being removed
    initial_count = len(bp["components"])
    bp["components"] = [c for c in bp["components"] if c.get("service") not in target_services]
    
    if len(bp["components"]) == initial_count:
        return f"❌ {', '.join(target_services).upper()} is not currently in your blueprint."

    attrs["infra_blueprint"] = json.dumps(bp)
    attrs["conversation_state"] = STATE_WAITING_PLAN_CONFIRM
    return f"⚠️ Removing {', '.join(target_services).upper()}. Generate update plan?"

def lex_webhook(event):
    intent = event["sessionState"]["intent"]
    intent_name = intent["name"]
    attrs = event["sessionState"].get("sessionAttributes") or {}
    text = (event.get("inputTranscript") or "").lower().strip()
    
    state = attrs.get("conversation_state", STATE_IDLE)

    is_yes = any(word in text for word in ["yes", "proceed", "generate", "ok", "sure"])
    is_no = any(word in text for word in ["no", "cancel", "stop", "discard"])

    # =====================================================
    # PLAN CONFIRMATION
    # =====================================================

    if state == STATE_WAITING_PLAN_CONFIRM:
        if is_yes:
            bp = json.loads(attrs["infra_blueprint"])
            result, error = call_backend(BACK_PLAN_URL, {"infra_blueprint": bp})

            if error:
                return build_response(
                    intent_name,
                    f"❌ Plan failed: {error}",
                    {"conversation_state": STATE_IDLE}
                )

            job_id = result.get("job_id")

            attrs.update({
                "plan_job_id": job_id,
                "conversation_state": STATE_IDLE
            })

            return build_response(
                intent_name,
                "⏳ Generating Terraform plan...",
                attrs,
                {"type": "PLAN_STARTED", "job_id": job_id}
            )

        if is_no:
            return build_response(
                intent_name,
                "❌ Plan discarded.",
                {"conversation_state": STATE_IDLE}
            )

    # =====================================================
    # INTENT ROUTING
    # =====================================================

    if intent_name == "CreateInfraIntent":
        app_type = parse_application_intent(text)
        env = parse_environment(text) or DEFAULT_ENV
        
        if not app_type:
            return build_response(
                intent_name,
                "What would you like to deploy? (Web App, K8s, etc.)",
                attrs
            )

        bp = build_blueprint(app_type, env)
        cost = calculate_cost(bp)

        attrs.update({
            "infra_blueprint": json.dumps(bp),
            "conversation_state": STATE_WAITING_PLAN_CONFIRM
        })

        msg = generate_detailed_plan_summary(bp, cost)
        return build_response(intent_name, msg, attrs)

    if intent_name == "ModifyInfraIntent":
        msg = handle_modify_intent(text, attrs)
        return build_response(intent_name, msg, attrs)

    if intent_name == "TerminateInfraIntent":
        msg = handle_terminate_intent(text, attrs)
        return build_response(intent_name, msg, attrs)

    if intent_name == "StatusInfraIntent":
        return build_response(intent_name, "Checking status...", attrs)

    return build_response(
        intent_name,
        "I didn't catch that. Try 'deploy a web app'.",
        {"conversation_state": STATE_IDLE}
    )