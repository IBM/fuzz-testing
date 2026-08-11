#!/usr/bin/env python3
"""RAS-Strike Stage 2: structure-guided sequence exploration."""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from ras_strike.backend import SystemPciBackend
from ras_strike.stage2 import (
    Stage2Config,
    Stage2Paths,
    Stage2Runner,
    load_stage1_summary,
)


def offset_list(value: str) -> tuple[int, ...]:
    if not value:
        return ()
    return tuple(sorted({int(item.strip(), 16) for item in value.split(",") if item.strip()}))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use a Stage 1 behavior map for structure-guided PCIe sequence exploration."
    )
    parser.add_argument("-d", "--device", help="PCI BDF; defaults to the Stage 1 device")
    parser.add_argument("-j", "-json", "--stage1", type=Path, required=True)
    parser.add_argument("-iter", "-i", "--iterations", type=int, default=10000)
    parser.add_argument("-writes", "-w", "--writes", type=int, default=2)
    parser.add_argument(
        "-delay",
        "--delay",
        type=float,
        default=0,
        help="Post-write delay in seconds (default: 0)",
    )
    parser.add_argument("-seed", "--seed", type=int, default=None)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print every write and sequence result with an elapsed-time prefix",
    )
    parser.add_argument(
        "-exclude",
        "--exclude",
        type=offset_list,
        default=None,
        help="Noisy hex offsets ignored in state diffs; defaults to Stage 1",
    )
    parser.add_argument("--output-dir", type=Path, default=Path.cwd())
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    if args.writes < 2:
        parser.error("--writes must be at least 2 for multi-register exploration")
    return args


def main() -> None:
    args = parse_args()
    stage1 = load_stage1_summary(args.stage1)
    device_id = args.device or stage1.get("device_id")
    if not device_id:
        raise SystemExit("A device BDF is required with -d/--device")
    seed = args.seed if args.seed is not None else int(time.time())
    if args.exclude is None:
        stage1_excluded = stage1.get("config", {}).get("exclude_offsets", [])
        excluded = tuple(
            int(item, 16) if isinstance(item, str) else int(item)
            for item in stage1_excluded
        )
    else:
        excluded = args.exclude
    config = Stage2Config(
        device_id=device_id,
        iterations=args.iterations,
        writes_per_sequence=args.writes,
        delay=args.delay,
        seed=seed,
        exclude_offsets=excluded,
    )
    paths = Stage2Paths.create(args.output_dir)
    runner = Stage2Runner(
        SystemPciBackend(device_id),
        config,
        stage1,
        paths,
        verbose=args.verbose,
    )
    try:
        runner.run()
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
