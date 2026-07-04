# Deployment Guide — Phase 0 & 1 (D1 setup + Session Bootstrap)

Yeh sirf **pehla piece** hai poore plan ka. Baaki phases (Cloudflare Worker,
GitHub Actions workflows, GLM pitch writer, Telegram approval) agle steps me aayenge.

---

## Phase 0 — Cloudflare D1 Database banao

Prerequisite: [Node.js](https://nodejs.org) + Cloudflare account.

```bash
npm install -g wrangler
wrangler login

# D1 database create karo
wrangler d1 create qwoted-agent-db
```

Output mein ek `database_id` milega — isko note kar lo, baad me chahiye hoga.

Schema apply karo:

```bash
wrangler d1 execute qwoted-agent-db --file=./schema.sql --remote
```

Verify:

```bash
wrangler d1 execute qwoted-agent-db --command="SELECT name FROM sqlite_master WHERE type='table';" --remote
```
5 tables dikhni chahiye: `session`, `profile`, `opportunities`, `pitches`, `agent_events`.

---

## Phase 0.1 — Cloudflare API Token banao

Dashboard → **My Profile → API Tokens → Create Token** → "Edit Cloudflare Workers"
template se start karo, aur permissions mein **D1: Edit** add karo (account level).

Teen values note kar lo:
- `CF_ACCOUNT_ID` (dashboard right sidebar se milega)
- `CF_D1_DATABASE_ID` (upar wale `wrangler d1 create` output se)
- `CF_API_TOKEN` (abhi banaya)

---

## Phase 1 — Local Session Bootstrap (yeh step MANUAL rahega — 30 din mein repeat)

Apne local machine pe (naa ki GitHub Actions pe):

```bash
# 1. Repo clone karo (agar nahi kiya)
git clone https://github.com/Bomx/qwoted-seo-backlinks-skill.git
cd qwoted-seo-backlinks-skill
pip install -r requirements.txt
playwright install chromium

# 2. Login (real browser khulega, MFA/captcha khud solve karo)
python3 qwoted_login.py

# 3. d1_client.py is repo ke andar copy karo (ya PYTHONPATH me add karo)
cp /path/to/d1_client.py .

# 4. Env vars set karo (isi terminal session ke liye)
export CF_ACCOUNT_ID="xxxx"
export CF_D1_DATABASE_ID="xxxx"
export CF_API_TOKEN="xxxx"

# 5. Session ko D1 me upload karo
python3 d1_client.py bootstrap-session
```

Agar successful hua, terminal pe dikhega:
```
[...] d1_client: session uploaded to D1 (N cookies)
```

Verify D1 se:
```bash
wrangler d1 execute qwoted-agent-db --command="SELECT updated_at FROM session WHERE id=1;" --remote
```

**Agar Source profile abhi tak nahi bana hai**, wahi terminal me:
```bash
python3 qwoted_profile.py --action create --full-name "..." --bio "..." --url "..." --email "..."
python3 d1_client.py push   # profile_state.json ko D1 me sync karega
```

---

## Quick Local Test (GitHub Actions banane se pehle verify karo)

Simulate karo ki GitHub Actions kya karega — same machine pe:

```bash
# Local ~/.qwoted ko temporarily clear karo (simulate "fresh runner")
mv ~/.qwoted ~/.qwoted.bak

# Pull (D1 se session/profile wapas laayega)
python3 d1_client.py pull

# Ab normal script chalao
python3 qwoted_search.py --query "SaaS pricing" --max-hits 5

# Push (naye opportunities D1 me save honge)
python3 d1_client.py push

# Verify
wrangler d1 execute qwoted-agent-db --command="SELECT count(*) FROM opportunities;" --remote

# Apna backup restore karo
rm -rf ~/.qwoted && mv ~/.qwoted.bak ~/.qwoted
```

Agar `opportunities` table me rows dikh rahi hain — **pipeline kaam kar raha hai.** ✅

---

## ⚠️ Important Notes

- `bootstrap-session` **sirf local pe** chalao, kabhi GitHub Actions me nahi
  (kyunki login khud MFA maangta hai — Actions me automate nahi ho sakta).
- Session ~30 din me expire hoga. Jab `d1_client.py pull` fail ho (D1 me row
  nahi milegi ya Qwoted "login page" return kare), phir se Phase 1 repeat karo.
- Yeh abhi tak sirf **D1 sync layer** hai. Cloudflare Worker (Telegram +
  repository_dispatch) aur GitHub Actions workflows agla step honge.

---

## Next Steps (agle phases)
1. ✅ ~~D1 schema + d1_client.py~~ ← **abhi yahi bana hai**
2. Cloudflare Worker (`index.js`) — Telegram webhook + repository_dispatch trigger
3. GitHub Actions workflows (`search.yml`, `draft-pitch.yml`, `send-pitch.yml`)
4. GLM pitch-writer (`glm_pitch_writer.py`)
5. Telegram approval flow wiring
