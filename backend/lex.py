# import json
# import logging
# import urllib.request
# import urllib.error

# logger = logging.getLogger()
# logger.setLevel(logging.INFO)

# # =========================================================
# # CONFIG
# # =========================================================

# DEFAULT_REGION = "us-east-1"
# DEFAULT_ENV = "development"

# LOCAL_BASE = "http://127.0.0.1:8000"
# BACK_PLAN_URL = f"{LOCAL_BASE}/plan"
# BACK_APPLY_URL = f"{LOCAL_BASE}/apply"
# BACK_DESTROY_URL = f"{LOCAL_BASE}/destroy"
# BACK_STATUS_URL = f"{LOCAL_BASE}/status"

# COST = {
#     "ec2": 10,
#     "alb": 8,
#     "rds": 15,
#     "s3": 5,
#     "eks": 40
# }

# STATE_IDLE = "IDLE"
# STATE_WAITING_PLAN_CONFIRM = "WAITING_PLAN_CONFIRM"

# # =========================================================
# # NLP PARSING
# # =========================================================

# def parse_environment(text):
#     if "prod" in text: return "production"
#     if "staging" in text: return "staging"
#     return None

# def parse_application_intent(text):
#     text = text.lower()
#     if any(w in text for w in ["static", "s3", "bucket"]): return "static_website"
#     if any(w in text for w in ["kubernetes", "cluster", "k8s", "eks"]): return "k8s_cluster"
#     if any(w in text for w in ["database", "rds", "postgres", "sql"]): return "database"
#     if "api" in text: return "api_backend"
#     if any(w in text for w in ["web app", "webapp", "page", "website", "react", "frontend"]): return "web_app"
#     return None

# # =========================================================
# # BLUEPRINT & COST
# # =========================================================

# def build_blueprint(app_type, env):
#     bp = {"environment": env, "region": DEFAULT_REGION, "components": [{"type": "network", "service": "vpc"}]}

#     if app_type in ["web_app", "api_backend"]:
#         bp["components"].extend([
#             {"type": "compute", "service": "ec2", "compute": {"instance_type": "t3.micro"}},
#             {"type": "traffic", "service": "alb"}
#         ])
#     elif app_type == "k8s_cluster":
#         bp["components"].append({"type": "container", "service": "eks", "container": {"min_nodes": 1, "max_nodes": 2}})
#     elif app_type == "database":
#         bp["components"].append({"type": "database", "service": "rds", "database": {"engine": "postgres", "instance_type": "db.t3.micro"}})
#     elif app_type == "static_website":
#         bp["components"].append({"type": "storage", "service": "s3"})
#     return bp

# def calculate_cost(bp):
#     return sum(COST.get(c.get("service"), 0) for c in bp["components"])

# def generate_detailed_plan_summary(bp, cost):
#     lines = ["📦 **Infrastructure Plan Summary**\n", f"🌍 Environment: {bp['environment']}", f"📍 Region: {bp['region']}\n", "🔧 Resources to be Created:\n"]
#     for c in bp["components"]:
#         svc = c.get("service")
#         if svc == "vpc": lines.append("• 🛜 VPC (Networking Layer)")
#         elif svc == "ec2": lines.append(f"• 🖥 EC2 Instance\n    ↳ Instance Type: {c.get('compute', {}).get('instance_type', 'default')}")
#         elif svc == "alb": lines.append("• 🌐 Application Load Balancer")
#         elif svc == "rds": lines.append(f"• 🗄 RDS Database\n    ↳ Engine: {c.get('database', {}).get('engine', 'postgres')}\n    ↳ Instance Type: {c.get('database', {}).get('instance_type', 'db.t3.micro')}")
#         elif svc == "s3": lines.append("• 🪣 S3 Bucket (Static Hosting)")
#         elif svc == "eks": lines.append(f"• ☸️ EKS Cluster\n    ↳ Min Nodes: {c.get('container', {}).get('min_nodes', 1)}\n    ↳ Max Nodes: {c.get('container', {}).get('max_nodes', 2)}")
#         lines.append("")
#     lines.append(f"\nDo you want to generate the Terraform plan?")
#     return "\n".join(lines)

# # =========================================================
# # BACKEND & RESPONSE
# # =========================================================

# def call_backend(url, payload):
#     try:
#         req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
#         response = urllib.request.urlopen(req, timeout=10)
#         return json.loads(response.read().decode()), None
#     except Exception as e:
#         logger.error(f"Internal Backend Call Failed: {str(e)}")
#         return None, str(e)

# def call_backend_get(url):
#     try:
#         req = urllib.request.Request(url, method="GET")
#         response = urllib.request.urlopen(req, timeout=5)
#         return json.loads(response.read().decode()), None
#     except Exception as e:
#         logger.error(f"GET Call Failed: {str(e)}")
#         return None, str(e)

# def build_response(intent_name, message, attrs, payload=None):
#     """
#     If payload is provided, it MUST include a "message" key — otherwise the
#     frontend will read payload.message as undefined and render an empty bubble.
#     When no payload is needed, omit it entirely so the plain messages[] path is used.
#     """
#     if payload:
#         # Always inject the message into the payload so the frontend can read it
#         payload["message"] = message
#         attrs["ui_payload"] = json.dumps(payload)
#     else:
#         attrs["ui_payload"] = json.dumps({"type": "MESSAGE", "message": message})

#     return {
#         "sessionState": {
#             "sessionAttributes": attrs,
#             "dialogAction": {"type": "Close"},
#             "intent": {"name": intent_name, "state": "Fulfilled"}
#         },
#         "messages": [{"contentType": "PlainText", "content": message}]
#     }

