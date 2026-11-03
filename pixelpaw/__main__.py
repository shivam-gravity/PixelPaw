"""PixelPaw command line.  Run `python -m pixelpaw --help`."""
from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # emoji-safe on Windows consoles
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(prog="pixelpaw", description="🐾 PixelPaw: your personalised YouTube channel bot.")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check your setup")
    a = sub.add_parser("auth", help="sign in to YouTube (opens a browser)")
    a.add_argument("--no-browser", action="store_true", help="just print the sign-in link")
    sub.add_parser("whoami", help="show the connected channel")
    c = sub.add_parser("comments", help="one comment sweep: classify, reply or draft")
    c.add_argument("--dry-run", action="store_true", help="show what would happen; post nothing, save nothing")
    sub.add_parser("review", help="approve / edit / discard reply drafts")
    r = sub.add_parser("report", help="build the daily report now")
    r.add_argument("--no-send", action="store_true", help="save to reports/ only")
    i = sub.add_parser("ideas", help="video ideas from your stats and viewer questions")
    i.add_argument("--count", type=int, default=10)
    i.add_argument("--theme", default="")
    k = sub.add_parser("package", help="titles, description, tags, chapters for a video")
    k.add_argument("topic")
    k.add_argument("--transcript", help="path to a transcript .txt (timestamps -> chapters)")
    sub.add_parser("persona-learn", help="draft persona.suggested.yaml from your channel and past replies")
    lv = sub.add_parser("live", help="run the live chat bot on your current stream")
    lv.add_argument("--video-id", help="stream video id (default: your active broadcast)")
    sub.add_parser("run", help="run everything on schedule (comments, report, live chat, overlay)")
    ov = sub.add_parser("overlay", help="only run the OBS overlay + dashboard")
    ov.add_argument("--demo", action="store_true", help="send sample alerts every few seconds to position the overlay")
    tr = sub.add_parser("translate-ui", help="add an interface language with AI, e.g. translate-ui ta")
    tr.add_argument("lang", help="language code, e.g. ta, bn, de, ko, tr, vi")
    tr.add_argument("--name", default="", help="language name, e.g. Tamil")
    sub.add_parser("languages", help="list interface languages")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    from .app import App

    app = App()

    if args.cmd == "doctor":
        return doctor(app)
    if args.cmd == "languages":
        from .i18n import available, strings

        for code in available():
            print(f"{code:<6} {strings(code).get('_name', code)}")
        print("\nAdd any other language:  python -m pixelpaw translate-ui <code>")
        return 0
    if args.cmd == "overlay":
        return overlay(app, args.demo)

    from .llm import LLMError
    from .net import ApiError
    from .youtube import NotAuthorised

    if args.cmd == "auth":
        try:
            from .youtube import sign_in

            sign_in(app.settings, open_browser=not args.no_browser)
            print(f"Signed in. Token saved to {app.settings.token_file}")
            print(f"Connected channel: {app.yt.channel()['snippet']['title']}")
            return 0
        except (NotAuthorised, ApiError) as exc:
            print(exc)
            return 2

    try:
        if args.cmd == "whoami":
            ch = app.yt.channel()
            st = ch["statistics"]
            print(f"{ch['snippet']['title']}  ({ch['snippet'].get('customUrl', '')})  id={ch['id']}")
            print(f"subscribers={st.get('subscriberCount', 'hidden')} views={st.get('viewCount')} videos={st.get('videoCount')}")
        elif args.cmd == "comments":
            from .comments import sweep

            print(sweep(app, dry_run=args.dry_run))
        elif args.cmd == "review":
            from .comments import review

            review(app)
        elif args.cmd == "report":
            from .report import daily_report

            print(daily_report(app, send=not args.no_send))
        elif args.cmd == "ideas":
            from .content import ideas

            print(ideas(app, args.count, args.theme))
        elif args.cmd == "package":
            from .content import package

            print(package(app, args.topic, args.transcript))
        elif args.cmd == "persona-learn":
            from .content import learn_persona

            path = learn_persona(app)
            print(f"Draft persona written to {path}\nReview it and copy what you like into persona.yaml.")
        elif args.cmd == "live":
            from .livechat import LiveChatBot

            found = app.yt.active_live_chat(args.video_id)
            if not found:
                print("No active live chat found. Start your stream (or pass --video-id) and try again.")
                return 1
            chat_id, title = found
            from .web import start_web

            web = start_web(app)
            try:
                LiveChatBot(app, app.yt, chat_id, title).run()
            except KeyboardInterrupt:
                pass
            finally:
                if web:
                    web.shutdown()
        elif args.cmd == "run":
            from .scheduler import run_forever

            run_forever(app)
        elif args.cmd == "translate-ui":
            from .i18n import translate_ui

            path = translate_ui(app.llm, args.lang, args.name)
            print(f"Created {path}. Set  ui: {{language: {args.lang}}}  in persona.yaml to use it.")
    except NotAuthorised as exc:
        print(exc)
        return 2
    except LLMError as exc:
        print(f"AI error: {exc}")
        return 3
    except ApiError as exc:
        print(f"YouTube/network error: {exc}")
        return 4
    return 0


