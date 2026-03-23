"""
ai_analyser.py

Analyses Terraform failure logs using Google Gemini 2.0 Flash and returns a
structured suggestion dict stored on the job row and in notification metadata.

Usage:
    from ai_analyser import analyse_failure

    suggestion = analyse_failure(job_id="plan-xxx", job_type="PLAN", log_chunks=[...])
    # Returns:
    # {
    #     "root_cause": "...",
    #     "fix_steps": ["...", "..."],
    #     "category": "permissions" | "resource_conflict" | "config_error" | "state_error" | "unknown",
    #     "raw": "..."        # full model response text, useful for debugging
    # }

Environment variable required:
    GEMINI_API_KEY   — get a free key at https://aistudio.google.com/app/apikey

Optional SDK (cleaner errors, slightly faster cold-start):
    pip install google-generativeai
Falls back to plain urllib if the SDK is not installed — no hard dependency.
"""

import os
import json
import re
import logging

logger = logging.getLogger(__name__)

# ── Model & limits ─────────────────────────────────────────────────────────────

GEMINI_MODEL = "gemini-2.5-flash-lite"
# Free tier: 15 RPM, 1 500 req/day, 1M TPM — plenty for failure analysis.
# Paid tier: $0.075 / 1M input tokens (half the price of GPT-4o-mini).

MAX_STDOUT_LINES = 60    # lines of stdout from the most advanced stage
MAX_LOG_CHARS    = 12_000  # hard cap on total characters sent to the model

# ── Prompts ────────────────────────────────────────────────────────────────────

SYSTEM_INSTRUCTION = (
    "You are an expert AWS infrastructure engineer specialising in Terraform. "
    "A deployment job has just failed. You will be given the Terraform log output. "
    "Respond ONLY with a valid JSON object — no markdown fences, no prose outside the JSON."
)

USER_PROMPT_TEMPLATE = """\
Analyse this Terraform failure and return a JSON object with exactly these keys:
{{
  "root_cause": "<one sentence — name the exact resource or error that caused the failure>",
  "fix_steps": ["<step 1>", "<step 2>", "<step 3 if needed — max 3 steps>"],
  "category": "<one of: permissions, resource_conflict, config_error, state_error, unknown>"
}}

Category definitions:
- permissions       → IAM / assume-role / access denied errors
- resource_conflict → resource already exists, name collision, quota exceeded
- config_error      → invalid HCL, wrong variable type, missing required field
- state_error       → state lock, state mismatch, workspace issues
- unknown           → anything else

Terraform logs:
{log_context}
"""

# ── ANSI stripper ──────────────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1B\[[0-9;]*[mGKHF]|\x1B\][^\x07]*\x07")

def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)

# ── Log extractor ──────────────────────────────────────────────────────────────

def _build_log_context(log_chunks: list[dict], job_type: str) -> str:
    """
    Build a concise log string for the model.

    - All stderr from every stage (this is where Terraform puts errors).
    - Last MAX_STDOUT_LINES of stdout from the most advanced stage.
    - Hard cap at MAX_LOG_CHARS.
    """
    if not log_chunks:
        return "No log output was captured for this job."

    # Stderr — all stages in order
    stderr_parts = []
    for chunk in log_chunks:
        if chunk.get("stream") == "stderr" and chunk.get("text", "").strip():
            stage = chunk.get("stage", "unknown")
            text  = _strip_ansi(chunk["text"]).strip()
            stderr_parts.append(f"[{stage}/stderr]\n{text}")

    # Stdout — prefer most advanced stage
    stage_priority = {"apply": 4, "destroy": 4, "plan": 3, "init": 2, "show": 1}
    stdout_chunks = [
        c for c in log_chunks
        if c.get("stream") == "stdout" and c.get("text", "").strip()
    ]
    stdout_chunks.sort(
        key=lambda c: stage_priority.get(c.get("stage", ""), 0),
        reverse=True,
    )

    stdout_parts = []
    if stdout_chunks:
        best  = stdout_chunks[0]
        lines = _strip_ansi(best["text"]).splitlines()
        tail  = lines[-MAX_STDOUT_LINES:]
        stage = best.get("stage", "unknown")
        stdout_parts.append(
            f"[{stage}/stdout — last {len(tail)} lines]\n" + "\n".join(tail)
        )

    sections = []
    if stderr_parts:
        sections.append("\n\n".join(stderr_parts))
    if stdout_parts:
        sections.append("\n\n".join(stdout_parts))

    header = f"=== Terraform {job_type} job failure ===\n\n"
    body   = "\n\n".join(sections) if sections else "No meaningful output captured."
    full   = header + body

    if len(full) > MAX_LOG_CHARS:
        full = full[:MAX_LOG_CHARS] + "\n\n[...truncated]"

    return full