# # =====================================================
# # MODIFY
# # =====================================================

# # Canonical service keyword map — used for both add and remove detection
# SERVICE_KEYWORDS = {
#     "ec2":  ["ec2", "compute", "server", "instance"],
#     "rds":  ["database", "rds", "postgres", "mysql", "mariadb", "sql"],
#     "s3":   ["s3", "bucket", "storage", "static"],
#     "alb":  ["alb", "load balancer", "loadbalancer"],
#     "eks":  ["eks", "kubernetes", "k8s", "cluster"],
# }

# EC2_TYPES  = ["t2.micro", "t2.small", "t3.micro", "t3.small", "t3.medium", "t3.large"]
# RDS_TYPES  = ["db.t3.micro", "db.t3.small", "db.t3.medium"]
# RDS_ENGINES = ["postgres", "mysql", "mariadb"]
# EKS_NODE_COUNTS = ["1", "2", "3", "4", "5", "6", "8", "10"]

# def _service_in_text(service, text):
#     """Return True if any keyword for this service appears in text."""
#     return any(kw in text for kw in SERVICE_KEYWORDS.get(service, []))

# def _current_components(components):
#     """Return a readable summary of active non-VPC components."""
#     active = [c["service"].upper() for c in components if c["service"] != "vpc"]
#     return ", ".join(active) if active else "none"

# def handle_modify_intent(text, attrs):
#     project_id = attrs.get("project_id")
#     text = text.lower()

#     # ── Blueprint hydration ──────────────────────────────────────────────────
#     if "infra_blueprint" not in attrs and project_id:
#         data, error = call_backend_get(f"{LOCAL_BASE}/projects/{project_id}/blueprint")
#         if data and data.get("blueprint"):
#             attrs["infra_blueprint"] = json.dumps(data["blueprint"])
#             logger.info(f"Hydrated blueprint for project {project_id}")

#     if "infra_blueprint" not in attrs:
#         return "❌ I don't see an active configuration. Try deploying something first."

#     bp = json.loads(attrs["infra_blueprint"])
#     components = bp.get("components", [])
#     existing_services = {c["service"] for c in components}

#     # ── Determine intent verb: add / remove / change ─────────────────────────
#     is_add    = any(w in text for w in ["add", "include", "attach", "also need", "also want"])
#     is_remove = any(w in text for w in ["remove", "delete", "drop", "get rid of", "without"])
#     is_change = any(w in text for w in ["change", "update", "upgrade", "downgrade", "switch", "use", "set", "scale", "resize", "modify"])

#     # Identify which service the user is referring to
#     mentioned_service = next(
#         (svc for svc in SERVICE_KEYWORDS if _service_in_text(svc, text)),
#         None
#     )

#     logger.info(f"Modify — verb: add={is_add} remove={is_remove} change={is_change} | service={mentioned_service}")
#     logger.info(f"Existing services: {existing_services}")

#     # ── ADD ──────────────────────────────────────────────────────────────────
#     if is_add:
#         if not mentioned_service:
#             return (
#                 "➕ **What would you like to add?**\n"
#                 f"Current components: {_current_components(components)}\n\n"
#                 "Available to add:\n"
#                 "• EC2 (compute server)\n"
#                 "• RDS (database)\n"
#                 "• S3 (storage bucket)\n"
#                 "• ALB (load balancer)\n"
#                 "• EKS (Kubernetes cluster)"
#             )

#         if mentioned_service in existing_services:
#             return f"⚠️ Your blueprint already has **{mentioned_service.upper()}**. Did you want to modify it instead?"

#         # Add the new component with sensible defaults
#         if mentioned_service == "ec2":
#             bp["components"].append({"type": "compute", "service": "ec2", "compute": {"instance_type": "t3.micro"}})
#         elif mentioned_service == "rds":
#             engine = next((e for e in RDS_ENGINES if e in text), "postgres")
#             size   = next((s for s in RDS_TYPES if s in text), "db.t3.micro")
#             bp["components"].append({"type": "database", "service": "rds", "database": {"engine": engine, "instance_type": size}})
#         elif mentioned_service == "s3":
#             bp["components"].append({"type": "storage", "service": "s3"})
#         elif mentioned_service == "alb":
#             bp["components"].append({"type": "traffic", "service": "alb"})
#         elif mentioned_service == "eks":
#             bp["components"].append({"type": "container", "service": "eks", "container": {"min_nodes": 1, "max_nodes": 2}})

#         return finalize_modification(bp, attrs, f"Added {mentioned_service.upper()}")

#     # ── REMOVE ───────────────────────────────────────────────────────────────
#     if is_remove:
#         if not mentioned_service:
#             removable = [c["service"].upper() for c in components if c["service"] != "vpc"]
#             if not removable:
#                 return "📭 Nothing removable — your blueprint only contains the core VPC."
#             return (
#                 "🗑️ **Which component should I remove?**\n• "
#                 + "\n• ".join(removable)
#             )

#         if mentioned_service == "vpc":
#             return "❌ VPC is the core network layer and cannot be removed."

#         if mentioned_service not in existing_services:
#             return f"❌ **{mentioned_service.upper()}** is not in your current blueprint."

#         bp["components"] = [c for c in components if c["service"] != mentioned_service]
#         return finalize_modification(bp, attrs, f"Removed {mentioned_service.upper()}")

#     # ── CHANGE / UPDATE ───────────────────────────────────────────────────────
#     if is_change or mentioned_service:

