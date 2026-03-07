import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# =========================================================
# CONFIG
# =========================================================

DEFAULT_REGION = "us-east-1"
DEFAULT_ENV = "development"

# 🔥 FIX 1: Communicating locally inside FastAPI to avoid timeouts!
LOCAL_BASE = "http://127.0.0.1:8000"
BACK_PLAN_URL = f"{LOCAL_BASE}/plan"
BACK_APPLY_URL = f"{LOCAL_BASE}/apply"
BACK_DESTROY_URL = f"{LOCAL_BASE}/destroy"
BACK_STATUS_URL = f"{LOCAL_BASE}/status"

COST = {
    "ec2": 10,
    "alb": 8,
    "rds": 15,
    "s3": 5,
    "eks": 40
}

STATE_IDLE = "IDLE"
STATE_WAITING_PLAN_CONFIRM = "WAITING_PLAN_CONFIRM"

# =========================================================
# NLP PARSING
# =========================================================

def parse_environment(text):
    if "prod" in text: return "production"
    if "staging" in text: return "staging"
    return None

def parse_application_intent(text):
    text = text.lower()
    # 🔥 FIX 2: Ordered from most specific to least specific so "static web app" correctly triggers S3!
    if any(w in text for w in ["static", "s3", "bucket"]): return "static_website"
    if any(w in text for w in ["kubernetes", "cluster", "k8s", "eks"]): return "k8s_cluster"
    if any(w in text for w in ["database", "rds", "postgres", "sql"]): return "database"
    if "api" in text: return "api_backend"
    if any(w in text for w in ["web app", "webapp", "page", "website", "react", "frontend"]): return "web_app"
    return None

def parse_lifecycle_intent(intent_name):
    if intent_name == "CreateInfraIntent": return "CREATE"
    if intent_name == "UpdateInfraIntent": return "UPDATE"
    if intent_name == "DestroyInfraIntent": return "DESTROY"
    if intent_name == "StatusInfraIntent": return "STATUS"
    return None

# =========================================================
# BLUEPRINT & COST
# =========================================================

def build_blueprint(app_type, env):
    bp = {"environment": env, "region": DEFAULT_REGION, "components": [{"type": "network", "service": "vpc"}]}
    
    if app_type in ["web_app", "api_backend"]:
        bp["components"].extend([
            {"type": "compute", "service": "ec2", "compute": {"instance_type": "t3.micro"}},
            {"type": "traffic", "service": "alb"}
        ])
    elif app_type == "k8s_cluster":
        bp["components"].append({"type": "container", "service": "eks", "container": {"min_nodes": 1, "max_nodes": 2}})
    elif app_type == "database":
        bp["components"].append({"type": "database", "service": "rds", "database": {"engine": "postgres", "instance_type": "db.t3.micro"}})
    elif app_type == "static_website":
        bp["components"].append({"type": "storage", "service": "s3"})
    return bp

def calculate_cost(bp):
    return sum(COST.get(c.get("service"), 0) for c in bp["components"])

def generate_detailed_plan_summary(bp, cost):
    lines = ["📦 **Infrastructure Plan Summary**\n", f"🌍 Environment: {bp['environment']}", f"📍 Region: {bp['region']}\n", "🔧 Resources to be Created:\n"]
    for c in bp["components"]:
        svc = c.get("service")
        if svc == "vpc": lines.append("• 🛜 VPC (Networking Layer)")
        elif svc == "ec2": lines.append(f"• 🖥 EC2 Instance\n    ↳ Instance Type: {c.get('compute', {}).get('instance_type', 'default')}")
        elif svc == "alb": lines.append("• 🌐 Application Load Balancer")
        elif svc == "rds": lines.append(f"• 🗄 RDS Database\n    ↳ Engine: {c.get('database', {}).get('engine', 'postgres')}\n    ↳ Instance Type: {c.get('database', {}).get('instance_type', 'db.t3.micro')}")
        elif svc == "s3": lines.append("• 🪣 S3 Bucket (Static Hosting)")
        elif svc == "eks": lines.append(f"• ☸️ EKS Cluster\n    ↳ Min Nodes: {c.get('container', {}).get('min_nodes', 1)}\n    ↳ Max Nodes: {c.get('container', {}).get('max_nodes', 2)}")
        lines.append("")
    lines.append(f"💰 Estimated Monthly Cost: ${cost}/month\n\nDo you want to generate the Terraform plan?")
    return "\n".join(lines)

