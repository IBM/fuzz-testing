#!/usr/bin/env python3
"""RAS-Strike Stage 1: exhaustive single-offset behavioral mapping."""

from __future__ import annotations

import argparse
from pathlib import Path

from ras_strike.backend import SystemPciBackend
from ras_strike.stage1 import Stage1Config, Stage1Paths, Stage1Runner


RANGES = {"basic": 64, "full": 256, "extended": 4096}


def hex_int(value: str) -> int:
    return int(value, 16)


def offset_list(value: str) -> tuple[int, ...]:
    if not value:
        return ()
    return tuple(sorted({int(item.strip(), 16) for item in value.split(",") if item.strip()}))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test PCIe configuration offsets and build the Stage 1 behavior map."
    )
    parser.add_argument("-d", "--device", help="PCI BDF, for example 41:00.1")
    parser.add_argument(
        "-r",
        "--range",
        dest="range_name",
        choices=RANGES,
        default="extended",
        help="Configuration-space range (default: extended)",
    )
    parser.add_argument(
        "-mode",
        "--mode",
        choices=("exhaustive", "fast"),
        default="exhaustive",
        help="exhaustive tests 0x00..0xff; fast tests one value",
    )
    parser.add_argument("-v", "--value", type=hex_int, default=0xFF, help="Fast-mode byte value")
    parser.add_argument("-s", "--skip", type=offset_list, default=(), help="Skipped hex offsets")
    parser.add_argument(
        "-exclude",
        "--exclude",
        type=offset_list,
        default=(),
        help="Hex offsets ignored when classifying secondary changes",
    )
    parser.add_argument("-start", "--start", type=hex_int, default=0, help="Starting hex offset")
    parser.add_argument(
        "-delay",
        "--delay",
        type=float,
        default=0,
        help="Post-write delay in seconds (default: 0)",
    )
    parser.add_argument("-restore", "--restore", action="store_true")
    parser.add_argument("-noreset", "--no-reset", action="store_true")
    parser.add_argument(
        "--max-tests",
        type=int,
        help="Stop cleanly after this many completed write tests",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print every write and result with an elapsed-time prefix",
    )
    parser.add_argument("--output-dir", type=Path, default=Path.cwd())
    parser.add_argument(
        "--resume",
        type=Path,
        help="Resume from a Stage 1 checkpoint; stored run configuration is reused",
    )
    args = parser.parse_args()
    if not args.resume and not args.device:
        parser.error("-d/--device is required unless --resume is used")
    if not 0 <= args.value <= 0xFF:
        parser.error("--value must be between 00 and ff")
    if args.start < 0 or args.start >= RANGES[args.range_name]:
        parser.error("--start must be inside the selected range")
    if args.max_tests is not None and args.max_tests < 1:
        parser.error("--max-tests must be positive")
    return args


def main() -> None:
    args = parse_args()
    if args.resume:
        import json

        with args.resume.open(encoding="utf-8") as source:
            device_id = json.load(source)["config"]["device_id"]
        runner = Stage1Runner.resume(
            SystemPciBackend(device_id),
            args.resume,
            verbose=args.verbose,
        )
    else:
        config = Stage1Config(
            device_id=args.device,
            byte_range=RANGES[args.range_name],
            start_offset=args.start,
            skip_offsets=args.skip,
            exclude_offsets=args.exclude,
            mode=args.mode,
            value=args.value,
            delay=args.delay,
            restore=args.restore,
            reset_at_end=not args.no_reset,
            max_tests=args.max_tests,
        )
        paths = Stage1Paths.create(args.output_dir)
        runner = Stage1Runner(
            SystemPciBackend(config.device_id),
            config,
            paths,
            verbose=args.verbose,
        )
    try:
        runner.run()
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