#         # EC2 instance type change
#         if mentioned_service == "ec2" or (not mentioned_service and "instance" in text):
#             if "ec2" not in existing_services:
#                 return "❌ There's no EC2 instance in your blueprint to modify. Try 'add ec2' first."
#             chosen = next((t for t in EC2_TYPES if t in text), None)
#             if not chosen:
#                 current = next((c["compute"]["instance_type"] for c in components if c["service"] == "ec2"), "unknown")
#                 return (
#                     f"🖥️ **EC2 Instance Type** (current: `{current}`)\n"
#                     "Choose a new type:\n• " + "\n• ".join(EC2_TYPES)
#                 )
#             for c in bp["components"]:
#                 if c["service"] == "ec2":
#                     c["compute"]["instance_type"] = chosen
#             return finalize_modification(bp, attrs, f"EC2 instance type → {chosen}")

#         # RDS engine or size change
#         if mentioned_service == "rds":
#             if "rds" not in existing_services:
#                 return "❌ There's no RDS database in your blueprint. Try 'add database' first."
#             new_engine = next((e for e in RDS_ENGINES if e in text), None)
#             new_size   = next((s for s in RDS_TYPES if s in text), None)
#             if not new_engine and not new_size:
#                 current = next((c["database"] for c in components if c["service"] == "rds"), {})
#                 return (
#                     f"🗄️ **RDS Database** (current: `{current.get('engine','postgres')}` / `{current.get('instance_type','db.t3.micro')}`)\n\n"
#                     f"• Engines: {', '.join(RDS_ENGINES)}\n"
#                     f"• Sizes: {', '.join(RDS_TYPES)}\n\n"
#                     "Example: 'change database to mysql' or 'use db.t3.small'"
#                 )
#             for c in bp["components"]:
#                 if c["service"] == "rds":
#                     if new_engine: c["database"]["engine"] = new_engine
#                     if new_size:   c["database"]["instance_type"] = new_size
#             label = f"RDS → {new_engine or ''} {new_size or ''}".strip()
#             return finalize_modification(bp, attrs, label)

#         # EKS node scaling
#         if mentioned_service == "eks":
#             if "eks" not in existing_services:
#                 return "❌ There's no EKS cluster in your blueprint. Try 'add eks' first."
#             new_count = next((int(n) for n in EKS_NODE_COUNTS if n in text), None)
#             if not new_count:
#                 current = next((c["container"] for c in components if c["service"] == "eks"), {})
#                 return (
#                     f"☸️ **EKS Cluster** (current: min={current.get('min_nodes',1)} max={current.get('max_nodes',2)})\n\n"
#                     "Example: 'scale eks to 3 nodes' or 'set max nodes to 5'"
#                 )
#             for c in bp["components"]:
#                 if c["service"] == "eks":
#                     c["container"]["min_nodes"] = 1
#                     c["container"]["max_nodes"] = new_count
#             return finalize_modification(bp, attrs, f"EKS → max {new_count} nodes")

#         # ALB — nothing to configure, just inform
#         if mentioned_service == "alb":
#             if "alb" not in existing_services:
#                 return "❌ There's no ALB in your blueprint. Try 'add load balancer' first."
#             return "ℹ️ ALB has no configurable options. You can 'remove alb' if you no longer need it."

#         # S3 — nothing to configure
#         if mentioned_service == "s3":
#             if "s3" not in existing_services:
#                 return "❌ There's no S3 bucket in your blueprint. Try 'add s3' first."
#             return "ℹ️ S3 has no configurable options. You can 'remove s3' if you no longer need it."

#     # ── Fallback: show current state + help ──────────────────────────────────
#     return (
#         f"❓ I can **add**, **change**, or **remove** components.\n\n"
#         f"Current blueprint: {_current_components(components)}\n\n"
#         "Examples:\n"
#         "• 'add a database'\n"
#         "• 'change instance to t3.medium'\n"
#         "• 'remove the load balancer'\n"
#         "• 'scale eks to 4 nodes'"
#     )


# def finalize_modification(bp, attrs, summary_text):
#     attrs["infra_blueprint"] = json.dumps(bp)
#     attrs["conversation_state"] = STATE_WAITING_PLAN_CONFIRM
#     new_cost = calculate_cost(bp)

#     # Build a readable component list for the confirmation message
#     component_lines = []
#     for c in bp.get("components", []):
#         svc = c.get("service")
#         if svc == "vpc":   component_lines.append("• 🛜 VPC")
#         elif svc == "ec2": component_lines.append(f"• 🖥 EC2 ({c.get('compute',{}).get('instance_type','t3.micro')})")
#         elif svc == "alb": component_lines.append("• 🌐 ALB")
#         elif svc == "rds": component_lines.append(f"• 🗄 RDS ({c.get('database',{}).get('engine','postgres')} / {c.get('database',{}).get('instance_type','db.t3.micro')})")
#         elif svc == "s3":  component_lines.append("• 🪣 S3")
#         elif svc == "eks": component_lines.append(f"• ☸️ EKS (max {c.get('container',{}).get('max_nodes',2)} nodes)")

#     components_summary = "\n".join(component_lines)

#     return (
#         f"✅ **Blueprint Updated** — {summary_text}\n\n"
#         f"📦 **Current Blueprint:**\n{components_summary}\n\n"
#         f"💰 Est. cost: **${new_cost}/month**\n\n"
#         "Should I generate the Terraform plan for these changes?"
#     )


# # =====================================================
# # TERMINATE
# # =====================================================

# def handle_terminate_intent(text, attrs):
#     if "project_id" not in attrs:
#         return "❌ I don't have a project context. Please select a project in the dashboard first."

#     text = text.lower().strip()

