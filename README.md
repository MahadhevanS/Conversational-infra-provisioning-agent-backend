# CloudCrafter — Backend

A **FastAPI** backend that orchestrates conversational AWS infrastructure provisioning using Terraform, Infracost, and Google Gemini AI — all driven by natural language via AWS Lex.

---

## Architecture Overview

```
Frontend (React)
     │
     ▼
FastAPI (backend/main.py)
     ├── /lex-webhook  → AWS Lex NLU → NLP parser (backend/lex.py)
     ├── /plan         → Terraform plan (background thread)
     ├── /apply        → Terraform apply (background thread)
     ├── /cost         → Infracost estimate (background thread)
     ├── /destroy      → Terraform destroy + RBAC approval flow
     └── /status/:id   → Job polling endpoint
          │
          ▼
     Supabase (PostgreSQL + Realtime)
          │
          ├── jobs          (status, log_chunks, ai_analysis)
          ├── projects       (ownership)
          ├── aws_credentials (IAM role ARNs)
          ├── user_profiles  (roles: admin / cloud_architect)
          ├── chat_messages
          ├── project_members
          ├── project_invitations
          └── destroy_approvals
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| API Framework | FastAPI + Uvicorn |
| IaC Engine | Terraform 1.6.6 |
| Cost Estimation | Infracost |
| AI Analysis | Google Gemini 2.5 Flash Lite |
| Database / Auth | Supabase (PostgreSQL + Auth) |
| AWS Integration | boto3 (STS AssumeRole) |
| NLU | AWS Lex V2 |
| Containerisation | Docker |

---

## Getting Started

### Prerequisites

- Python 3.11+
- Docker (recommended for full environment)
- Terraform 1.6.6+
- Infracost CLI
- A Supabase project
- AWS account with an IAM role configured for cross-account assume

### 1. Clone the repository

```bash
git clone <repo-url>
cd Conversational-infra-provisioning-agent-backend
```

### 2. Configure environment variables

Create a `.env` file in the root directory:

```env
# Supabase
SUPABASE_URL=https://<your-project>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<your-service-role-key>

# AWS (backend's own credentials — used for STS AssumeRole)
AWS_ACCESS_KEY_ID=<your-access-key>
AWS_SECRET_ACCESS_KEY=<your-secret-key>
AWS_DEFAULT_REGION=us-east-1

# Infracost
INFRACOST_API_KEY=<your-infracost-key>

# Google Gemini (for AI failure analysis)
GEMINI_API_KEY=<your-gemini-key>
```

### 3a. Run with Docker (recommended)

```bash
docker build -t cloudcrafter-backend .
docker run -p 8000:8000 --env-file .env cloudcrafter-backend
```

### 3b. Run locally

```bash
pip install -r requirements.txt
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

The API will be available at `http://localhost:8000`.

---

## 📡 API Endpoints

### Auth

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/signup` | Register a new user with IAM role validation |
| `POST` | `/login` | Authenticate and receive a JWT |

### Projects

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/projects` | List all projects for the authenticated user |
| `POST` | `/projects/create` | Create a new project and send invitations |
| `POST` | `/projects/{id}/invite` | Invite a Cloud Architect to a project |
| `POST` | `/projects/{id}/members/invite` | Bulk invite members |

### Invitations

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/invitations/{token}/accept` | Accept a project invitation |

### Infrastructure Jobs

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/plan` | Run `terraform plan` (async, returns `job_id`) |
| `POST` | `/apply` | Run `terraform apply` (async, returns `apply_job_id`) |
| `POST` | `/cost` | Run Infracost estimate (async) |
| `POST` | `/destroy` | Destroy infrastructure (RBAC-gated for architects) |
| `GET` | `/status/{job_id}` | Poll job status, result, and AI analysis |

### Destroy Approvals (RBAC)

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/destroy-approvals/{token}` | View approval request details (admin only) |
| `POST` | `/destroy-approvals/{token}/approve` | Approve destroy request |
| `POST` | `/destroy-approvals/{token}/reject` | Reject destroy request |

### Logs & Chats

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/logs/project/{project_id}` | Fetch all Terraform log chunks for a project |
| `POST` | `/lex-webhook` | AWS Lex fulfillment webhook |

---

## RBAC Model

The system enforces two roles:

| Role | Capabilities |
|---|---|
| **admin** | Full control — create/destroy projects, approve destroy requests |
| **cloud_architect** | Can plan, apply, and request destroys (destroy requires admin approval) |

When a Cloud Architect triggers a destroy, the system:
1. Creates a `destroy_approvals` record in Supabase
2. Emails the project admin via the email service
3. Posts a pending status message to the project chat feed
4. Admin approves/rejects via a dedicated token URL

---

## AI Failure Analysis

On any failed Terraform job (`PLAN`, `APPLY`, or `DESTROY`), the backend:

1. Spawns a background daemon thread
2. Fetches the `log_chunks` stored in Supabase
3. Calls **Gemini 2.5 Flash Lite** with the error context
4. Persists the structured result to the `ai_analysis` column on the job row
5. The frontend picks it up via Supabase Realtime or the `/status` polling endpoint

The AI returns:

```json
{
  "root_cause": "One sentence describing the exact cause",
  "fix_steps": ["Step 1", "Step 2", "Step 3"],
  "category": "permissions | resource_conflict | config_error | state_error | unknown"
}
```

---

## Project Structure

```
Conversational-infra-provisioning-agent-backend/
├── backend/
│   ├── main.py           # FastAPI app, all endpoints, job workers
│   ├── lex.py            # AWS Lex webhook + NLP parsing + blueprint builder
│   ├── orchestrator.py   # Terraform CLI orchestration (plan/apply/destroy/cost)
│   ├── ai_analyser.py    # Gemini AI failure analysis
│   ├── db.py             # Supabase client initialisation
│   ├── email_service.py  # Invitation & approval emails
│   ├── tfvars_generator.py # Generates .tfvars from blueprint JSON
│   └── validator.py      # Input validation helpers
├── terraform/            # Terraform module templates
├── persistent_jobs/      # Runtime working directories for Terraform state
├── requirements.txt
├── Dockerfile
└── .env
```

---

## Environment Setup Notes

- **Terraform** is installed at build time in the Docker image (`v1.6.6`).
- **Infracost** is installed via its official shell installer in the Dockerfile.
- AWS credentials are **never stored** — the backend uses STS `AssumeRole` with the user-supplied `role_arn` and optional `external_id` for every Terraform run.
- Supabase Realtime is used on the frontend to receive live `jobs` row updates without polling.

---

## Requirements

```
fastapi
uvicorn[standard]
supabase
python-dotenv
requests
boto3
python-jose
```

---

## Running Tests

```bash
# No automated test suite yet — manual testing via the frontend or:
curl -X GET http://localhost:8000/status/<job_id>
```

---

## License

MIT