# ── Gemini REST call (urllib — no SDK required) ────────────────────────────────

def _call_gemini_urllib(api_key: str, log_context: str) -> str:
    """
    Call the Gemini generateContent REST endpoint directly.
    response_mime_type=application/json guarantees valid JSON output.
    """
    import urllib.request

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={api_key}"
    )

    payload = {
        "system_instruction": {
            "parts": [{"text": SYSTEM_INSTRUCTION}]
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": USER_PROMPT_TEMPLATE.format(log_context=log_context)}],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 512,
            "response_mime_type": "application/json",
        },
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())

    # Response shape:
    # { "candidates": [ { "content": { "parts": [ { "text": "..." } ] } } ] }
    return data["candidates"][0]["content"]["parts"][0]["text"]

# ── Gemini SDK call (preferred when google-generativeai is installed) ──────────

def _call_gemini_sdk(api_key: str, log_context: str) -> str:
    import google.generativeai as genai

    genai.configure(api_key=api_key)

    model = genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        system_instruction=SYSTEM_INSTRUCTION,
        generation_config=genai.GenerationConfig(
            temperature=0.2,
            max_output_tokens=512,
            response_mime_type="application/json",
        ),
    )

    response = model.generate_content(
        USER_PROMPT_TEMPLATE.format(log_context=log_context)
    )
    return response.text

# ── Main entry point ───────────────────────────────────────────────────────────

def analyse_failure(job_id: str, job_type: str, log_chunks: list[dict]) -> dict:
    """
    Analyse a Terraform failure using Gemini 2.0 Flash.

    Always returns a dict — never raises.  On any error the 'root_cause'
    field explains what went wrong so the UI can still surface something useful.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        logger.warning("GEMINI_API_KEY not set — skipping AI analysis")
        return {
            "root_cause": "AI analysis is not configured (GEMINI_API_KEY missing).",
            "fix_steps": [
                "Add GEMINI_API_KEY to your server environment.",
                "Get a free key at https://aistudio.google.com/app/apikey",
                "Check the Logs panel for the raw Terraform error.",
            ],
            "category": "unknown",
            "raw": "",
        }

    log_context = _build_log_context(log_chunks, job_type)
    raw_text    = ""

    try:
        # Prefer the SDK if installed; fall back to urllib otherwise
        try:
            import google.generativeai  # noqa: F401 — availability check only
            raw_text = _call_gemini_sdk(api_key, log_context)
            logger.info(f"Gemini SDK call succeeded for job {job_id}")
        except ImportError:
            raw_text = _call_gemini_urllib(api_key, log_context)
            logger.info(f"Gemini urllib call succeeded for job {job_id}")

        parsed = json.loads(raw_text)

        return {
            "root_cause": str(parsed.get("root_cause", "Unknown error")),
            "fix_steps":  [str(s) for s in parsed.get("fix_steps", [])],
            "category":   str(parsed.get("category", "unknown")),
            "raw":        raw_text,
        }

    except json.JSONDecodeError as e:
        logger.error(f"Gemini returned non-JSON for job {job_id}: {e}\nRaw: {raw_text[:200]}")
        return {
            "root_cause": "AI analysis returned an unexpected format.",
            "fix_steps":  ["Check the Logs panel for the raw Terraform error."],
            "category":   "unknown",
            "raw":        raw_text,
        }
    except Exception as e:
        logger.error(f"AI analysis failed for job {job_id}: {e}")
        return {
            "root_cause": f"AI analysis unavailable: {str(e)}",
            "fix_steps":  ["Check the Logs panel for the raw Terraform error."],
            "category":   "unknown",
            "raw":        "",
        }