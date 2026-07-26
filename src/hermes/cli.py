"""The ``hermes`` command-line entry point."""

from __future__ import annotations

import argparse

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes", description="Hermes trading framework")
    parser.add_argument("--version", action="version", version=f"hermes {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    ins = sub.add_parser(
        "install-skills",
        help="Copy the Hermes Claude Code skills into a project (or ~/.claude with --user).",
    )
    ins.add_argument("--user", action="store_true", help="Install into ~/.claude/skills")
    ins.add_argument("--dest", default=".", help="Project dir to install into (default: cwd)")

    args = parser.parse_args(argv)

    if args.command == "install-skills":
        from .skilltools import PORTABLE_SKILLS, install_skills

        target = install_skills(dest=args.dest, user=args.user)
        print(f"Installed {len(PORTABLE_SKILLS)} skills + hermes-reference/ into {target}")
        print("Open this project in Claude Code and type /ask-hermes to start.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
