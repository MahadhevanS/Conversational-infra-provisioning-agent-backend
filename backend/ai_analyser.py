"""
ai_analyser.py

Analyses Terraform failure logs using OpenAI gpt-4o-mini and returns a
structured suggestion dict that can be stored in a notification's metadata
and posted as a bot chat message.

Usage:
    from ai_analyser import analyse_failure

    suggestion = analyse_failure(job_id="plan-xxx", job_type="PLAN", log_chunks=[...])
    # Returns:
    # {
    #     "root_cause": "...",
    #     "fix_steps": ["...", "..."],
    #     "category": "permissions" | "resource_conflict" | "config_error" | "unknown",
    #     "raw": "..."        # full GPT response text, useful for debugging
    # }
"""

import os
import json
import re
import logging

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

OPENAI_MODEL = "gpt-4o-mini"

# How many lines of stdout to include from the failing stage (stderr is always
# included in full — it's where Terraform puts the actual error message).
MAX_STDOUT_LINES = 60

# Hard cap on total characters sent to OpenAI — keeps costs predictable and
# avoids accidental prompt injection from extremely long logs.
MAX_LOG_CHARS = 12_000

SYSTEM_PROMPT = """You are an expert AWS infrastructure engineer specialising in Terraform.
A deployment job has just failed. You will be given the Terraform log output.

Respond ONLY with a valid JSON object — no markdown fences, no prose outside the JSON.
The JSON must have exactly these keys:
{
  "root_cause": "<one sentence describing the root cause>",
  "fix_steps": ["<step 1>", "<step 2>", "<step 3 if needed>"],
  "category": "<one of: permissions, resource_conflict, config_error, state_error, unknown>"
}

Guidelines:
- root_cause: be specific — name the exact resource or error message that caused the failure.
- fix_steps: actionable steps the user can take right now, max 3. Reference AWS resource names or Terraform blocks where possible.
- category:
    permissions  → IAM / assume-role / access denied errors
    resource_conflict → resource already exists, name collision, quota exceeded
    config_error → invalid HCL, wrong variable type, missing required field
    state_error  → state lock, state mismatch, workspace issues
    unknown      → anything else
"""

# ── ANSI stripper ──────────────────────────────────────────────────────────────

_ANSI_RE = re.compile(r"\x1B\[[0-9;]*[mGKHF]|\x1B\][^\x07]*\x07")

def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# ── Log extractor ──────────────────────────────────────────────────────────────

def _build_log_context(log_chunks: list[dict], job_type: str) -> str:
    """
    Extract the most relevant lines from log_chunks for the failing job.

    Strategy:
    - Include ALL stderr from every stage (this is where errors live).
    - Include the last MAX_STDOUT_LINES lines of stdout from the most
      advanced stage that has content (apply > plan > init for APPLY jobs).
    - Prepend a one-line header so the model knows what it's reading.
    - Truncate the whole thing to MAX_LOG_CHARS.
    """
    if not log_chunks:
        return "No log output was captured for this job."

    # Collect stderr blobs (all stages, in order)
    stderr_parts = []
    for chunk in log_chunks:
        if chunk.get("stream") == "stderr" and chunk.get("text", "").strip():
            stage = chunk.get("stage", "unknown")
            text = _strip_ansi(chunk["text"]).strip()
            stderr_parts.append(f"[{stage}/stderr]\n{text}")

    # Collect stdout — prefer the most advanced stage
    stage_priority = {"apply": 4, "destroy": 4, "plan": 3, "init": 2, "show": 1}
    stdout_chunks = [c for c in log_chunks if c.get("stream") == "stdout" and c.get("text", "").strip()]
    stdout_chunks.sort(key=lambda c: stage_priority.get(c.get("stage", ""), 0), reverse=True)

    stdout_parts = []
    if stdout_chunks:
        best = stdout_chunks[0]
        lines = _strip_ansi(best["text"]).splitlines()
        tail = lines[-MAX_STDOUT_LINES:]
        stage = best.get("stage", "unknown")
        stdout_parts.append(f"[{stage}/stdout — last {len(tail)} lines]\n" + "\n".join(tail))

    sections = []
    if stderr_parts:
        sections.append("\n\n".join(stderr_parts))
    if stdout_parts:
        sections.append("\n\n".join(stdout_parts))

    header = f"=== Terraform {job_type} job failure ===\n\n"
    body = "\n\n".join(sections) if sections else "No meaningful output captured."

    full = header + body
    if len(full) > MAX_LOG_CHARS:
        full = full[:MAX_LOG_CHARS] + "\n\n[...truncated]"

    return full


# ── Main entry point ───────────────────────────────────────────────────────────

def analyse_failure(job_id: str, job_type: str, log_chunks: list[dict]) -> dict:
    """
    Call OpenAI and return a structured analysis dict.

    Always returns a dict — never raises.  On any error the 'root_cause'
    field describes what went wrong so callers can still surface something
    useful to the user.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.warning("OPENAI_API_KEY not set — skipping AI analysis")
        return {
            "root_cause": "AI analysis is not configured (OPENAI_API_KEY missing).",
            "fix_steps": ["Check the server logs for the raw error message.", "Review the Terraform plan output in the Logs panel."],
            "category": "unknown",
            "raw": "",
        }

    log_context = _build_log_context(log_chunks, job_type)

    try:
        # Use the openai SDK if available, fall back to raw urllib so the
        # feature works even if the package isn't installed yet.
        try:
            import openai
            client = openai.OpenAI(api_key=api_key)
            response = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": log_context},
                ],
                temperature=0.2,       # low temp → consistent, factual output
                max_tokens=512,
                response_format={"type": "json_object"},
            )
            raw_text = response.choices[0].message.content or ""

        except ImportError:
            # Fallback: raw HTTP call — no extra dependency needed
            import urllib.request
            import urllib.error

            body = json.dumps({
                "model": OPENAI_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": log_context},
                ],
                "temperature": 0.2,
                "max_tokens": 512,
                "response_format": {"type": "json_object"},
            }).encode()

            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
            raw_text = data["choices"][0]["message"]["content"] or ""

        # Parse the JSON response
        parsed = json.loads(raw_text)

        return {
            "root_cause": str(parsed.get("root_cause", "Unknown error")),
            "fix_steps": [str(s) for s in parsed.get("fix_steps", [])],
            "category": str(parsed.get("category", "unknown")),
            "raw": raw_text,
        }

    except json.JSONDecodeError as e:
        logger.error(f"OpenAI returned non-JSON for job {job_id}: {e}")
        return {
            "root_cause": "AI analysis returned an unexpected format.",
            "fix_steps": ["Check the Logs panel for the raw Terraform error."],
            "category": "unknown",
            "raw": raw_text if "raw_text" in locals() else "",
        }
    except Exception as e:
        logger.error(f"AI analysis failed for job {job_id}: {e}")
        return {
            "root_cause": f"AI analysis unavailable: {str(e)}",
            "fix_steps": ["Check the Logs panel for the raw Terraform error."],
            "category": "unknown",
            "raw": "",
        }