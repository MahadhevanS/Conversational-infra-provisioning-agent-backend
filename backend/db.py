import os
from supabase import create_client, ClientOptions
from dotenv import load_dotenv

load_dotenv(override=True)

SUPABASE_URL = os.getenv("SUPABASE_URL", "").replace('"', '').replace("'", "").strip()
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "").replace('"', '').replace("'", "").strip()
SUPABASE_ANON_KEY = os.getenv("SUPABASE_KEY", "").replace('"', '').replace("'", "").strip()

opts = ClientOptions(
    persist_session=False,
    auto_refresh_token=False
)

# 🔥 Service role client — used for ALL table queries in routes
# Bypasses RLS so the backend queries freely; authorization is
# handled manually in each route with ownership/membership checks
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY, options=opts)


def make_auth_client():
    """
    Returns a fresh one-shot Supabase client using the anon key.
    Use ONLY for auth operations (sign_up, sign_in_with_password).
    Never reuse — create a new one each call to prevent session bleed.
    """
    return create_client(
        SUPABASE_URL,
        SUPABASE_ANON_KEY,
        options=ClientOptions(persist_session=False, auto_refresh_token=False)
    )