#     # Destroy All — go straight to DESTROY_STARTED, no extra confirmation step.
#     # TerraformPlanView in destroyMode IS the confirmation UI.
#     if any(word in text for word in ["all", "everything", "whole", "entire", "environment", "teardown"]):
#         attrs.update({
#             "conversation_state": STATE_IDLE,
#             "action": "DESTROY"
#         })
#         # Signal the frontend to kick off the destroy job immediately
#         return "__DESTROY_ALL__"

#     if "infra_blueprint" not in attrs:
#         return "❌ I don't see any active infrastructure in this session to modify. Try 'destroy everything' if you want a full cleanup."

#     try:
#         bp = json.loads(attrs["infra_blueprint"])
#     except Exception:
#         return "❌ Error reading current infrastructure state."

#     service_map = [
#         ("ec2", ["ec2", "compute", "server", "instance"]),
#         ("rds", ["database", "rds", "postgres", "sql"]),
#         ("s3", ["storage", "s3", "bucket"]),
#         ("eks", ["eks", "kubernetes", "k8s", "cluster"]),
#         ("alb", ["alb", "load balancer", "traffic"])
#     ]

#     targets = [s for s, keys in service_map if any(k in text for k in keys)]

#     if not targets:
#         return "❓ Which service should I terminate? (e.g., 'delete the database' or 'remove the S3 bucket')"

#     initial_count = len(bp.get("components", []))
#     bp["components"] = [c for c in bp.get("components", []) if c.get("service") not in targets]

#     if len(bp["components"]) == initial_count:
#         return f"❌ I couldn't find {', '.join(targets).upper()} in your current configuration."

#     attrs.update({
#         "infra_blueprint": json.dumps(bp),
#         "conversation_state": STATE_WAITING_PLAN_CONFIRM,
#         "action": "DESTROY"
#     })

#     removed_list = ", ".join([t.upper() for t in targets])
#     return f"⚠️ I've staged the removal of: **{removed_list}**. Should I generate the updated plan?"


# # =====================================================
# # MAIN WEBHOOK
# # =====================================================


# # =====================================================
# # MAIN WEBHOOK
# # =====================================================

# def lex_webhook(event):
#     intent_name = event["sessionState"]["intent"]["name"]
#     logger.info(f"Intent: {intent_name}")
#     attrs = event["sessionState"].get("sessionAttributes") or {}

#     text = event.get("inputTranscript", "")
#     if not text and event.get("transcriptions"):
#         text = event["transcriptions"][0].get("transcription", "")
#     text = text.lower().strip()

#     if "project_id" not in attrs:
#         logger.warning("WARNING: Lex did not receive a project_id from the React frontend!")

#     state = attrs.get("conversation_state", STATE_IDLE)

#     is_yes = any(word in text.split() for word in ["yes", "proceed", "generate", "ok", "sure", "y", "do it"])
#     is_no  = any(word in text.split() for word in ["no", "cancel", "stop", "discard", "n"])

#     # =====================================================
#     # STATE INTERCEPT
#     # Runs BEFORE intent routing so that yes/no confirmation
#     # turns work even when Lex mis-routes them to ModifyInfraIntent.
#     # =====================================================

#     if state == "WAITING_FOR_APP_TYPE":
#         app_type = parse_application_intent(text)
#         if not app_type:
#             return build_response("CreateInfraIntent", f"I heard '{text}', but did not recognize the app type. Try typing exactly 'S3 Bucket', 'Web App', or 'K8s'.", attrs)
#         env = parse_environment(text) or DEFAULT_ENV
#         bp = build_blueprint(app_type, env)
#         cost = calculate_cost(bp)
#         attrs.update({"infra_blueprint": json.dumps(bp), "conversation_state": STATE_WAITING_PLAN_CONFIRM})
#         return build_response("CreateInfraIntent", generate_detailed_plan_summary(bp, cost), attrs)

#     if state == STATE_WAITING_PLAN_CONFIRM:
#         if is_yes:
#             bp = json.loads(attrs.get("infra_blueprint", "{}"))
#             logger.info(f"is_yes -- bp keys: {list(bp.keys())}, destroy_all: {bp.get('destroy_all')}")

#             if bp.get("destroy_all"):
#                 result, error = call_backend(BACK_DESTROY_URL, {"project_id": attrs["project_id"]})
#                 action_msg = "Initiating FULL destruction of environment..."
#                 ui_type = "DESTROY_STARTED"
#             else:
#                 result, error = call_backend(BACK_PLAN_URL, {"project_id": attrs["project_id"], "infra_blueprint": bp})
#                 action_msg = "Generating Terraform plan for changes..."
#                 ui_type = "PLAN_STARTED"

#             if error:
#                 return build_response(intent_name, f"Request failed: {error}", {"conversation_state": STATE_IDLE, "project_id": attrs["project_id"]})

#             attrs.update({"job_id": result.get("job_id"), "conversation_state": STATE_IDLE})
#             return build_response(intent_name, action_msg, attrs, {"type": ui_type, "job_id": result.get("job_id")})

#         if is_no:
#             attrs["conversation_state"] = STATE_IDLE
#             attrs.pop("action", None)
#             attrs.pop("infra_blueprint", None)
#             return build_response(intent_name, "Action cancelled. No changes were made.", attrs)

#     # =====================================================
#     # INTENT ROUTING
#     # ModifyInfraIntent comes AFTER state intercept so that
#     # yes/no confirmation turns are never accidentally routed here.
#     # =====================================================

#     if intent_name == "ModifyInfraIntent":
#         logger.info(f"Processing Modify Intent: {text}")
#         msg = handle_modify_intent(text, attrs)
#         return build_response(intent_name, msg, attrs)

