# 🐾 PixelPaw

**A lightweight, personalised YouTube bot that replies to comments, runs your live chat, puts alerts on your OBS stream and briefs you every morning, in your own voice and in any language.**

- **Lightweight**: pure Python with one dependency (PyYAML). No Google or OpenAI SDKs and no build step. It runs on Windows, macOS, Linux, a Raspberry Pi, or an Android phone (Termux).
- **Any language**: replies to every viewer in the language they wrote in. The overlay, dashboard and reports come in 8 bundled languages, and you can add any other with one command.
- **Works on any device**: an OBS overlay for your stream, a dashboard that works as an OBS dock **and** on your phone, and reports on Discord or Telegram.
- **Follows YouTube's Community Guidelines** on everything it posts. This can't be switched off ([details](#-youtube-community-guidelines-always-enforced)).

| Feature | What it does |
|---|---|
| 💬 **Comment replies** | Every 30 min it reads new comments, sorts them (question, praise, feedback, criticism, collab, spam), and drafts replies in your voice. It posts them (auto mode) or queues them for you to approve (review mode). |
| ⚙️ **Automations** | Rules on comment content: alert you about sponsor emails and broken-link reports, hold spam for review, send harsh criticism to you. |
| 🔴 **Live chat bot** | Starts when you go live: `!commands`, `!ask` (AI answers as you, labelled 🤖), `!top` chatters, Super Chat and member thanks, spam and link removal. |
| 🎥 **OBS overlay + dock** | Animated on-stream alerts for Super Chats, new members and `!ask` answers, plus a control panel inside OBS. |
| 📱 **Dashboard** | Live status, stats, approve or edit reply drafts, and an activity feed. Works on a phone. |
| 💡 **Content assistant** | `ideas` (what to make next, from your stats and viewer questions) and `package` (titles, description, tags, chapters, thumbnail text, pinned comment), all guideline-checked. |
| 📊 **Daily report** | An AI briefing (what happened, what needs attention, what to do today), then stats, analytics, uploads, and unanswered or flagged comments, sent to a file, Discord or Telegram. |

Its design follows DailyBot's *routines → intelligence → automations → reports* model ([analysis](docs/DAILYBOT_ANALYSIS.md)).

---

## Quick start

Requires **Python 3.10+**.

```bash
git clone https://github.com/shivam-srivastava-031/PixelPaw.git
cd PixelPaw
python -m venv .venv
# Windows: .venv\Scripts\activate     macOS/Linux/Termux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                # Windows: copy .env.example .env
```

### 1. AI key
In `.env`, set **one** of these:
- `LLM_PROVIDER=gemini` and `GEMINI_API_KEY=…` (free tier at <https://aistudio.google.com/apikey>)
- `LLM_PROVIDER=openai` and `OPENAI_API_KEY=…`
- A **free local model**: `LLM_PROVIDER=openai`, `OPENAI_BASE_URL=http://localhost:11434/v1`, `OPENAI_MODEL=llama3.2` ([Ollama](https://ollama.com)). Any OpenAI-compatible API (OpenRouter, Groq…) works the same way.

If a model is busy, PixelPaw automatically moves to the fallback models (`GEMINI_FALLBACK_MODELS`).

### 2. Let PixelPaw act on your channel (Google Cloud, once)
1. Go to <https://console.cloud.google.com/> and create a project.
2. **APIs & Services → Library**: enable **YouTube Data API v3** and **YouTube Analytics API**.
3. **OAuth consent screen**: External. Add the Google account that owns your channel as a **test user**.
4. **Credentials → Create credentials → OAuth client ID → Desktop app**. Download the JSON and save it as `client_secret.json` in this folder.

> In *Testing* mode Google expires the sign-in after 7 days. Run `auth` again, or click *Publish app* on the consent screen.

### 3. Sign in and make it yours
```bash
python -m pixelpaw auth            # opens the browser; pick your channel's account
python -m pixelpaw persona-learn   # studies your channel + past replies -> persona.suggested.yaml
python -m pixelpaw doctor          # everything should be ✔
python -m pixelpaw comments --dry-run
python -m pixelpaw run             # everything on schedule + overlay + dashboard
```
Copy the good parts of `persona.suggested.yaml` into **`persona.yaml`**, the file that makes the bot sound like you (tone, FAQ, links, chat commands, rules).

> **Headless device (Raspberry Pi, server)?** Run `auth` on your PC, then copy `data/token.json` to the device.

---

## 🎥 OBS setup (2 minutes, no plugins to compile)

Start PixelPaw first (`python -m pixelpaw run`, or `python -m pixelpaw overlay` for the overlay only). It prints two links.

**Overlay (on-stream alerts)**
- Easiest: **Tools → Scripts → `+`** and pick [`obs/pixelpaw.lua`](obs/pixelpaw.lua), then click **Add overlay to current scene**.
- Or manually: **Sources → `+` → Browser**, URL `http://127.0.0.1:8765/overlay`, set the size to your canvas (e.g. 1920×1080). Leave **Local file** unticked: the `.lua` file goes in *Tools → Scripts*, not in a Browser Source.
- To position it before going live, open `http://127.0.0.1:8765/overlay?demo=1` or run `python -m pixelpaw overlay --demo`.

Overlay options (add them to the URL): `?lang=hi` · `&pos=bottom-right` (`bottom-left`, `top-left`, `top-right`, `center`) · `&scale=1.3` · `&seconds=10` · `&show=super_chat,member,ask` · `&theme=light`

**Dock (control panel inside OBS)**
**View → Docks → Custom Browser Docks…**, Name `PixelPaw`, URL = the **Dashboard** link PixelPaw printed (`http://127.0.0.1:8765/dock?token=…`).

## 📱 Dashboard on your phone
Set `PIXELPAW_WEB_HOST=0.0.0.0` in `.env` and restart. PixelPaw prints a `http://192.168.x.x:8765/dock?token=…` link; open it on your phone on the same Wi-Fi (allow Python through the firewall if asked). Keep the token private: it lets the holder approve replies.

## 🌍 Languages
- **Replies**: comments and `!ask` answers are written in the viewer's own language, whatever it is.
- **Interface** (overlay, dashboard, reports): bundled `en hi es pt fr ar ja id`, with right-to-left support for Arabic, Urdu, Hebrew and Persian. Set `ui.language` in `persona.yaml`.
- **Add any language**: `python -m pixelpaw translate-ui ta --name Tamil`, then set `ui: {language: ta}`. `python -m pixelpaw languages` lists what's installed.

---

## 🛡️ YouTube Community Guidelines (always enforced)

PixelPaw follows [YouTube's Community Guidelines](https://support.google.com/youtube/answer/9288567), including the [spam & deceptive practices](https://support.google.com/youtube/answer/2801973) and [fake engagement](https://support.google.com/youtube/answer/3399767) policies. The guard lives in [`pixelpaw/policy.py`](pixelpaw/policy.py), and **nothing in `persona.yaml` can switch it off**.

| Where | What happens |
|---|---|
| Every AI prompt | The guideline rules are appended last, so persona settings can't override them. |
| Incoming comments | Flagged comments (harassment, hate, scams, sexual, dangerous, misinformation…) are **never replied to** and are held for review, and you get an alert. Self-harm mentions alert you instead of being hidden. |
| Everything posted | **Rule checks** (only your own links, no sub4sub or like-to-win, no scam wording, no emails or phone numbers, no spammy formatting or duplicates), then an **AI compliance review**. If the review can't run, **nothing is posted**. |
| Your approvals | Replies approved in the terminal or dashboard go through the same checks. |
| Volume | At most 60 replies a day, 20 seconds apart. Chat commands answer once per 30 seconds. |
| Live chat | Viewer links and sub4sub or scam messages are removed (mods exempt). Rule-breaking canned messages are disabled at startup. AI answers are labelled 🤖. |
| Upload packages | No misleading clickbait or keyword stuffing, tags fit YouTube's 500-character limit, and each package gets a guideline check. |

You remain responsible for your channel. Start in `review` mode and skim what the bot posts.

---

## Commands

```bash
python -m pixelpaw run                 # everything on schedule (comments, report, live chat, overlay, dashboard)
python -m pixelpaw comments [--dry-run]
python -m pixelpaw review              # approve / edit / discard drafts in the terminal
python -m pixelpaw report [--no-send]
python -m pixelpaw ideas --count 10
python -m pixelpaw package "My video topic" --transcript script.txt
python -m pixelpaw live [--video-id ID]
python -m pixelpaw overlay [--demo]
python -m pixelpaw translate-ui <code> | languages
python -m pixelpaw auth | whoami | doctor | persona-learn
```
Or `pip install .` and use `pixelpaw <command>` from anywhere inside your PixelPaw folder.

**Run at startup (Windows):** Task Scheduler → *At log on* → Program `…\PixelPaw\.venv\Scripts\python.exe`, Arguments `-m pixelpaw run`, Start in `…\PixelPaw`.
**Linux / Raspberry Pi:** a systemd service or `@reboot cd ~/PixelPaw && .venv/bin/python -m pixelpaw run` in `crontab -e`.

## Limits
- **YouTube API quota**: 10,000 units/day free. Reads cost 1 unit; each posted reply, moderation action or chat message costs 50. The defaults fit a normal day.
- **Privacy**: everything runs on your machine. Your keys stay in `.env` and your sign-in in `data/`; both are git-ignored.

## Project layout
```
persona.yaml          your voice, FAQ, links, rules, UI language   ← edit this
.env                  keys and settings (never commit)
obs/pixelpaw.lua      OBS script: adds the overlay in one click
pixelpaw/
  policy.py           YouTube guideline guard
  comments.py         comment routine + review      intelligence.py  classification, drafts, briefing
  livechat.py         live chat bot                  automations.py   content rules
  web.py, web/        overlay + dashboard server     events.py        live event bus
  content.py          ideas, upload packages         report.py        daily report
  youtube.py          YouTube REST + OAuth           llm.py           Gemini / OpenAI-compatible REST
  i18n.py, locales/   interface languages            scheduler.py     timezone-aware routines
tests/test_offline.py offline tests (no keys needed): python tests/test_offline.py
```
