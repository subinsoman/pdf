# PDF Chatbot (Streamlit + Google OAuth)

## Prerequisites
- Python 3.10+
- Git
- Google Cloud OAuth2 credentials (Client ID, Client Secret)

## 1) Create and activate a virtual environment

Linux/macOS:
```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows (PowerShell):
```powershell
python -m venv .venv
source .venv/bin/activate
```

## 2) Install dependencies
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

## 3) Configure Streamlit and OAuth
Create the folder and secrets file:
```bash
mkdir -p .streamlit
nano .streamlit/secrets.toml
```
Sample `.streamlit/secrets.toml`:
```toml
[google]
client_id = "YOUR_GOOGLE_CLIENT_ID.apps.googleusercontent.com"
client_secret = "YOUR_GOOGLE_CLIENT_SECRET"
# Must exactly match an Authorized redirect URI in Google Cloud Console
redirect_uri = "http://127.0.0.1:8000/auth"

# Optional: cookie encryption password for persistent login
COOKIE_PASSWORD = "change-this-strong-secret"

# Optional admin password (if used elsewhere)
ADMIN_PASSWORD = "admin"
```

Optionally configure Streamlit server (port, address) in `.streamlit/config.toml`:
```toml
[server]
port = 8000
address = "127.0.0.1"
headless = true
```

In Google Cloud Console (Credentials):
- Authorized JavaScript origins: `http://127.0.0.1:8000`
- Authorized redirect URIs: `http://127.0.0.1:8000/auth`

## 4) Run the app
```bash
streamlit run app.py
```
Open: http://127.0.0.1:8000

## 5) Project scripts
- Login with Google on startup (centered UI).
- Top-right user chip with dropdown (name, email, logout).
- Logout revokes token and clears cookie + session.
- Sidebar navigation after login.

## Troubleshooting
- If you see `redirect_uri_mismatch`, ensure the redirect URI matches in both secrets and Google Console.
- If you keep returning to login on refresh, ensure you're opening 127.0.0.1 (not 0.0.0.0) and that cookies are enabled.