#     if intent_name == "CreateInfraIntent":
#         app_type = parse_application_intent(text)
#         if not app_type:
#             attrs["conversation_state"] = "WAITING_FOR_APP_TYPE"
#             return build_response(intent_name, "What would you like to deploy? (Web App, K8s, S3 Bucket, etc.)", attrs)
#         env = parse_environment(text) or DEFAULT_ENV
#         bp = build_blueprint(app_type, env)
#         cost = calculate_cost(bp)
#         attrs.update({"infra_blueprint": json.dumps(bp), "conversation_state": STATE_WAITING_PLAN_CONFIRM})
#         return build_response(intent_name, generate_detailed_plan_summary(bp, cost), attrs)

#     if intent_name == "TerminateInfraIntent":
#         msg = handle_terminate_intent(text, attrs)

#         if msg == "__DESTROY_ALL__":
#             result, error = call_backend(BACK_DESTROY_URL, {"project_id": attrs["project_id"]})
#             if error:
#                 return build_response(intent_name, f"Destroy failed: {error}", attrs)
#             attrs.update({"job_id": result.get("job_id"), "conversation_state": STATE_IDLE})
#             return build_response(
#                 intent_name,
#                 "Initiating FULL destruction of environment...",
#                 attrs,
#                 {"type": "DESTROY_STARTED", "job_id": result.get("job_id")}
#             )

#         return build_response(intent_name, msg, attrs)

#     if intent_name == "StatusInfraIntent":
#         return build_response(intent_name, "Checking status...", attrs)

#     return build_response(intent_name, "I did not catch that. Try 'deploy a web app'.", {"conversation_state": STATE_IDLE})


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
STATE_WAITING_DESTROY_CONFIRM = "WAITING_DESTROY_CONFIRM"

# =========================================================
# NLP PARSING
# =========================================================

def parse_environment(text):
    if "prod" in text: return "production"
    if "staging" in text: return "staging"
    return None

def parse_application_intent(text):
    text = text.lower()
    if any(w in text for w in ["static", "s3", "bucket"]): return "static_website"
    if any(w in text for w in ["kubernetes", "cluster", "k8s", "eks"]): return "k8s_cluster"
    if any(w in text for w in ["database", "rds", "postgres", "sql"]): return "database"
    if "api" in text: return "api_backend"
    if any(w in text for w in ["web app", "webapp", "page", "website", "react", "frontend"]): return "web_app"
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
    lines.append(f"\nDo you want to generate the Terraform plan?")
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

def call_backend_get(url):
    try:
        req = urllib.request.Request(url, method="GET")
        response = urllib.request.urlopen(req, timeout=5)
        return json.loads(response.read().decode()), None
    except Exception as e:
        logger.error(f"GET Call Failed: {str(e)}")
        return None, str(e)

def build_response(intent_name, message, attrs, payload=None):
    """
    If payload is provided, it MUST include a "message" key — otherwise the
    frontend will read payload.message as undefined and render an empty bubble.
    When no payload is needed, omit it entirely so the plain messages[] path is used.
    """
    if payload:
        # Always inject the message into the payload so the frontend can read it
        payload["message"] = message
        attrs["ui_payload"] = json.dumps(payload)
    else:
        attrs["ui_payload"] = json.dumps({"type": "MESSAGE", "message": message})

    return {
        "sessionState": {
            "sessionAttributes": attrs,
            "dialogAction": {"type": "Close"},
            "intent": {"name": intent_name, "state": "Fulfilled"}
        },
        "messages": [{"contentType": "PlainText", "content": message}]
    }

# =====================================================
# MODIFY
# =====================================================

# Canonical service keyword map — used for both add and remove detection
SERVICE_KEYWORDS = {
    "ec2":  ["ec2", "compute", "server", "instance"],
    "rds":  ["database", "rds", "postgres", "mysql", "mariadb", "sql"],
    "s3":   ["s3", "bucket", "storage", "static"],
    "alb":  ["alb", "load balancer", "loadbalancer"],
    "eks":  ["eks", "kubernetes", "k8s", "cluster"],
}

EC2_TYPES  = ["t2.micro", "t2.small", "t3.micro", "t3.small", "t3.medium", "t3.large"]
RDS_TYPES  = ["db.t3.micro", "db.t3.small", "db.t3.medium"]
RDS_ENGINES = ["postgres", "mysql", "mariadb"]
EKS_NODE_COUNTS = ["1", "2", "3", "4", "5", "6", "8", "10"]

def _service_in_text(service, text):
    """Return True if any keyword for this service appears in text."""
    return any(kw in text for kw in SERVICE_KEYWORDS.get(service, []))

def _current_components(components):
    """Return a readable summary of active non-VPC components."""
    active = [c["service"].upper() for c in components if c["service"] != "vpc"]
    return ", ".join(active) if active else "none"

