"""CLI entry point — send a message to the tutor."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from salient_tutor.daemon import TutorDaemon


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="salient-tutor",
        description="A spaced-repetition teaching agent on salient-core.",
    )
    parser.add_argument("message", nargs="?", help="Message to send to the tutor.")
    parser.add_argument(
        "--agent",
        default="tutor",
        choices=["tutor", "librarian"],
        help="Which agent to address (default: tutor).",
    )
    parser.add_argument(
        "--work-root",
        default="work",
        help="Working directory for persistent state (default: work).",
    )
    parser.add_argument("--session-id", help="Resume a durable lesson session.")
    parser.add_argument(
        "--new-session", action="store_true", help="Create a durable session before prompting."
    )
    parser.add_argument(
        "--analytics", action="store_true", help="Print local learning analytics and exit."
    )
    parser.add_argument(
        "--migration-report",
        action="store_true",
        help="Print the lessons.db migration report and exit.",
    )
    args = parser.parse_args()

    if args.analytics or args.migration_report:
        daemon = TutorDaemon(work_root=args.work_root)
        result = daemon.analytics() if args.analytics else daemon.migration_report()
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    if not args.message:
        parser.print_help()
        sys.exit(1)

    result = asyncio.run(
        _run(args.agent, args.message, args.work_root, args.session_id, args.new_session)
    )
    print(result)


async def _run(
    agent: str,
    message: str,
    work_root: str,
    session_id: str | None = None,
    new_session: bool = False,
) -> str:
    daemon = TutorDaemon(work_root=work_root)
    await daemon.start()
    try:
        if new_session:
            session = daemon.create_session(f"custom:{message.strip().lower().replace(' ', '-')}")
            session_id = session["session_id"]
        elif session_id:
            daemon.get_session(session_id)
        return await daemon.prompt(agent, message, session_id=session_id)
    finally:
        await daemon.stop()


if __name__ == "__main__":
    main()