# =========================================================
# BACKEND & RESPONSE
# =========================================================

def call_backend(url, payload):
    try:
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
        response = urllib.request.urlopen(req, timeout=10) 
        return json.loads(response.read().decode()), None
    except Exception as e:
        logger.error(f"Internal Backend Call Failed: {str(e)}")
        return None, str(e)

def build_response(intent_name, message, attrs, payload=None):
    if payload: attrs["ui_payload"] = json.dumps(payload)
    elif "ui_payload" not in attrs: attrs["ui_payload"] = json.dumps({"type": "MESSAGE", "message": message})
    return {
        "sessionState": {
            "sessionAttributes": attrs,
            "dialogAction": {"type": "Close"},
            "intent": {"name": intent_name, "state": "Fulfilled"}
        },
        "messages": [{"contentType": "PlainText", "content": message}]
    }

# =====================================================
# MODIFY & TERMINATE
# =====================================================

def handle_modify_intent(text, attrs):
    if "infra_blueprint" not in attrs: return "❌ No active infrastructure found."
    bp = json.loads(attrs["infra_blueprint"])
    changes = []
    text = text.lower()
    
    for t in ["t2.micro", "t2.small", "t3.micro", "t3.small", "t3.medium"]:
        if t in text:
            for c in bp["components"]:
                if c["service"] == "ec2": c["compute"]["instance_type"] = t; changes.append(f"EC2 ➔ {t}")
    for d in ["db.t3.micro", "db.t3.small", "db.m5.large"]:
        if d in text:
            for c in bp["components"]:
                if c["service"] == "rds": c["database"]["instance_type"] = d; changes.append(f"RDS ➔ {d}")
    if "nodes" in text:
        import re
        match = re.search(r'(\d+)\s*nodes', text)
        if match:
            new_count = int(match.group(1))
            for c in bp["components"]:
                if c["service"] == "eks": c["container"]["min_nodes"] = new_count; c["container"]["max_nodes"] = new_count + 1; changes.append(f"EKS Nodes ➔ {new_count}")

    if not changes: return "❓ I heard modify, but I'm not sure what to change."
    attrs.update({"infra_blueprint": json.dumps(bp), "conversation_state": STATE_WAITING_PLAN_CONFIRM})
    return f"✅ Update Summary: {', '.join(changes)}. Generate new plan?"

def handle_terminate_intent(text, attrs):
    if "infra_blueprint" not in attrs: return "❌ No active infrastructure found."
    bp = json.loads(attrs["infra_blueprint"])
    if any(word in text for word in ["all", "everything", "whole", "entire"]):
        bp["destroy_all"] = True
        attrs.update({"infra_blueprint": json.dumps(bp), "conversation_state": STATE_WAITING_PLAN_CONFIRM})
        return "⚠️ This will destroy ALL resources in this environment. Proceed to plan?"
    
    targets = [s for s, keys in [("ec2", ["ec2", "compute"]), ("rds", ["database", "rds"]), ("s3", ["storage", "s3"]), ("eks", ["eks", "kubernetes"])] if any(k in text for k in keys)]
    if not targets: return "❓ Which service should I terminate?"
    
    initial = len(bp["components"])
    bp["components"] = [c for c in bp["components"] if c.get("service") not in targets]
    if len(bp["components"]) == initial: return f"❌ {', '.join(targets).upper()} is not in your blueprint."
    
    attrs.update({"infra_blueprint": json.dumps(bp), "conversation_state": STATE_WAITING_PLAN_CONFIRM})
    return f"⚠️ Removing {', '.join(targets).upper()}. Generate update plan?"