def handle_modify_intent(text, attrs):
    project_id = attrs.get("project_id")
    text = text.lower()

    # ── Blueprint hydration ──────────────────────────────────────────────────
    if "infra_blueprint" not in attrs and project_id:
        data, error = call_backend_get(f"{LOCAL_BASE}/projects/{project_id}/blueprint")
        if data and data.get("blueprint"):
            attrs["infra_blueprint"] = json.dumps(data["blueprint"])
            logger.info(f"Hydrated blueprint for project {project_id}")

    if "infra_blueprint" not in attrs:
        return "❌ I don't see an active configuration. Try deploying something first."

    bp = json.loads(attrs["infra_blueprint"])
    components = bp.get("components", [])
    existing_services = {c["service"] for c in components}

    # ── Determine intent verb: add / remove / change ─────────────────────────
    is_add    = any(w in text for w in ["add", "include", "attach", "also need", "also want"])
    is_remove = any(w in text for w in ["remove", "delete", "drop", "get rid of", "without"])
    is_change = any(w in text for w in ["change", "update", "upgrade", "downgrade", "switch", "use", "set", "scale", "resize", "modify"])

    # Identify which service the user is referring to
    mentioned_service = next(
        (svc for svc in SERVICE_KEYWORDS if _service_in_text(svc, text)),
        None
    )

    logger.info(f"Modify — verb: add={is_add} remove={is_remove} change={is_change} | service={mentioned_service}")
    logger.info(f"Existing services: {existing_services}")

    # ── ADD ──────────────────────────────────────────────────────────────────
    if is_add:
        if not mentioned_service:
            return (
                "➕ **What would you like to add?**\n"
                f"Current components: {_current_components(components)}\n\n"
                "Available to add:\n"
                "• EC2 (compute server)\n"
                "• RDS (database)\n"
                "• S3 (storage bucket)\n"
                "• ALB (load balancer)\n"
                "• EKS (Kubernetes cluster)"
            )

        if mentioned_service in existing_services:
            return f"⚠️ Your blueprint already has **{mentioned_service.upper()}**. Did you want to modify it instead?"

        # Add the new component with sensible defaults
        if mentioned_service == "ec2":
            bp["components"].append({"type": "compute", "service": "ec2", "compute": {"instance_type": "t3.micro"}})
        elif mentioned_service == "rds":
            engine = next((e for e in RDS_ENGINES if e in text), "postgres")
            size   = next((s for s in RDS_TYPES if s in text), "db.t3.micro")
            bp["components"].append({"type": "database", "service": "rds", "database": {"engine": engine, "instance_type": size}})
        elif mentioned_service == "s3":
            bp["components"].append({"type": "storage", "service": "s3"})
        elif mentioned_service == "alb":
            bp["components"].append({"type": "traffic", "service": "alb"})
        elif mentioned_service == "eks":
            bp["components"].append({"type": "container", "service": "eks", "container": {"min_nodes": 1, "max_nodes": 2}})

        return finalize_modification(bp, attrs, f"Added {mentioned_service.upper()}")

    # ── REMOVE ───────────────────────────────────────────────────────────────
    if is_remove:
        if not mentioned_service:
            removable = [c["service"].upper() for c in components if c["service"] != "vpc"]
            if not removable:
                return "📭 Nothing removable — your blueprint only contains the core VPC."
            return (
                "🗑️ **Which component should I remove?**\n• "
                + "\n• ".join(removable)
            )

        if mentioned_service == "vpc":
            return "❌ VPC is the core network layer and cannot be removed."

        if mentioned_service not in existing_services:
            return f"❌ **{mentioned_service.upper()}** is not in your current blueprint."

        bp["components"] = [c for c in components if c["service"] != mentioned_service]
        return finalize_modification(bp, attrs, f"Removed {mentioned_service.upper()}")

    # ── CHANGE / UPDATE ───────────────────────────────────────────────────────
    if is_change or mentioned_service:

        # EC2 instance type change
        if mentioned_service == "ec2" or (not mentioned_service and "instance" in text):
            if "ec2" not in existing_services:
                return "❌ There's no EC2 instance in your blueprint to modify. Try 'add ec2' first."
            chosen = next((t for t in EC2_TYPES if t in text), None)
            if not chosen:
                current = next((c["compute"]["instance_type"] for c in components if c["service"] == "ec2"), "unknown")
                return (
                    f"🖥️ **EC2 Instance Type** (current: `{current}`)\n"
                    "Choose a new type:\n• " + "\n• ".join(EC2_TYPES)
                )
            for c in bp["components"]:
                if c["service"] == "ec2":
                    c["compute"]["instance_type"] = chosen
            return finalize_modification(bp, attrs, f"EC2 instance type → {chosen}")

        # RDS engine or size change
        if mentioned_service == "rds":
            if "rds" not in existing_services:
                return "❌ There's no RDS database in your blueprint. Try 'add database' first."
            new_engine = next((e for e in RDS_ENGINES if e in text), None)
            new_size   = next((s for s in RDS_TYPES if s in text), None)
            if not new_engine and not new_size:
                current = next((c["database"] for c in components if c["service"] == "rds"), {})
                return (
                    f"🗄️ **RDS Database** (current: `{current.get('engine','postgres')}` / `{current.get('instance_type','db.t3.micro')}`)\n\n"
                    f"• Engines: {', '.join(RDS_ENGINES)}\n"
                    f"• Sizes: {', '.join(RDS_TYPES)}\n\n"
                    "Example: 'change database to mysql' or 'use db.t3.small'"
                )
            for c in bp["components"]:
                if c["service"] == "rds":
                    if new_engine: c["database"]["engine"] = new_engine
                    if new_size:   c["database"]["instance_type"] = new_size
            label = f"RDS → {new_engine or ''} {new_size or ''}".strip()
            return finalize_modification(bp, attrs, label)

        # EKS node scaling
        if mentioned_service == "eks":
            if "eks" not in existing_services:
                return "❌ There's no EKS cluster in your blueprint. Try 'add eks' first."
            new_count = next((int(n) for n in EKS_NODE_COUNTS if n in text), None)
            if not new_count:
                current = next((c["container"] for c in components if c["service"] == "eks"), {})
                return (
                    f"☸️ **EKS Cluster** (current: min={current.get('min_nodes',1)} max={current.get('max_nodes',2)})\n\n"
                    "Example: 'scale eks to 3 nodes' or 'set max nodes to 5'"
                )
            for c in bp["components"]:
                if c["service"] == "eks":
                    c["container"]["min_nodes"] = 1
                    c["container"]["max_nodes"] = new_count
            return finalize_modification(bp, attrs, f"EKS → max {new_count} nodes")

        # ALB — nothing to configure, just inform
        if mentioned_service == "alb":
            if "alb" not in existing_services:
                return "❌ There's no ALB in your blueprint. Try 'add load balancer' first."
            return "ℹ️ ALB has no configurable options. You can 'remove alb' if you no longer need it."

        # S3 — nothing to configure
        if mentioned_service == "s3":
            if "s3" not in existing_services:
                return "❌ There's no S3 bucket in your blueprint. Try 'add s3' first."
            return "ℹ️ S3 has no configurable options. You can 'remove s3' if you no longer need it."

    # ── Fallback: show current state + help ──────────────────────────────────
    return (
        f"❓ I can **add**, **change**, or **remove** components.\n\n"
        f"Current blueprint: {_current_components(components)}\n\n"
        "Examples:\n"
        "• 'add a database'\n"
        "• 'change instance to t3.medium'\n"
        "• 'remove the load balancer'\n"
        "• 'scale eks to 4 nodes'"
    )