def overlay(app, demo: bool) -> int:
    import itertools
    import threading

    from .events import bus
    from .web import start_web

    web = start_web(app)
    if not web:
        return 1
    stop = threading.Event()
    samples = itertools.cycle([
        ("super_chat", {"name": "Aarav", "amount": "₹500", "message": "Love the streams! 🔥"}),
        ("member", {"name": "Sofia"}),
        ("ask", {"name": "Kenji", "question": "Which IDE do you use?", "answer": "VS Code with the Python extension 🙂"}),
        ("super_chat", {"name": "مريم", "amount": "$5", "message": "تحية من القاهرة"}),
    ])
    print("Overlay running. Press Ctrl+C to stop." + (" Sending demo alerts every 5s." if demo else ""))
    try:
        while not stop.wait(5 if demo else 3600):
            if demo:
                kind, data = next(samples)
                bus.emit(kind, **data)
    except KeyboardInterrupt:
        pass
    web.shutdown()
    return 0


def doctor(app) -> int:
    s, ok = app.settings, True

    def line(good: bool, msg: str, fix: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"{'✔' if good else '✖'} {msg}" + (f"\n    → {fix}" if fix and not good else ""))

    line(s.persona_file.exists(), f"persona file: {s.persona_file}", "copy persona.yaml from the repo")
    line(app.persona["channel"]["name"] not in ("", "My Channel", "YOUR CHANNEL NAME"),
         f"persona channel name: {app.persona['channel']['name']}", "edit persona.yaml, or run persona-learn")
    line(s.llm_provider in ("openai", "gemini"), f"LLM provider: {s.llm_provider}", "set LLM_PROVIDER=openai or gemini in .env")
    key = s.openai_api_key if s.llm_provider == "openai" else s.gemini_api_key
    line(bool(key), f"{s.llm_provider} API key set", f"add {s.llm_provider.upper()}_API_KEY to .env")
    line(s.client_secrets_file.exists(), f"Google OAuth client: {s.client_secrets_file}", "README step 2")
    line(s.token_file.exists(), f"YouTube sign-in token: {s.token_file}", "run: python -m pixelpaw auth")
    targets = app.persona["report"]["deliver_to"]
    if "discord" in targets:
        line(bool(s.discord_webhook_url), "Discord webhook set", "add DISCORD_WEBHOOK_URL to .env")
    if "telegram" in targets:
        line(bool(s.telegram_bot_token and s.telegram_chat_id), "Telegram bot set", "add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env")
    print(f"\ncomment mode: {app.persona['comments']['mode']}  ·  report at {app.persona['report']['time']} {s.timezone}")
    print("All good." if ok else "Fix the ✖ items above.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
