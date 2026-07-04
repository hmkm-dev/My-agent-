# Deployment Guide — Phase 3 (GitHub Actions Workflows)

Yeh phase 3 files original repo (`qwoted-seo-backlinks-skill`) ke **andar** jaayengi —
fork/clone karke inhe add karo, koi existing file edit nahi karni.

---

## Step 1 — Files repo me copy karo

Apne fork/clone ke root me:

```
qwoted-seo-backlinks-skill/
├── qwoted_common.py        ← original, untouched
├── qwoted_login.py         ← original, untouched
├── qwoted_search.py        ← original, untouched
├── qwoted_pitch.py         ← original, untouched
├── qwoted_profile.py       ← original, untouched
├── requirements.txt
├── d1_client.py            ← Phase 1 se
├── glm_pitch_writer.py     ← NAYA (Phase 3)
├── send_approved_pitch.py  ← NAYA (Phase 3)
└── .github/
    └── workflows/
        ├── search.yml         ← NAYA
        ├── draft-pitch.yml    ← NAYA
        └── send-pitch.yml     ← NAYA
```

```bash
mkdir -p .github/workflows
cp /path/to/search.yml .github/workflows/
cp /path/to/draft-pitch.yml .github/workflows/
cp /path/to/send-pitch.yml .github/workflows/
cp /path/to/glm_pitch_writer.py .
cp /path/to/send_approved_pitch.py .
git add . && git commit -m "Add production automation (D1 sync + GitHub Actions + GLM)" && git push
```

---

## Step 2 — Free GLM API Key lo (NVIDIA NIM)

`glm_pitch_writer.py` **by default NVIDIA NIM** (free, no card required) use karne ke liye configured hai:

1. [build.nvidia.com](https://build.nvidia.com) pe free account banao (phone verification lagta hai)
2. Account → API Keys → naya key generate karo (format: `nvapi-xxxxxxxxxxxxxxxx`)
3. Yehi key `GLM_API_KEY` secret mein daalni hai (Step 3 dekho)

**Limits:** ~40 requests/minute, no daily token cap — hamare batch size (5 pitches/run) ke liye zyada hai.

> Agar chaho toh Zhipu ka **paid official** GLM API bhi use kar sakte ho — bas 2 extra secrets set karo: `GLM_API_URL = https://open.bigmodel.cn/api/paas/v4/chat/completions` aur `GLM_MODEL = glm-4-plus`. Default (bina inko set kiye) NIM hi use hoga.

---

## Step 3 — GitHub Secrets set karo

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret name | Value |
|---|---|
| `CF_ACCOUNT_ID` | Phase 1 wala |
| `CF_D1_DATABASE_ID` | Phase 1 wala |
| `CF_API_TOKEN` | Phase 1 wala |
| `GLM_API_KEY` | Step 2 se (`nvapi-...` NVIDIA NIM key — free) |
| `GLM_MODEL` | *(optional)* — sirf tab set karo agar `z-ai/glm-5.1` (default, NIM) ki jagah koi aur model use karna hai |
| `WORKER_URL` | Phase 2 me jo Worker URL mila (`https://....workers.dev`) |
| `NOTIFY_SECRET` | Naya random string — **Cloudflare Worker me bhi same value set karni hai** (niche Step 4) |

Generate karne ke liye: `openssl rand -hex 32`

---

## Step 4 — Worker me NOTIFY_SECRET add karo

```bash
wrangler secret put NOTIFY_SECRET
# yahi exact wahi value paste karo jo Step 3 me GitHub secret me daali
```

> ⚠️ Yeh Phase 2 ke `index.js` me pehle se handled hai (`env.NOTIFY_SECRET`
> check `/api/notify` endpoint pe) — bas secret set karna baaki tha.

---

## Step 5 — Session bootstrap already ho chuka hai (Phase 1)?

Agar Phase 1 ka `python3 d1_client.py bootstrap-session` nahi kiya abhi tak —
pehle woh karo, warna `search.yml` fail hoga "No Qwoted session in D1" error ke saath.

---

## Step 6 — Test karo (manual trigger se, cron ka wait mat karo)

GitHub repo → **Actions tab** → "Qwoted Search" workflow → **Run workflow** button
→ query daalo (e.g. "fintech content marketing") → Run.

Ya Telegram se:
```
/search fintech content marketing
```

**Expect karo:**
1. Telegram pe "🔍 Search triggered..." message
2. ~1 min baad "🔍 Search complete: ... N opportunities found" message
3. `/status` command se D1 me naye opportunities dikhne chahiye

---

## Step 7 — Draft workflow test karo

Actions tab → "Qwoted Draft Pitches" → Run workflow (limit: 2, testing ke liye kam rakho).

**Expect karo:** Telegram pe pitch draft message with **✅ Approve / ❌ Reject** buttons.

---

## Step 8 — End-to-end: Approve karke dekho

Telegram pe **✅ Approve** button dabao (ya `/approve <id>`).

**Expect karo:**
1. "✅ Pitch #N approved. Sending now..."
2. Actions tab me "Qwoted Send Pitch" workflow trigger hoga automatically
3. ~30 sec baad "✅ Pitch #N sent successfully to the journalist on Qwoted."
4. Qwoted dashboard pe khud jaake verify karo ki pitch waqai submit hui

---

## 🔒 Safety Checklist (deploy se pehle zaroor confirm karo)

- [ ] `send-pitch.yml` **sirf** `repository_dispatch` (`qwoted_send_pitch`) pe trigger hota hai — koi cron/schedule nahi. ✅ (already is)
- [ ] `send_approved_pitch.py` DB me `status='approved'` check karta hai, warna refuse karta hai. ✅ (already is)
- [ ] Sirf tumhara `TELEGRAM_CHAT_ID` approve/reject kar sakta hai (Worker level check). ✅
- [ ] Test run **kam limit** (1-2) se karo pehle, pura batch (5+) baad me try karo
- [ ] `glm_pitch_writer.py` ke spammy-filters (`_spammy_filters_pass`) apni requirement ke hisaab se tighten karo — abhi basic hai (details length, want_pitches flag)

---

## Cron Schedule Summary (already set, edit as needed in the .yml files)

| Workflow | Trigger |
|---|---|
| `search.yml` | Daily 06:00 UTC + manual + Telegram `/search` |
| `draft-pitch.yml` | Daily 06:30 UTC (30 min after search) + manual |
| `send-pitch.yml` | **Only** on Telegram approval (never on schedule) |

---

## ✅ System Complete — Full Recap

```
Telegram /search
      ↓
Cloudflare Worker → repository_dispatch → GitHub Actions (search.yml)
      ↓                                          ↓
   D1 pull                                  qwoted_search.py
      ↓                                          ↓
                                            D1 push (opportunities)
                                                   ↓
                                    (scheduled) draft-pitch.yml
                                                   ↓
                                    GLM drafts pitch → D1 (pending_approval)
                                                   ↓
                                    Worker /api/notify → Telegram (Approve/Reject)
                                                   ↓
                              [YOU tap Approve] → Worker → repository_dispatch
                                                   ↓
                                          send-pitch.yml → qwoted_pitch.py --send
                                                   ↓
                                          D1 update (sent) → Telegram confirmation
```

Sab 3 phases mil ke poora "real" production system bana dete hain jo tumne
originally maanga tha — Cloudflare Worker + GitHub Actions combination, D1
as the persistent brain, GLM likhta hai pitches, aur tumhara Telegram approval
har real send se pehle mandatory hai.
