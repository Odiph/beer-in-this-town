"""enrich / filter / export -- STUB.

Placeholder written by stream A1 so `cli.py` can import this module on its
branch. Stream A2 owns the real implementation; the integrator takes A2's
version of this file and discards this one.

Interface `cli.py` relies on:

    add_parsers(sub, common) -> None      register the subcommands
    dispatch(args, s) -> Envelope | None  None when args.cmd is not ours
"""
from __future__ import annotations

import argparse

from .agent_io import Envelope
from .config import Settings

COMMANDS: frozenset[str] = frozenset()


def add_parsers(sub: argparse._SubParsersAction,
                common: argparse.ArgumentParser) -> None:
    """Register enrich / filter / export. The stub registers nothing."""
    return None


def dispatch(args: argparse.Namespace, s: Settings) -> Envelope | None:
    """Run one of this module's commands, or None if `args.cmd` is not one."""
    return None
