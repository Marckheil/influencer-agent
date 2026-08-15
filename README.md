# Agentic Influencer — build notes

An agent that runs on Maritime, generates faceless 9:16 videos (Higgsfield),
emails you each one for approval (Inkbox), and on your "post" reply publishes
it to Instagram as a Reel (Upload-Post — the official, ToS-compliant path).

## The loop (one step per wake)

```
idle ──► generating ──► awaiting_approval ──► posting ──► idle
 │  pick idea      │ poll Higgsfield   │ you reply    │ Upload-Post
 │  + start gen    │ until video ready │ "post"/"skip"│ publishes Reel
 └──────────────── kick a new idea when back to idle ─────────────┘
```

- **AUTO_POST=false** (default): agent emails you the video + caption; you reply
  `post` or `skip`. Compliant + safe + better demo.
- **AUTO_POST=true**: skips approval, posts automatically. Flip once you trust it.

This shape fits Maritime's sleep/wake model: bursty, scheduled work. Video gen
is async, so the agent kicks a job then polls on later wakes.

## What's PROVEN vs what needs your accounts

**Proven & tested locally** (state machine, all paths): idle→generate→poll→
approve→post→idle, plus skip, auto-post, idea-queue, history tracking.

**Needs your credentials + one confirmation each:**
- **Inkbox** — same as your reminder agent; already known-good.
- **Upload-Post** — API confirmed exact: `POST https://api.upload-post.com/api/upload`,
  header `Authorization: Apikey KEY`, `platform[]=instagram`, `media_type=REELS`,
  `video_url=<public mp4>`, `user=<your connected profile>`, `title=<caption>`.
  The `uploadpost_publish()` function already matches this.
- **Higgsfield** — this is the ONE piece to confirm. Higgsfield's main interface
  is an MCP server; the exact REST endpoint/params for a headless agent must be
  read off your Higgsfield account's API docs once you have a key. Only two
  functions depend on it, both clearly marked:
    - `higgsfield_start(prompt) -> job_id`
    - `higgsfield_poll(job_id)  -> None | video_url`
  Adjust the URL/params/response-field names there; everything else is done.

## Environment variables (set with `maritime env set`)

| Var | What | Required |
|---|---|---|
| `INKBOX_API_KEY` | your Inkbox key | yes |
| `AGENT_HANDLE` | Inkbox identity handle (e.g. `reminder`) | yes |
| `OWNER_EMAIL` | where approval emails go (you) | yes |
| `HF_KEY_ID` | Higgsfield key ID (the part before the colon) | yes |
| `HF_KEY_SECRET` | Higgsfield key secret (the part after the colon) | yes |
| `HF_IMAGE_MODEL` | text->image model path (default higgsfield-ai/soul/standard) | no |
| `HF_VIDEO_MODEL` | image->video model path (default higgsfield-ai/dop/standard) | no |
| `HIGGSFIELD_BASE` | override base URL (default platform.higgsfield.ai) | no |
| `UPLOADPOST_API_KEY` | Upload-Post key | yes |
| `UPLOADPOST_USER` | your connected IG profile name in Upload-Post | yes |
| `AUTO_POST` | `true` to skip approval | no (default false) |

## One-time account setup (do this first)

1. **Instagram**: switch the target account to **Business or Creator** (free,
   ~30s in IG settings) and it must connect to a Facebook Page — Upload-Post
   handles the linking during connect. Personal accounts CANNOT post via API.
2. **Upload-Post**: sign up (free tier = 10 uploads/mo, no card), connect the IG
   account, note your **profile name** (that's `UPLOADPOST_USER`) and API key.
3. **Higgsfield**: get an account + API key; confirm the REST generate endpoint.
4. **Inkbox**: reuse your existing identity/key.

## Endpoints

- `GET /run` — advance the pipeline one step (this is what a cron/heartbeat pokes)
- `GET /health` — stage + posts-made count
- `POST /idea` — queue a specific idea: `{"idea": "A day in the life in 1977 ..."}`
- `GET /` — status page

## Reels spec (already handled, but know it)

- 9:16, 1080x1920, **5–90s** to appear in the Reels tab, MP4/MOV, <1GB.
  Higgsfield generates 9:16 ≤15s → compliant.

## Maritime wake caveat (design decision that matters here)

Video gen is async — the agent must come back minutes later for the result. If
the agent sleeps and won't wake over HTTP, it never collects the video. Two
robust options:
- **Heartbeat**: an external cron (e.g. cron-job.org) hits `/run` every few
  minutes. Simple, reliable, keeps the pipeline advancing.
- **Email-wake**: have Higgsfield/webhook email the agent on completion — inbound
  email wakes Maritime agents reliably where raw HTTP doesn't.
Start with the heartbeat; it doubles as the scheduler.

## Deploy (same pattern as your other agents)

1. Push `server.py` + `Dockerfile` to a public GitHub repo.
2. `maritime create influencer-agent --repo <url> --public --port 8080 --always-on`
3. Set all env vars with `maritime env set influencer-agent KEY=value` (one line each).
4. `maritime deploy influencer-agent --source github --repo <url>`
5. Set up the heartbeat cron to hit `/run`.
6. Queue a first idea and watch the approval email arrive.

## Compliance note (say this to Maria)

This uses Instagram's **official** Content Publishing API via Upload-Post — the
same pipeline Buffer/Hootsuite use — NOT browser automation or login bots, which
violate IG's ToS and get accounts banned. The human-approval step keeps a person
in the loop on everything published.
