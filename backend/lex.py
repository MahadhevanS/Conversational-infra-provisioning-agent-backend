import json
import logging
import urllib.request
import uuid

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ================= CONFIG & COST DATA =================

DEFAULT_REGION = "us-east-1"
VALID_ENVIRONMENTS = ["development", "production"]

VALID_EC2_TYPES = ["t2.micro", "t2.small", "t2.medium", "t3.micro", "t3.small"]
EC2_COST = {"t2.micro": 8, "t2.small": 15, "t2.medium": 25, "t3.micro": 10, "t3.small": 18}

VALID_RDS_TYPES = ["db.t3.micro", "db.t3.small", "db.m5.large"]
RDS_COST = {"db.t3.micro": 15, "db.t3.small": 30, "db.m5.large": 100}

S3_BASE_COST = 5
VERSIONING_COST = 2

EKS_BASE_COST = 40
NODE_COST = 20

# ================= APPROVAL STATES =================

APPROVAL_PENDING = "PENDING_APPROVAL"
APPROVAL_APPROVED = "APPROVED"
APPROVAL_REJECTED = "REJECTED"

# ================= BACKEND =================

BACKEND_URL = "https://unmicrobial-suzie-unapprehendably.ngrok-free.dev/plan"

BACKEND_APPLY_URL = "https://unmicrobial-suzie-unapprehendably.ngrok-free.dev/apply"



# ================= BACKEND CALLS =================

import urllib.error

