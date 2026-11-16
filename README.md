# PS5 Stream Manager – Stage 1

## Google setup
1. console.cloud.google.com → new project → enable **YouTube Data API v3**.
2. OAuth consent screen: External, add yourself as test user, then **Publish app** (avoids 7-day token expiry; ignore the "unverified" warning).
3. Credentials → OAuth client ID → **Web application**. Redirect URI: `https://YOUR-APP.vercel.app/oauth2callback`.

## Vercel env vars
GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, BASE_URL (https://YOUR-APP.vercel.app), OWNER_EMAIL (your Google email),
SECRET_KEY (any long random string), FERNET_KEY (run: `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`).

## Deploy
Push this folder to GitHub, import into Vercel (or `vercel --prod`).

## Test
Go live from PS5 → open `/` → sign in → run buttons 1–6 in order. Send me the raw output of any step that fails.

Note: Stage 1 keeps the encrypted refresh token in your session cookie. Stage 2 moves it to Neon Postgres.