# =====================================================
# MAIN WEBHOOK
# =====================================================

def lex_webhook(event):
    intent_name = event["sessionState"]["intent"]["name"]
    attrs = event["sessionState"].get("sessionAttributes") or {}
    
    # Extract text robustly
    text = event.get("inputTranscript", "")
    if not text and event.get("transcriptions"):
        text = event["transcriptions"][0].get("transcription", "")
    text = text.lower().strip()
    
    # 🔥 THE REAL FIX: Only use the project_id passed by the React frontend!
    if "project_id" not in attrs:
        print("⚠️ WARNING: Lex did not receive a project_id from the React frontend!")
    
    state = attrs.get("conversation_state", STATE_IDLE)
    
    is_yes = any(word in text.split() for word in ["yes", "proceed", "generate", "ok", "sure", "y", "do it"])
    is_no = any(word in text.split() for word in ["no", "cancel", "stop", "discard", "n"])

    # =====================================================
    # STATE INTERCEPT (Bypasses Lex Intent Confusion)
    # =====================================================
    # If we are waiting for an App Type, we evaluate the text NO MATTER WHAT intent Lex thinks it is!
    if state == "WAITING_FOR_APP_TYPE":
        app_type = parse_application_intent(text)
        if not app_type:
            # We catch the error so you know exactly what the bot heard
            return build_response("CreateInfraIntent", f"I heard '{text}', but didn't recognize the app type. Try typing exactly 'S3 Bucket', 'Web App', or 'K8s'.", attrs)
        
        env = parse_environment(text) or DEFAULT_ENV
        bp = build_blueprint(app_type, env)
        cost = calculate_cost(bp)
        attrs.update({"infra_blueprint": json.dumps(bp), "conversation_state": STATE_WAITING_PLAN_CONFIRM})
        return build_response("CreateInfraIntent", generate_detailed_plan_summary(bp, cost), attrs)

    if state == STATE_WAITING_PLAN_CONFIRM:
        if is_yes:
            result, error = call_backend(BACK_PLAN_URL, {"project_id": attrs["project_id"], "infra_blueprint": json.loads(attrs["infra_blueprint"])})
            if error: return build_response(intent_name, f"❌ Plan failed: {error}", {"conversation_state": STATE_IDLE})
            attrs.update({"plan_job_id": result.get("job_id"), "conversation_state": STATE_IDLE})
            return build_response(intent_name, "⏳ Generating Terraform plan...", attrs, {"type": "PLAN_STARTED", "job_id": result.get("job_id")})
        if is_no: return build_response(intent_name, "❌ Plan discarded.", {"conversation_state": STATE_IDLE})
        return build_response(intent_name, "Please reply with 'yes' or 'no' to generate the plan.", attrs)

    # =====================================================
    # INTENT ROUTING
    # =====================================================
    if intent_name == "CreateInfraIntent":
        app_type = parse_application_intent(text)
        if not app_type:
            # Tell the bot we are waiting for the answer on the next turn!
            attrs["conversation_state"] = "WAITING_FOR_APP_TYPE"
            return build_response(intent_name, "What would you like to deploy? (Web App, K8s, S3 Bucket, etc.)", attrs)

        env = parse_environment(text) or DEFAULT_ENV
        bp = build_blueprint(app_type, env)
        cost = calculate_cost(bp)
        attrs.update({"infra_blueprint": json.dumps(bp), "conversation_state": STATE_WAITING_PLAN_CONFIRM})
        return build_response(intent_name, generate_detailed_plan_summary(bp, cost), attrs)

    if intent_name == "ModifyInfraIntent": return build_response(intent_name, handle_modify_intent(text, attrs), attrs)
    if intent_name == "TerminateInfraIntent": return build_response(intent_name, handle_terminate_intent(text, attrs), attrs)
    if intent_name == "StatusInfraIntent": return build_response(intent_name, "Checking status...", attrs)

    return build_response(intent_name, "I didn't catch that. Try 'deploy a web app'.", {"conversation_state": STATE_IDLE})