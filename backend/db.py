import os
from supabase import create_client, ClientOptions
from dotenv import load_dotenv

load_dotenv(override=True)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").replace('"', "").replace("'", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").replace('"', "").replace("'", "").strip()

opts = ClientOptions(
    persist_session=False,
    auto_refresh_token=False
)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY, options=opts)