def call_backend_plan(infra_blueprint):
    try:
        req = urllib.request.Request(
            BACKEND_URL,
            data=json.dumps(infra_blueprint).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        # Increase timeout (backend/ngrok may be slow)
        response = urllib.request.urlopen(req, timeout=30)

        result = json.loads(response.read().decode())
        job_id = result.get("job_id")
        if not job_id:
            return None, "Failed to start plan job."
        return job_id, None

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        logger.error(f"PLAN HTTPError {e.code}: {body}")
        return None, f"Backend PLAN HTTP {e.code}"

    except urllib.error.URLError as e:
        logger.error(f"PLAN URLError: {e.reason}")
        return None, f"Backend PLAN URL error: {e.reason}"

    except Exception as e:
        logger.error(f"PLAN Unexpected error: {str(e)}")
        return None, "Backend unreachable."


def call_backend_apply(job_id, infra_blueprint):
    try:
        payload = {
            "job_id": job_id,
            "infra_blueprint": infra_blueprint
        }

        req = urllib.request.Request(
            BACKEND_APPLY_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        response = urllib.request.urlopen(req, timeout=10)
        result = json.loads(response.read().decode())

        apply_job_id = result.get("apply_job_id") or result.get("job_id")
        if not apply_job_id:
            return None, "Failed to start apply job."

        return apply_job_id, None

    except Exception as e:
        logger.error(f"Backend APPLY call failed: {str(e)}")
        return None, "Backend apply unreachable."


# ================= NLP HELPERS =================

def parse_services(text):
    if not text: return []
    text = text.lower()
    services = []
    if any(x in text for x in ["ec2", "compute", "server"]): services.append("ec2")
    if any(x in text for x in ["database", "rds", "db"]): services.append("rds")
    if any(x in text for x in ["storage", "s3", "bucket"]): services.append("s3")
    if any(x in text for x in ["container", "eks", "kubernetes"]): services.append("eks")
    return list(set(services))

def get_slot(slots, name):
    if not slots: return None
    slot_data = slots.get(name)
    if not slot_data: return None
    try:
        return slot_data.get("value", {}).get("interpretedValue")
    except Exception:
        return None

# ================= RESPONSE HELPERS =================

def elicit_slot(intent, slots, slot, msg, attrs):
    return {
        "sessionState": {
            "sessionAttributes": attrs,
            "dialogAction": {"type": "ElicitSlot", "slotToElicit": slot},
            "intent": {"name": intent, "slots": slots, "state": "InProgress"}
        },
        "messages": [{"contentType": "PlainText", "content": msg}]
    }

def confirm_intent(intent, slots, msg, attrs):
    return {
        "sessionState": {
            "sessionAttributes": attrs,
            "dialogAction": {"type": "ConfirmIntent"},
            "intent": {"name": intent, "slots": slots, "state": "InProgress"}
        },
        "messages": [{"contentType": "PlainText", "content": msg}]
    }

def build_ui_attrs(attrs, message, bp, total_cost):
    # Construct a structured cost object for the React component
    resources_list = []
    for comp in bp.get("components", []):
        service = comp.get("service", "").upper()
        if service == "VPC": continue # Skip network for simple cost view
        
        # Determine specific cost for this component
        comp_cost = 0
        if service == "EC2":
            comp_cost = EC2_COST.get(comp["compute"]["instance_type"], 0)
        elif service == "RDS":
            comp_cost = RDS_COST.get(comp["database"]["instance_type"], 0)
        
        resources_list.append({
            "name": f"AWS {service}",
            "cost": str(comp_cost),
            "components": [
                {
                    "name": f"Provisioning {service} usage",
                    "cost": str(comp_cost),
                    "unit": "month",
                    "quantity": 1
                }
            ]
        })

    cost_obj = {
        "total": str(total_cost),
        "currency": "USD",
        "resources": resources_list,
        "version": "1.0-mock"
    }

    ui_data = {
        "message": message,
        "cost": cost_obj, # Pass the object, not just the number
        "ui": {
            "conversationName": "Infrastructure Plan",
            "disableInput": False
        }
    }
    attrs["ui_payload"] = json.dumps(ui_data)
    return attrs

def close(intent, slots, msg, attrs):
    # Clear the slots for the current intent so they don't 
    # trigger a 're-fill' loop on the next message
    empty_slots = {k: None for k in slots.keys()} if slots else {}

    return {
        "sessionState": {
            "sessionAttributes": attrs,
            "dialogAction": {"type": "Close"},
            "intent": {
                "name": intent, 
                "slots": empty_slots, # Send back empty slots
                "state": "Fulfilled"
            }
        },
        "messages": [{"contentType": "PlainText", "content": msg}]
    }

# ================= BLUEPRINT LOGIC =================

def build_blueprint(env, services, ec2_type=None):
    bp = {
        "environment": env,
        "region": DEFAULT_REGION,
        "components": [{"type": "network", "service": "vpc"}]
    }

    if "ec2" in services:
        bp["components"].append({
            "type": "compute", "service": "ec2",
            "compute": {"instance_type": ec2_type or "t2.micro"}
        })

    if "rds" in services:
        bp["components"].append({
            "type": "database", "service": "rds",
            "database": {"instance_type": "db.t3.micro"} 
        })

    if "s3" in services:
        bp["components"].append({
            "type": "storage", "service": "s3",
            "storage": {"versioning": False} 
        })

    if "eks" in services:
        bp["components"].append({
            "type": "container", "service": "eks",
            "container": {"min_nodes": 1, "max_nodes": 2} 
        })

    return bp

def recalc_cost(bp):
    cost = 0
    for c in bp["components"]:
        if c["type"] == "compute":
            cost += EC2_COST.get(c["compute"]["instance_type"], 0)
        
        if c["type"] == "database":
            cost += RDS_COST.get(c["database"]["instance_type"], 0)
            
        if c["type"] == "storage":
            cost += S3_BASE_COST
            if c["storage"].get("versioning") == "true":
                cost += VERSIONING_COST
            
        if c["type"] == "container":
            nodes = int(c["container"].get("max_nodes", 1))
            cost += EKS_BASE_COST + (nodes * NODE_COST)
            
    return cost

# ================= CREATE HANDLER =================

def handle_create(intent, slots, attrs, text):
    saved_services = []
    if "temp_services" in attrs:
        try: saved_services = json.loads(attrs["temp_services"])
        except: saved_services = []

    current_services = parse_services(text)
    all_services = list(set(saved_services + current_services))
    attrs["temp_services"] = json.dumps(all_services)

    env = get_slot(slots, "environment")
    if not env:
        return elicit_slot(intent, slots, "environment", "Development or production?", attrs)
    
    if env.lower() not in VALID_ENVIRONMENTS:
        return elicit_slot(intent, slots, "environment", "Choose development or production.", attrs)

    if not all_services:
        return elicit_slot(intent, slots, "services", "Which service? Compute, database, storage, or containers?", attrs)

    ec2_type = None
    if "ec2" in all_services:
        ec2_type = get_slot(slots, "instance_type")
        if not ec2_type:
            return elicit_slot(intent, slots, "instance_type", "Which instance type? (e.g. t2.micro)", attrs)
        if ec2_type not in VALID_EC2_TYPES:
            return elicit_slot(intent, slots, "instance_type", f"Invalid. Choose: {', '.join(VALID_EC2_TYPES)}", attrs)

    bp = build_blueprint(env, all_services, ec2_type)
    cost = recalc_cost(bp)

    logger.info("Blueprint being sent to backend")
    logger.info(json.dumps(bp))

    job_id, error = call_backend_plan(bp)

    if error:
        return close(intent, slots, f"❌ {error}", attrs)

    if "temp_services" in attrs: del attrs["temp_services"]

    run_id = str(uuid.uuid4())

    # Define the message BEFORE using it in build_ui_attrs
    msg = (f"🚀 Plan creation initiated successfully!\n\n"
           f"Services: {', '.join(all_services).upper()}\n"
           f"Plan Job ID: {job_id}\n\n"
           f"The Plan will be available soon.\n")

    attrs.update({
        "infra_blueprint": json.dumps(bp),
        "active_services": json.dumps(all_services),
        "estimated_cost": str(cost),
        "last_action": "CREATE",
        "approval_status": APPROVAL_PENDING,
        "plan_job_id": job_id,
        "run_id": run_id
    })

    attrs = build_ui_attrs(attrs, msg, bp, cost)

    return close(intent, slots, msg, attrs)


# ================= MODIFY HANDLER =================

def handle_modify(intent, slots, attrs, confirm, text):

    attrs["approval_status"] = None
    attrs.pop("plan_job_id", None)
    attrs.pop("apply_job_id", None)
    attrs.pop("approval_status", None)
    attrs.pop("run_id", None)

    if "infra_blueprint" not in attrs:
        return close(intent, slots, "❌ No infrastructure found. Create first.", attrs)

    bp = json.loads(attrs["infra_blueprint"])
    try: current_active = json.loads(attrs["active_services"])
    except: current_active = []

    target_services = parse_services(text) or current_active
    
    if not target_services:
        return close(intent, slots, "❌ Please specify what to modify.", attrs)

    changes_made = []

    # 1. EC2
    if "ec2" in target_services:
        new_type = get_slot(slots, "new_instance_type")
        if not new_type:
            return elicit_slot(intent, slots, "new_instance_type", "New instance type? (e.g., t3.micro)", attrs)
        if new_type not in VALID_EC2_TYPES:
            return elicit_slot(intent, slots, "new_instance_type", f"Invalid. Options: {VALID_EC2_TYPES}", attrs)

        for c in bp["components"]:
            if c["service"] == "ec2":
                c["compute"]["instance_type"] = new_type
                changes_made.append(f"EC2 ➔ {new_type}")

    # 2. RDS
    if "rds" in target_services:
        new_db = get_slot(slots, "new_db_class")
        if not new_db:
            return elicit_slot(intent, slots, "new_db_class", "Database size? (e.g., db.t3.small)", attrs)
        if new_db not in VALID_RDS_TYPES:
             return elicit_slot(intent, slots, "new_db_class", f"Invalid. Options: {VALID_RDS_TYPES}", attrs)

        for c in bp["components"]:
            if c["service"] == "rds":
                c["database"]["instance_type"] = new_db
                changes_made.append(f"RDS ➔ {new_db}")

    # 3. S3
    if "s3" in target_services:
        versioning = get_slot(slots, "enable_versioning")
        if not versioning:
            return elicit_slot(intent, slots, "enable_versioning", "Enable versioning? (true/false)", attrs)
        
        v_str = str(versioning).lower()
        for c in bp["components"]:
            if c["service"] == "s3":
                c["storage"]["versioning"] = v_str
                changes_made.append(f"S3 Versioning ➔ {v_str}")

    # 4. EKS
    if "eks" in target_services:
        min_nodes = get_slot(slots, "min_nodes")
        if not min_nodes:
            return elicit_slot(intent, slots, "min_nodes", "Min nodes?", attrs)
        
        max_nodes = get_slot(slots, "max_nodes")
        if not max_nodes:
            return elicit_slot(intent, slots, "max_nodes", "Max nodes?", attrs)

        for c in bp["components"]:
            if c["service"] == "eks":
                c["container"]["min_nodes"] = min_nodes
                c["container"]["max_nodes"] = max_nodes
                changes_made.append(f"EKS Nodes ➔ {min_nodes}-{max_nodes}")

    if not changes_made:
         return close(intent, slots, "❌ No changes made. Service not running?", attrs)

    if confirm != "Confirmed":
        return confirm_intent(intent, slots, f"Apply changes: {', '.join(changes_made)}?", attrs)

    cost = recalc_cost(bp)
    attrs.update({
        "infra_blueprint": json.dumps(bp),
        "estimated_cost": str(cost),
        "last_action": "MODIFY"
    })

    # Inside handle_modify (before the return)
    ui_data = {
        "message": f"✅ Updates Applied:\n{chr(10).join(changes_made)}\nNew Cost: ${cost}",
        "ui": {
            "topic": "Configuration Updated",
            "resetLifecycle": True # Hint for frontend
        }
    }
    attrs["ui_payload"] = json.dumps(ui_data)
    # Clear old plan/apply IDs so frontend stops polling them
    attrs.pop("plan_job_id", None)
    attrs.pop("apply_job_id", None)

    return close(intent, slots, f"✅ Updates Applied:\n{chr(10).join(changes_made)}\nNew Cost: ${cost}", attrs)


# ================= TERMINATE HANDLER (SMART) =================

def handle_terminate(intent, slots, attrs, confirm, text):

    attrs["approval_status"] = None
    attrs.pop("apply_job_id", None)

    # No infra exists
    if "infra_blueprint" not in attrs or "active_services" not in attrs:
        return close(intent, slots, "❌ No infrastructure found.", attrs)

    bp = json.loads(attrs["infra_blueprint"])
    active_services = json.loads(attrs["active_services"])

    if not active_services:
        return close(intent, slots, "❌ No running services to terminate.", attrs)

    # Try to detect specific service from user input
    target_services = parse_services(text)

    # -------------------------------
    # CASE 1: User specifies service
    # -------------------------------
    if target_services:
        valid_targets = [s for s in target_services if s in active_services]

        if not valid_targets:
            return close(
                intent,
                slots,
                "❌ Invalid input. Service not found or not running.",
                attrs
            )

        if confirm != "Confirmed":
            return confirm_intent(
                intent,
                slots,
                f"⚠️ Terminate {', '.join(valid_targets).upper()}?",
                attrs
            )

        # Remove selected services
        bp["components"] = [
            c for c in bp["components"]
            if c.get("service") not in valid_targets
        ]

        active_services = [s for s in active_services if s not in valid_targets]

    # ------------------------------------
    # CASE 2: No service specified → LAST
    # ------------------------------------
    else:
        last_service = active_services[-1]

        if confirm != "Confirmed":
            return confirm_intent(
                intent,
                slots,
                f"⚠️ Terminate last created service: {last_service.upper()}?",
                attrs
            )

        bp["components"] = [
            c for c in bp["components"]
            if c.get("service") != last_service
        ]

        active_services.remove(last_service)

    # Recalculate cost
    cost = recalc_cost(bp)

    # Update attributes
    attrs.update({
        "infra_blueprint": json.dumps(bp),
        "active_services": json.dumps(active_services),
        "estimated_cost": str(cost),
        "last_action": "TERMINATE"
    })

    # Inside handle_modify (before the return)
    ui_data = {
        "message": f"✅ Updates Applied:\n{chr(10).join(changes_made)}\nNew Cost: ${cost}",
        "ui": {
            "topic": "Configuration Updated",
            "resetLifecycle": True # Hint for frontend
        }
    }
    attrs["ui_payload"] = json.dumps(ui_data)
    # Clear old plan/apply IDs so frontend stops polling them
    attrs.pop("plan_job_id", None)
    attrs.pop("apply_job_id", None)
    return close(
        intent,
        slots,
        f"✅ Terminated successfully.\n"
        f"Remaining Services: {', '.join(active_services).upper() or 'None'}\n"
        f"New Cost: ${cost}",
        attrs
    )

# ================= APPROVAL MANAGER =========
def handle_approve_apply(intent, slots, attrs):

    try:
        status = attrs.get("approval_status")
        job_id = attrs.get("plan_job_id")
        bp_raw = attrs.get("infra_blueprint")

        if not job_id:
            return close(intent, slots,
                         "❌ No plan found. Create infrastructure first.", attrs)

        if status in [APPROVAL_APPROVED, "DEPLOYING"]:
            return close(intent, slots,
                         "⚠️ Already approved. Deployment already be running.", attrs)

        if status == APPROVAL_REJECTED:
            return close(intent, slots,
                         "❌ Execution was rejected. Create a new plan.", attrs)

        if status != APPROVAL_PENDING:
            return close(intent, slots,
                         "❌ No pending approval found.", attrs)

        bp = json.loads(bp_raw)
        apply_job_id, error = call_backend_apply(job_id, bp)

        if error:
            return close(intent, slots, f"❌ Apply failed: {error}", attrs)

        # Clear temp creation slots but KEEP the blueprint for memory
        if "temp_services" in attrs: del attrs["temp_services"] 

        # Create session state for the deployment phase
        deployment_attrs = {
            "infra_blueprint": bp_raw,
            "active_services": attrs.get("active_services"),
            "approval_status": "DEPLOYING",
            "apply_job_id": apply_job_id,
            "last_action": "APPLY_STARTED"
        }

        ui_msg = f"✅ Approval received. Deployment started!\nApply Job ID: {apply_job_id}"
        
        # Build UI payload for frontend specialized polling
        ui_data = {
            "message": ui_msg,
            "apply_job_id": apply_job_id,
            "ui": {"conversationName": "Deploying...", "disableInput": False}
        }
        deployment_attrs["ui_payload"] = json.dumps(ui_data)

        return close(intent, slots, ui_msg, deployment_attrs)

    except Exception as e:
        logger.error(f"APPROVE CRASH: {str(e)}")
        return close(intent, slots, "❌ Internal error during approval.", attrs)
        

def handle_cancel_apply(intent, slots, attrs):

    status = attrs.get("approval_status")

    if not status:
        return close(intent, slots,
                     "❌ No approval workflow found.", attrs)

    if status == APPROVAL_APPROVED:
        return close(intent, slots,
                     "⚠️ Already approved. Apply may be running.", attrs)

    attrs["approval_status"] = APPROVAL_REJECTED

    return close(intent, slots,
                 "❌ Execution cancelled. Apply will not run.", attrs)

# ================= MAIN =================

def lex_webhook(event):
    intent = event["sessionState"]["intent"]
    name = intent["name"]
    slots = intent.get("slots") or {}
    confirm = intent.get("confirmationState")
    attrs = event["sessionState"].get("sessionAttributes") or {}
    text = (event.get("inputTranscript") or "").lower()

    # --- LIFECYCLE CLEANUP ---
    # If the user shifts to a new major action, we clear the 'approval' state 
    # to prevent the backend or frontend from thinking we are still in a 'Create' loop.
    if name in ["ModifyInfraIntent", "TerminateInfraIntent", "CreateInfraIntent"]:
        if attrs.get("approval_status") == APPROVAL_REJECTED:
             attrs.pop("approval_status", None)

    # --- ROUTING ---
    if name == "ApproveApplyIntent":
        return handle_approve_apply(name, slots, attrs)

    if name == "CancelApplyIntent":
        return handle_cancel_apply(name, slots, attrs)

    if name == "CreateInfraIntent": 
        return handle_create(name, slots, attrs, text)
    
    if name == "ModifyInfraIntent": 
        return handle_modify(name, slots, attrs, confirm, text)
    
    if name == "TerminateInfraIntent": 
        return handle_terminate(name, slots, attrs, confirm, text)

    return close(name, slots, "❓ I'm not sure. Try 'create ec2' or 'modify ec2 type'.", attrs)