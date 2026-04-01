import smtplib
from email.message import EmailMessage

SMTP_SERVER   = "smtp.gmail.com"
SMTP_PORT     = 587
SMTP_USERNAME = "sportspose300@gmail.com"
SMTP_PASSWORD = "yrbl fcab zzmb okba"
FRONTEND_URL  = "http://localhost:3000"


def send_invitation_email(target_email: str, project_name: str, inviter_email: str, invite_token: str):
    try:
        msg = EmailMessage()
        msg['Subject'] = f"Invitation to join project: {project_name}"
        msg['From']    = f"CloudCrafter <{SMTP_USERNAME}>"
        msg['To']      = target_email

        invite_link = f"{FRONTEND_URL}/join/{invite_token}"

        msg.set_content(f"""
Hello!

{inviter_email} has invited you to collaborate on the "{project_name}" infrastructure project in CloudCrafter as a Cloud Architect.

Click the secure link below to accept the invitation and instantly join the workspace:
{invite_link}

If you do not have a CloudCrafter account yet, you will be prompted to create one.

See you in the cloud,
The CloudCrafter Team
""")

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)

        print(f"✅ Successfully sent invite to {target_email}")

    except Exception as e:
        print(f"🚨 FAILED to send email to {target_email}. Error: {e}")


def send_destroy_approval_email(
    admin_email: str,
    admin_name: str,
    project_name: str,
    requested_by_name: str,
    requested_by_email: str,
    scope: str,
    approval_token: str,
):
    """
    Sends the project admin an email asking them to approve or reject
    a destroy request made by a Cloud Architect.
    """
    try:
        approval_link = f"{FRONTEND_URL}/approve-destroy/{approval_token}"
        scope_label   = "the entire environment" if scope == "ALL" else "selected resources"

        msg = EmailMessage()
        msg['Subject'] = f"⚠️ Destroy Approval Required — {project_name}"
        msg['From']    = f"CloudCrafter <{SMTP_USERNAME}>"
        msg['To']      = admin_email

        msg.set_content(f"""
Hello {admin_name or "Admin"},

{requested_by_name} ({requested_by_email}) has requested to DESTROY {scope_label} in the project "{project_name}".

This action is IRREVERSIBLE and will permanently remove all associated AWS infrastructure.

To review and approve or reject this request, click the link below:

{approval_link}

This link will expire in 24 hours. If you did not expect this request, you can safely ignore this email.

— The CloudCrafter Team
""")

        # HTML version with styled buttons
        msg.add_alternative(f"""
<html>
  <body style="font-family: sans-serif; background: #0f0f16; color: #e4e4e7; padding: 32px;">
    <div style="max-width: 520px; margin: 0 auto; background: #151521; border: 1px solid rgba(255,255,255,0.1); border-radius: 16px; padding: 32px;">
      
      <div style="width: 56px; height: 56px; background: rgba(239,68,68,0.15); border-radius: 50%; display: flex; align-items: center; justify-content: center; margin-bottom: 24px;">
        <span style="font-size: 28px;">⚠️</span>
      </div>

      <h2 style="margin: 0 0 8px; color: #f87171; font-size: 20px;">Destroy Request — Approval Required</h2>
      <p style="color: #a1a1aa; margin: 0 0 24px; font-size: 14px;">Project: <strong style="color: #e4e4e7;">{project_name}</strong></p>

      <div style="background: rgba(239,68,68,0.08); border: 1px solid rgba(239,68,68,0.2); border-radius: 10px; padding: 16px; margin-bottom: 24px;">
        <p style="margin: 0; font-size: 14px; line-height: 1.6;">
          <strong>{requested_by_name}</strong> ({requested_by_email})<br/>
          has requested to permanently destroy <strong>{scope_label}</strong>.
        </p>
      </div>

      <p style="color: #a1a1aa; font-size: 13px; margin: 0 0 24px;">
        This action is <strong style="color: #f87171;">irreversible</strong>. Review the request carefully before approving.
      </p>

      <a href="{approval_link}"
         style="display: inline-block; background: #4f46e5; color: white; text-decoration: none;
                padding: 12px 28px; border-radius: 10px; font-weight: 600; font-size: 14px;">
        Review Request →
      </a>

      <p style="color: #52525b; font-size: 12px; margin-top: 32px;">
        This link expires in 24 hours. If you did not expect this, ignore this email.
      </p>
    </div>
  </body>
</html>
""", subtype='html')

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)

        print(f"✅ Destroy approval email sent to {admin_email}")

    except Exception as e:
        print(f"🚨 FAILED to send destroy approval email to {admin_email}. Error: {e}")