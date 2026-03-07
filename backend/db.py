import os
from supabase import create_client
from dotenv import load_dotenv

# 🔥 override=True forces Python to use your .env file instead of old terminal cache!
load_dotenv(override=True) 

# 🛡️ Scrubbing out spaces AND quotation marks
SUPABASE_URL = os.getenv("SUPABASE_URL", "").replace('"', '').replace("'", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").replace('"', '').replace("'", "").strip()

# 🕵️‍♂️ Debug prints to verify exactly what Python is seeing!
print("=========================================")
print(f"🕵️‍♂️ DEBUG SUPABASE URL: {SUPABASE_URL}")
print(f"🕵️‍♂️ DEBUG SUPABASE KEY LENGTH: {SUPABASE_KEY}")
print("=========================================")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)