def finalize_modification(bp, attrs, summary_text):
    attrs["infra_blueprint"] = json.dumps(bp)
    attrs["conversation_state"] = STATE_WAITING_PLAN_CONFIRM
    new_cost = calculate_cost(bp)

    # Build a readable component list for the confirmation message
    component_lines = []
    for c in bp.get("components", []):
        svc = c.get("service")
        if svc == "vpc":   component_lines.append("• 🛜 VPC")
        elif svc == "ec2": component_lines.append(f"• 🖥 EC2 ({c.get('compute',{}).get('instance_type','t3.micro')})")
        elif svc == "alb": component_lines.append("• 🌐 ALB")
        elif svc == "rds": component_lines.append(f"• 🗄 RDS ({c.get('database',{}).get('engine','postgres')} / {c.get('database',{}).get('instance_type','db.t3.micro')})")
        elif svc == "s3":  component_lines.append("• 🪣 S3")
        elif svc == "eks": component_lines.append(f"• ☸️ EKS (max {c.get('container',{}).get('max_nodes',2)} nodes)")

    components_summary = "\n".join(component_lines)

    return (
        f"✅ **Blueprint Updated** — {summary_text}\n\n"
        f"📦 **Current Blueprint:**\n{components_summary}\n\n"
        f"💰 Est. cost: **${new_cost}/month**\n\n"
        "Should I generate the Terraform plan for these changes?"
    )


# =====================================================
# TERMINATE
# =====================================================

def handle_terminate_intent(text, attrs):
    if "project_id" not in attrs:
        return {"msg": "I don't have a project context. Please select a project first.", "state": None}

    text = text.lower().strip()

    # Destroy All
    if any(word in text for word in ["all", "everything", "whole", "entire", "environment", "teardown"]):
        attrs.update({"conversation_state": STATE_WAITING_DESTROY_CONFIRM, "pending_destroy_scope": "ALL"})
        attrs.pop("infra_blueprint", None)  # clear any stale blueprint
        return {
            "msg": "CRITICAL: This will permanently destroy EVERY resource in your environment. This cannot be undone. Are you absolutely sure?",
            "state": STATE_WAITING_DESTROY_CONFIRM
        }

    # Partial destroy — requires a blueprint to be in session
    if "infra_blueprint" not in attrs:
        return {"msg": "I don't see any active infrastructure in this session. Try 'destroy everything' for a full teardown.", "state": None}

    try:
        bp = json.loads(attrs["infra_blueprint"])
    except Exception:
        return {"msg": "Error reading current infrastructure state.", "state": None}

    service_map = [
        ("ec2", ["ec2", "compute", "server", "instance"]),
        ("rds", ["database", "rds", "postgres", "sql"]),
        ("s3",  ["storage", "s3", "bucket"]),
        ("eks", ["eks", "kubernetes", "k8s", "cluster"]),
        ("alb", ["alb", "load balancer", "traffic"])
    ]

    targets = [s for s, keys in service_map if any(k in text for k in keys)]

    if not targets:
        active = [c["service"].upper() for c in bp.get("components", []) if c["service"] != "vpc"]
        return {"msg": f"Which service should I remove? Active: {', '.join(active) or 'none'}. Example: 'destroy the database'", "state": None}

    initial_count = len(bp.get("components", []))
    bp["components"] = [c for c in bp.get("components", []) if c.get("service") not in targets]

    if len(bp["components"]) == initial_count:
        return {"msg": f"Could not find {', '.join(t.upper() for t in targets)} in your current configuration.", "state": None}

    removed_list = ", ".join(t.upper() for t in targets)
    attrs.update({
        "infra_blueprint": json.dumps(bp),
        "conversation_state": STATE_WAITING_DESTROY_CONFIRM,
        "pending_destroy_scope": "PARTIAL",
        "pending_destroy_targets": json.dumps(targets)
    })
    return {
        "msg": f"WARNING: I will permanently remove {removed_list} from your infrastructure. Are you sure you want to proceed?",
        "state": STATE_WAITING_DESTROY_CONFIRM
    }


# =====================================================
# MAIN WEBHOOK
# =====================================================

