"""CLI entry point for the manual Optimization Loop (Día 4-6, TP4).

Runs as:
    uv run python -m app.cli.run_optimization_loop --project-id pyme_maria --limit 500
    uv run python -m app.cli.run_optimization_loop --project-id pyme_maria --limit 500 --dry-run

Typer is NOT a project dependency; using stdlib argparse to avoid adding a dep
(the CLI is a research/TFG tool, not a product surface).

Output:
  - Header: project, date, eval count.
  - Per sub-agent: pattern name + root_cause + unified diff of prompt_before → prompt_after.
  - Footer: total cost USD + persisted improvement IDs + DB query hint.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import logging
import sys
import textwrap
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_optimization_loop",
        description=(
            "Manual one-shot Optimization Loop: detect failure patterns in "
            "evaluated conversations and propose prompt improvements."
        ),
    )
    parser.add_argument(
        "--project-id",
        required=True,
        metavar="PUBLIC_ID",
        help="project.public_id to analyze (e.g. pyme_maria)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        metavar="N",
        help="Max evaluations to load per sub-agent analysis (default: 500)",
    )
    parser.add_argument(
        "--sub-agents",
        nargs="+",
        default=["productos", "pedidos", "devoluciones", "pagos"],
        metavar="SLUG",
        help="Sub-agent slugs to analyze (default: productos pedidos devoluciones pagos)",
    )
    parser.add_argument(
        "--prompt-version",
        default="v1",
        metavar="VERSION",
        help="Prompt version label to load from filesystem (default: v1)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="If set, skip Sonnet calls and DB writes; only print what would happen",
    )
    return parser


def _print_separator(char: str = "-", width: int = 72) -> None:
    print(char * width)


def _print_diff(before: str, after: str, sub_agent: str) -> None:
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    diff = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"prompt_before ({sub_agent})",
            tofile=f"prompt_after ({sub_agent})",
            lineterm="",
        )
    )
    if diff:
        print("\n".join(diff))
    else:
        print("(no diff — prompt_before and prompt_after are identical)")


def _print_results(output) -> None:  # noqa: ANN001
    """Print the OptimizationLoopOutput to stdout in human-readable format."""
    _print_separator("=")
    print(f"  OPTIMIZATION LOOP RESULTS")
    print(f"  Project       : {output.project_public_id}")
    print(f"  Started at    : {output.started_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Completed at  : {output.completed_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Sub-agents    : {output.sub_agents_analyzed}")
    print(f"  Patterns found: {len(output.patterns)}")
    print(f"  Cost (Sonnet) : ${output.sonnet_cost_usd:.6f} USD")
    _print_separator("=")

    if not output.patterns:
        print("\nNo patterns detected. Possible causes:")
        print("  - Fewer than 10 failed evals per sub-agent in the DB.")
        print("  - --dry-run mode was active (no Sonnet calls).")
        return

    for i, p in enumerate(output.patterns, start=1):
        print(f"\n[Pattern {i}/{len(output.patterns)}]  sub_agent={p.sub_agent}")
        _print_separator()
        print(f"  Pattern    : {p.pattern}")
        print(f"  Root cause : {p.root_cause}")
        print(f"  Impact     : {p.impact_estimate.upper()}")
        if p.affected_conv_ids:
            print(f"  Conv IDs   : {', '.join(p.affected_conv_ids[:5])}", end="")
            if len(p.affected_conv_ids) > 5:
                print(f"  (+{len(p.affected_conv_ids) - 5} more)", end="")
            print()
        if p.sample_excerpts:
            print("  Excerpts   :")
            for excerpt in p.sample_excerpts[:3]:
                wrapped = textwrap.fill(
                    excerpt,
                    width=68,
                    initial_indent="    > ",
                    subsequent_indent="      ",
                )
                print(wrapped)
        print("\n  Prompt diff:")
        _print_diff(p.prompt_before, p.prompt_after, p.sub_agent)

    _print_separator("=")
    print(f"\nImprovements persisted ({len(output.improvements_persisted_ids)}):")
    for imp_id in output.improvements_persisted_ids:
        print(f"  {imp_id}")

    if output.improvements_persisted_ids:
        ids_sql = ", ".join(f"'{x}'" for x in output.improvements_persisted_ids)
        print(
            f"\nDB query to inspect:\n"
            f"  SELECT * FROM improvements WHERE public_id IN ({ids_sql});"
        )

    print(f"\nTotal Sonnet cost: ${output.sonnet_cost_usd:.6f} USD")
    _print_separator("=")


async def _run(args: argparse.Namespace) -> int:
    from app.core.db import async_session_factory
    from app.services.optimization_loop import (
        OptimizationLoopInput,
        run_optimization_loop,
    )

    input_ = OptimizationLoopInput(
        project_public_id=args.project_id,
        limit=args.limit,
        sub_agents=args.sub_agents,
        prompt_version_label=args.prompt_version,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        print(
            f"[DRY RUN] project={args.project_id} limit={args.limit} "
            f"sub_agents={args.sub_agents} — no Sonnet calls, no DB writes"
        )

    try:
        async with async_session_factory() as session:
            output = await run_optimization_loop(input_, session)
        _print_results(output)
        return 0
    except ValueError as exc:
        logger.error("[CLI] validation error: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        logger.error("[CLI] runtime error: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.error("[CLI] unexpected error: %s", exc, exc_info=True)
        print(f"Unexpected error: {exc}", file=sys.stderr)
        return 2


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    exit_code = asyncio.run(_run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
