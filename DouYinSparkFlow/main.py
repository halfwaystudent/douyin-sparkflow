import argparse
import asyncio
import os

from rich.console import Console
from rich.prompt import Prompt

from core.login import userLogin
from utils.github_action_config import print_github_action_config


console = Console()


def interactive_cli():
    console.print("[bold green]Welcome to DouYin Spark Flow[/bold green]")
    console.print("[bold yellow]Choose an action:[/bold yellow]")
    console.print("[cyan]1.[/cyan] Add account login")
    console.print("[cyan]2.[/cyan] Export USER_DATA for GitHub Actions")
    console.print("[cyan]3.[/cyan] Run task locally")
    console.print("[cyan]4.[/cyan] Start Web Admin UI")

    choice = Prompt.ask("Enter a choice (1/2/3/4)", choices=["1", "2", "3", "4"])

    if choice == "1":
        console.print("[bold blue]Starting account login...[/bold blue]")
        while True:
            asyncio.run(userLogin())
            if Prompt.ask("Continue adding accounts? (y/n)", choices=["y", "n"]) == "n":
                break
    elif choice == "2":
        print_github_action_config()
    elif choice == "3":
        from core.tasks import runTasks

        asyncio.run(runTasks())
    else:
        from webui.app import run_web_app

        run_web_app()


def build_parser():
    parser = argparse.ArgumentParser(description="DouYin Spark Flow")
    parser.add_argument("--doTask", action="store_true", help="Run the message task immediately")
    parser.add_argument(
        "--patrol",
        action="store_true",
        help="Run the read-only account patrol (login state and target friends)",
    )
    parser.add_argument(
        "--account",
        action="append",
        default=None,
        help="Limit --patrol to these account refs (repeatable or comma separated)",
    )
    parser.add_argument("--web", action="store_true", help="Start the Web Admin UI")
    parser.add_argument("--host", default=None, help="Web UI bind host")
    parser.add_argument("--port", type=int, default=None, help="Web UI bind port")
    return parser


def patrol_account_refs(values):
    refs = [
        part.strip()
        for value in values or []
        for part in str(value or "").split(",")
        if part.strip()
    ]
    return refs or None


def run_patrol_cli(account_refs=None):
    from core.patrol import run_patrol

    return asyncio.run(run_patrol(account_refs, trigger="scheduled"))


def task_mode():
    """Cron passes SPARKFLOW_TASK_MODE=patrol to the shared task entry point."""
    return str(os.getenv("SPARKFLOW_TASK_MODE") or "").strip().lower()


def run(args):
    if args.patrol or (args.doTask and task_mode() == "patrol"):
        return run_patrol_cli(patrol_account_refs(args.account))
    if args.doTask:
        from core.tasks import runTasks

        return asyncio.run(runTasks())
    if args.web:
        from webui.app import run_web_app

        return run_web_app(host=args.host, port=args.port)
    return interactive_cli()


if __name__ == "__main__":
    run(build_parser().parse_args())