def lex_webhook(event):
    intent_name = event["sessionState"]["intent"]["name"]
    logger.info(f"Intent: {intent_name}")
    attrs = event["sessionState"].get("sessionAttributes") or {}

    text = event.get("inputTranscript", "")
    if not text and event.get("transcriptions"):
        text = event["transcriptions"][0].get("transcription", "")
    text = text.lower().strip()

    if "project_id" not in attrs:
        logger.warning("WARNING: Lex did not receive a project_id from the React frontend!")

    state = attrs.get("conversation_state", STATE_IDLE)

    is_yes = any(word in text.split() for word in ["yes", "proceed", "confirm", "ok", "sure", "y", "do it"])
    is_no  = any(word in text.split() for word in ["no", "cancel", "stop", "abort", "n"])

    # =====================================================
    # STATE INTERCEPT
    # Runs BEFORE intent routing so that yes/no confirmation
    # turns work even when Lex mis-routes them.
    # =====================================================

    if state == "WAITING_FOR_APP_TYPE":
        app_type = parse_application_intent(text)
        if not app_type:
            return build_response("CreateInfraIntent",
                f"I heard '{text}', but did not recognize the app type. Try 'S3 Bucket', 'Web App', or 'K8s'.", attrs)
        env = parse_environment(text) or DEFAULT_ENV
        bp = build_blueprint(app_type, env)
        cost = calculate_cost(bp)
        attrs.update({"infra_blueprint": json.dumps(bp), "conversation_state": STATE_WAITING_PLAN_CONFIRM})
        return build_response("CreateInfraIntent", generate_detailed_plan_summary(bp, cost), attrs)

    # ── Plan confirmation (create / modify flows) ─────────────────────────────
    if state == STATE_WAITING_PLAN_CONFIRM:
        if is_yes:
            bp = json.loads(attrs.get("infra_blueprint", "{}"))
            logger.info(f"PLAN CONFIRM yes -- bp keys: {list(bp.keys())}")
            result, error = call_backend(BACK_PLAN_URL, {"project_id": attrs["project_id"], "infra_blueprint": bp})
            if error:
                attrs["conversation_state"] = STATE_IDLE
                return build_response(intent_name, f"Request failed: {error}", attrs)
            attrs.update({"job_id": result.get("job_id"), "conversation_state": STATE_IDLE})
            return build_response(intent_name, "Generating Terraform plan for changes...", attrs,
                                  {"type": "PLAN_STARTED", "job_id": result.get("job_id")})
        if is_no:
            attrs["conversation_state"] = STATE_IDLE
            attrs.pop("action", None)
            attrs.pop("infra_blueprint", None)
            return build_response(intent_name, "Action cancelled. No changes were made.", attrs)

    # ── Destroy confirmation (dedicated state — never mixed with plan confirm) ─
    if state == STATE_WAITING_DESTROY_CONFIRM:
        if is_yes:
            scope = attrs.get("pending_destroy_scope", "ALL")
            project_id = attrs["project_id"]

            if scope == "ALL":
                payload = {"project_id": project_id}
            else:
                # Partial destroy — send the updated blueprint (targets already removed)
                bp = json.loads(attrs.get("infra_blueprint", "{}"))
                payload = {"project_id": project_id, "infra_blueprint": bp}

            result, error = call_backend(BACK_DESTROY_URL, payload)
            if error:
                attrs["conversation_state"] = STATE_IDLE
                return build_response(intent_name, f"Destroy request failed: {error}", attrs)

            # Clean up destroy-specific session state
            attrs.update({"job_id": result.get("job_id"), "conversation_state": STATE_IDLE})
            attrs.pop("pending_destroy_scope", None)
            attrs.pop("pending_destroy_targets", None)
            attrs.pop("infra_blueprint", None)

            scope_label = "FULL environment" if scope == "ALL" else "selected resources"
            return build_response(
                intent_name,
                f"Initiating destruction of {scope_label}...",
                attrs,
                {"type": "DESTROY_STARTED", "job_id": result.get("job_id")}
            )

        if is_no:
            attrs["conversation_state"] = STATE_IDLE
            attrs.pop("pending_destroy_scope", None)
            attrs.pop("pending_destroy_targets", None)
            attrs.pop("infra_blueprint", None)
            return build_response(intent_name, "Destruction cancelled. Your infrastructure is safe.", attrs)

    # =====================================================
    # INTENT ROUTING
    # ModifyInfraIntent comes AFTER state intercept so that
    # yes/no confirmation turns are never accidentally routed here.
    # =====================================================

    if intent_name == "ModifyInfraIntent":
        logger.info(f"Processing Modify Intent: {text}")
        msg = handle_modify_intent(text, attrs)
        return build_response(intent_name, msg, attrs)

    if intent_name == "CreateInfraIntent":
        app_type = parse_application_intent(text)
        if not app_type:
            attrs["conversation_state"] = "WAITING_FOR_APP_TYPE"
            return build_response(intent_name, "What would you like to deploy? (Web App, K8s, S3 Bucket, etc.)", attrs)
        env = parse_environment(text) or DEFAULT_ENV
        bp = build_blueprint(app_type, env)
        cost = calculate_cost(bp)
        attrs.update({"infra_blueprint": json.dumps(bp), "conversation_state": STATE_WAITING_PLAN_CONFIRM})
        return build_response(intent_name, generate_detailed_plan_summary(bp, cost), attrs)

    if intent_name == "TerminateInfraIntent":
        result = handle_terminate_intent(text, attrs)
        return build_response(intent_name, result["msg"], attrs)

    if intent_name == "StatusInfraIntent":
        return build_response(intent_name, "Checking status...", attrs)

    return build_response(intent_name, "I did not catch that. Try 'deploy a web app'.", {"conversation_state": STATE_IDLE})
