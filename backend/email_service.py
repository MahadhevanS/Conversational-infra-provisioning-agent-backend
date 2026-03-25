import smtplib
from email.message import EmailMessage

# ==========================================
# EMAIL CONFIGURATION
# ==========================================
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587

# Replace these with your actual details
SMTP_USERNAME = "falldetector91@gmail.com" 
# CRITICAL: This must be an App Password, NOT your normal Gmail password!
SMTP_PASSWORD = "" 

# Your React Frontend URL (Change this when you deploy to production!)
FRONTEND_URL = "http://localhost:3000"

def send_invitation_email(target_email: str, project_name: str, inviter_email: str, invite_token: str):
    """
    Sends a secure click-to-join invitation email to a Cloud Architect.
    """
    try:
        msg = EmailMessage()
        msg['Subject'] = f"Invitation to join project: {project_name}"
        msg['From'] = f"CloudCrafter <{SMTP_USERNAME}>"
        msg['To'] = target_email
        
        # The secure link pointing to your React app
        invite_link = f"{FRONTEND_URL}/join/{invite_token}"
        
        # The Email Body
        msg.set_content(f"""
Hello!

{inviter_email} has invited you to collaborate on the "{project_name}" infrastructure project in CloudCrafter as a Cloud Architect.

Click the secure link below to accept the invitation and instantly join the workspace:
{invite_link}

If you do not have a CloudCrafter account yet, you will be prompted to create one.

See you in the cloud,
The CloudCrafter Team
""")

        # Connect to the server and send the email
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls() # Upgrades the connection to secure encrypted TLS
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
            
        print(f"✅ Successfully sent click-to-join link to {target_email}")
        
    except Exception as e:
        print(f"🚨 FAILED to send email to {target_email}. Error: {e}")