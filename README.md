# 🐾 PixelPaw

**A lightweight, personalised YouTube bot that replies to comments, runs your live chat, puts alerts on your OBS stream and briefs you every morning, in your own voice and in any language. No terminal needed.**

- **Double-click and go**: PixelPaw runs quietly in the background. Setup, dashboard and settings all happen in your browser. It can start with your computer or with OBS.
- **Lightweight**: pure Python with one small dependency. Runs on Windows, macOS, Linux, a Raspberry Pi or Android (Termux).
- **Any language**: replies to every viewer in the language they wrote in. The interface comes in 8 languages and can add any other with one click.
- **Any device**: an OBS overlay and dock, plus a dashboard that works on your phone.
- **Follows YouTube's Community Guidelines** on everything it posts. This can't be switched off ([details](#-youtube-community-guidelines-always-enforced)).

| Feature | What it does |
|---|---|
| 💬 **Comment replies** | Every 30 min it reads new comments, sorts them, and drafts replies in your voice. It posts them (Automatic mode) or waits for your one-tap approval (Review mode). |
| ⚙️ **Automations** | Alerts you to sponsor emails and broken-link reports, holds spam for review, and sends harsh criticism to you. |
| 🔴 **Live chat bot** | Starts when you go live: `!commands`, `!ask` (answers as you, labelled 🤖), `!top` chatters, Super Chat and member thanks, spam and link removal. |
| 🎥 **OBS overlay + dock** | Animated alerts on stream, and a control panel inside OBS. |
| 💡 **Create** | Video ideas and upload packages (titles, description, tags, chapters, thumbnail text, pinned comment), all guideline-checked. |
| 📊 **Daily report** | An AI briefing plus stats, delivered to Discord, Telegram or a file. |
| 🩺 **Health** | Status of YouTube, AI, live chat and OBS, quota and AI budgets, routines, and recent problems. |

---

## Get started (about 10 minutes, once)

1. **Install Python 3.10+** from [python.org](https://www.python.org/downloads/) (on Windows, tick *Add python.exe to PATH*).
2. **Download PixelPaw**: green **Code** button → *Download ZIP*, then unzip it anywhere.
3. **Double-click `PixelPaw.cmd`** (Windows) or **`PixelPaw.command`** (macOS / Linux).
   The first time it installs itself (about a minute). PixelPaw then opens in your browser.
4. **Follow the Setup page**:
   - **AI model**: paste a free [Gemini key](https://aistudio.google.com/apikey) (or OpenAI, or a free local model with [Ollama](https://ollama.com)), then click **Save & test**.
   - **YouTube**: the page walks you through getting Google's `client_secret.json` (5 minutes, once). Choose the file, then click **Sign in with Google**.
   - **Your voice**: click **Learn from my channel**, check the suggestion, then click **Use this**.
   - **OBS** (optional): see below.

That's it. PixelPaw works in the background from now on. To open it again, double-click the same file; it won't start twice. In **Settings → This computer** you can make it start when you log in, add a desktop shortcut, or stop it.

## 🎥 OBS

### PixelPaw plugin (Windows, OBS 30+): one PixelPaw dock

The plugin adds a single **PixelPaw** dock to OBS, just like YouTube Chat, Scenes and Controls: drag it, tab it, or show and hide it under **Docks**.
It starts in the bottom row beside OBS's **Controls**, so it never covers your chat, and it holds everything in one window:

| Tab | What's there |
|---|---|
| **Live** | status lights (YouTube, AI, live chat, overlay), *Check comments now*, *Send test alert*, the live activity feed (`!ask` answers, Super Chats, members, replies, alerts), today's budgets and reply mode |
| **Drafts** | reply drafts to approve or discard |
| **Create**, **Health**, **Settings**, **Setup** | the same pages as the full dashboard |

It also starts PixelPaw with OBS and adds **Tools → PixelPaw** (show the dock, open PixelPaw in your browser, add the alerts overlay, reload, start PixelPaw).
The dock only opens a browser while it's visible and PixelPaw is running, and OBS remembers wherever you move it.

Build and install it (needs Visual Studio Build Tools with C++; close OBS first):
```powershell
powershell -ExecutionPolicy Bypass -File obs\plugin\build.ps1 -Install
```
This installs into `C:\ProgramData\obs-studio\plugins\pixelpaw` (no admin rights needed). `python -m pixelpaw.obsplugin status|uninstall` checks or removes it. Problems show in OBS's log (*Help → Log Files*) as lines starting with `[PixelPaw]`.

### Script (any OS, no build)

In OBS: **Tools → Scripts → `+`** → choose [`obs/pixelpaw.lua`](obs/pixelpaw.lua) in the PixelPaw folder. Then:

- ☑ **Start PixelPaw when OBS starts**: PixelPaw runs whenever OBS is open, with no separate launch.
- **Add overlay to current scene**: on-stream alerts. Change position, size, language and which alerts show in *Settings → OBS overlay & language*.
- **Open PixelPaw**: opens the dashboard.
- **Dock**: *View → Docks → Custom Browser Docks* → name `PixelPaw`, URL `http://127.0.0.1:8765/`.

> Never pick the `.lua` file as a Browser Source's *Local file*: the script goes in *Tools → Scripts* only.

## 📱 On your phone
*Settings → Phone access & security* → tick **Allow phones on my Wi-Fi** → **Apply** → **Show phone link**, then open that link on your phone. Only devices on your own network can connect, and **Reset phone links** cuts off old ones.

## 🌍 Languages
Viewers always get replies in their own language. For the interface, pick a language in *Settings → OBS overlay & language*; bundled: `en hi es pt fr ar ja id`, with right-to-left support. Type any other code (e.g. `ta`, `bn`, `de`, `ko`) and click **Translate** to add it.

---

## 🛡️ YouTube Community Guidelines (always enforced)

PixelPaw follows [YouTube's Community Guidelines](https://support.google.com/youtube/answer/9288567), including the [spam & deceptive practices](https://support.google.com/youtube/answer/2801973) and [fake engagement](https://support.google.com/youtube/answer/3399767) policies, through [`pixelpaw/policy.py`](pixelpaw/policy.py). No setting can turn it off.

- **Comments that break the rules** (harassment, hate, scams, sexual, dangerous, misinformation) are never answered. They're held for your review, and you get an alert. Self-harm mentions alert you and are never hidden.
- **Everything PixelPaw posts** passes rule checks: only your own links, no sub4sub or like-to-win, no scam wording, no contact details, no spammy or repeated text. It then gets an **AI compliance review**. If the review can't run, **nothing is posted**.
- **Limits**: at most 60 replies a day, spaced 20 seconds apart. Chat commands answer at most once per 30 seconds, and AI answers are labelled 🤖.

## 🔧 Built for reliability

| | |
|---|---|
| **Recovery** | YouTube calls retry with exponential backoff and jitter. Expired sign-ins refresh themselves, and a failed routine retries after 1, 2, 4… 30 minutes. Live chat reconnects, and its watcher restarts the bot if it crashes. Replies and chat messages are **never retried after a network error**, so nothing is posted twice. |
| **YouTube quota** | Every call's cost is tracked against the daily quota, which resets at midnight Pacific. Normal work stops early to keep a reserve for live chat, and live polling slows down when quota runs low. See *Health* and *Settings → Budgets*. |
| **AI cost** | `!ask` answers come from your FAQ first, then a cache of approved answers, and only then from the AI. There are per-viewer and per-minute limits and a daily AI budget. |
| **Security** | The dashboard listens on this computer only by default. Phone access is limited to private-network devices and needs a link you can reset. Sessions use HttpOnly SameSite cookies, with CSRF and DNS-rebinding protection, lockout after repeated wrong tokens, and strict browser security headers. API keys and Google sign-in can only be changed on the computer itself. |
| **Observability** | JSON-lines logs with rotation (`data/logs/pixelpaw.jsonl`), plus a **Health** page showing component status, routines, budgets and recent problems. Startup problems go to `data/logs/startup-error.txt`. |

## 🧪 Tests
```bash
python tests/run_all.py          # offline + integration (fake YouTube API) + OBS script (needs: pip install lupa)
node tests/obs_live_check.mjs    # your real OBS: PixelPaw running, OBS WebSocket enabled; refuses while live/recording
```

---

## Advanced (optional)

Everything above works without a terminal. For power users, the same features are available as commands:

```bash
python -m pixelpaw                 # same as double-clicking
python -m pixelpaw run             # run in the foreground with logs in the terminal
python -m pixelpaw comments --dry-run | review | report | ideas | package "topic"
python -m pixelpaw live | overlay --demo | translate-ui ta | languages | doctor
```
Hand-edited defaults live in `persona.yaml`. Changes made in the dashboard are stored in `data/persona.local.json` and take priority. Keys and options are in `.env` (see `.env.example`). Free local AI: `LLM_PROVIDER=openai`, `OPENAI_BASE_URL=http://localhost:11434/v1`.

**Headless device (e.g. Raspberry Pi)?** Do the setup on your PC, then copy the `data/` folder, `.env` and `client_secret.json` to the device and run `./PixelPaw.command`.

## Project layout
```
PixelPaw.cmd / PixelPaw.command / PixelPaw.pyw   double-click launchers (install once, then run in background)
obs/pixelpaw.lua      OBS script: auto-start, overlay, open dashboard
persona.yaml          your default voice, FAQ, links, rules
pixelpaw/
  launcher.py, service.py   single-instance background service + routines with backoff
  web.py, web/              setup + dashboard app and OBS overlay (security layer)
  youtube.py, quota.py      YouTube REST + OAuth, retries, quota budget
  llm.py                    Gemini / OpenAI-compatible REST, fallbacks, AI budget
  livechat.py               live chat bot (FAQ → cache → AI)
  comments.py, policy.py    replies + YouTube guideline guard
  health.py                 JSON logs, health registry
  autostart.py, i18n.py     start-with-computer/shortcuts, languages
tests/                      offline, integration (fake YouTube), OBS script, live OBS check
```
