# DailyBot → PixelPaw: what REA showed and what we copied

**Tool:** [REA](https://github.com/morluto/rea) `rea-agents@6.8.0` (npm), run against DailyBot's **public** website in headless Chrome over CDP. Only publicly served pages and scripts were analysed. No login, private API or app internals were touched.

## What REA found

| Command | Result |
|---|---|
| `rea analyze-web-bundle` on `https://www.dailybot.com/` | 17 scripts captured and analysed without execution. The site is an **Astro + Svelte** front end (`/_astro/*.js`). The route inventory found 11 candidates: `/product`, `/product/check-ins`, `/skills`, `/cli`, locale roots `/es`, `/pt`, and blog posts on async check-ins and dependency management. Evidence id `ev_8c2eac10…`. |
| `rea discover-webmcp-tools` | No tools registered at runtime in headless Chrome (status `available`, 0 tools). |
| Static read of `WebMCPBridge.js` (found by REA) | The site exposes two WebMCP tools to AI agents: `search_blog` and `search_help`, both backed by static JSON (`/api/posts.json`, `/api/help.json`). This is agent-friendly docs, not product logic. |

The `/cli` and `/skills` routes, plus the WebMCP bridge, show that DailyBot positions itself as usable by AI agents as well as humans ("agents can submit responses the same way humans do").

## DailyBot's product model (from REA routes + the `/product/check-ins` page)

1. **Check-ins (routines)**: questions, participants, a recurring **timezone-aware schedule**, reminders.
2. **Reports**: compiled and posted to team channels: who responded, what they said, **flagged blockers**, who didn't respond.
3. **Intelligence**: an AI **summary at the top** of the report, blocker extraction, risk signals.
4. **Automations**: rules **triggered by response content** (e.g. a blocker creates a ticket) and follow-through such as escalation.
5. **Kudos**: recognition.

## How PixelPaw maps it

| DailyBot | PixelPaw |
|---|---|
| Check-in routine on a schedule | `scheduler.py`: comment sweep every N min, daily report at a local time, live-stream detector |
| Responses collected async | New comments and live chat messages |
| Intelligence: classify, summarise, flag blockers | `intelligence.py`: categories, sentiment, `needs_human`, drafts in your voice, AI briefing on top of the report |
| Automations on response content | `automations.py`: `when` (category, sentiment, contains_any) → `then` (notify, reply, review, moderate) |
| Report delivered to channels | `report.py` + `notify.py`: file, Discord, Telegram |
| "Who didn't respond" / blockers | Unanswered questions, flagged comments, pending drafts |
| Kudos | `!top` chatters, Super Chat and new-member thanks |
| Human-in-the-loop | `review` mode and the `review` command |
