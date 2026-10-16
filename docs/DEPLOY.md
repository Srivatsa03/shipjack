# Deploying the public demo (Vercel + Render + Neon)

```
browser ──> Vercel: the web app (web/)
              │  HTTPS
              v
           Render: shipjack-assistant ──MCP + ACP REST──> Render: shipjack-store
              │      (agents, approvals)  <──signed webhooks──   (checkout, knowledge base)
              v                                                      │
           Neon: Postgres + pgvector  <──────────────────────────────┘
```

What the public demo does differently from a local run (`SHIPJACK_DEMO=1`):

- Every visitor gets a private sandbox: their own shopper account and a store console that shows only
  their chats and orders. There are no shared, well-known tokens.
- Caps: 40 model calls per visitor per day, 600 for the whole demo per day, and 6 new sandboxes per
  network per hour. When a cap is hit, the app says so.
- Groq only (`openai/gpt-oss-120b`, falling back to `openai/gpt-oss-20b`), so the demo costs nothing.
  Groq's free tier has its own daily limits; anything else using the same Groq key shares them.
- Catalog imports are off, so nobody can change the shared store. The store starts with the sample
  Shopify export.
- The store's own API takes tokens Render generates, not the local `demo-token` / `demo-operator`.

Cost: $0 on the free tiers. Render's free services sleep after 15 minutes without traffic; the first
visit after that waits about a minute while they wake (the app says so).

## 1. Push the code

The blueprint and the web app deploy from GitHub, so the commits must be on `github.com/Srivatsa03/shipjack`.

## 2. Neon (the database)

1. Sign up at neon.tech (sign in with GitHub is fine).
2. Create a project: name `shipjack`, Postgres 17, region **AWS US East (Ohio)** (closest to Render's
   Ohio region).
3. On the project dashboard, open **Connect**, turn **Connection pooling off**, and copy the connection
   string. It looks like `postgresql://neondb_owner:...@ep-...us-east-2.aws.neon.tech/neondb?sslmode=require`
   with no `-pooler` in the host. (The pooled one breaks the store's database locks.)

Keep the string to yourself: paste it into Render in the next step, not into a chat or a file.

## 3. Render (the store and the assistant)

1. Sign up at render.com with GitHub and allow it to read the `shipjack` repo.
2. **New > Blueprint**, pick `Srivatsa03/shipjack`. Render reads `render.yaml` and lists two services,
   `shipjack-store` and `shipjack-assistant`, and the group `shipjack-shared`.
3. Fill in what it asks for:
   - `SHIPJACK_DB`: the Neon string from step 2.
   - `GROQ_API_KEY`: copy it from the Keychain without showing it:
     `security find-generic-password -a "$USER" -s groq-api-key -w | pbcopy`, then paste.
   - `SHIPJACK_WEBHOOK_URL`, `SHIPJACK_STORE_URL`, `SHIPJACK_WEB_ORIGINS`: type `https://pending` for
     now; the real addresses exist only after the first deploy.
4. **Apply**. The first build takes about 10 minutes.
5. When both services are live, each has an address like `https://shipjack-store-xxxx.onrender.com`.
   Set, in each service's **Environment** tab:
   - store: `SHIPJACK_WEBHOOK_URL` = `https://<assistant address>/api/webhooks/orders`
   - assistant: `SHIPJACK_STORE_URL` = `https://<store address>`
6. Check: `https://<assistant address>/api/health` answers `{"ok":true,"demo":true,...}`.

## 4. Vercel (the web app)

1. **Add New > Project**, import `Srivatsa03/shipjack`.
2. **Root Directory**: `web`. Framework: Next.js (detected).
3. Environment variable `NEXT_PUBLIC_API_URL` = `https://<assistant address>`.
4. **Deploy**, then copy the address (e.g. `https://shipjack.vercel.app`).
5. Back in Render, set the assistant's `SHIPJACK_WEB_ORIGINS` to that address (comma-separate several,
   e.g. a custom domain too). Without it, the browser blocks the app's calls.

## 5. Check it

Open the Vercel address. The first visit may show "Waking the demo server" for up to a minute. Then:
ask for a hub, approve the card, open **Store console** and ship the order, and watch the order card
update from the store's webhook.

## Running the same stack on one machine

```bash
export GROQ_API_KEY="$(security find-generic-password -a "$USER" -s groq-api-key -w)"
SHIPJACK_DEMO=1 SHIPJACK_MODEL_PROVIDER=groq SHIPJACK_MODEL=openai/gpt-oss-120b \
  docker compose --profile app up --build
```

Then open http://localhost:3000. Without `SHIPJACK_DEMO=1` it is the single-user local app with the
fixed demo tokens.
