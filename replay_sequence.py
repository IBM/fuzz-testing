#!/usr/bin/env python3
"""Manually replay a recorded RAS-Strike write sequence."""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path

from ras_strike.backend import PciBackend, SystemPciBackend
from ras_strike.durable import append_jsonl, utc_now


def validate_write(offset: int, value: int) -> tuple[int, int]:
    if not 0 <= offset < 4096:
        raise ValueError(f"Offset 0x{offset:x} is outside PCIe configuration space")
    if not 0 <= value <= 0xFF:
        raise ValueError(f"Value 0x{value:x} is not a byte")
    return offset, value


def load_legacy_pattern(path: Path) -> list[tuple[int, int]]:
    writes: list[tuple[int, int]] = []
    pattern = re.compile(r"@\s*([0-9a-fA-Fx]+)\s+([0-9a-fA-Fx]+)")
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            match = pattern.search(line)
            if not match:
                raise ValueError(f"{path}:{line_number}: invalid legacy pattern line")
            offset = int(match.group(1), 16)
            value = int(match.group(2), 16)
            writes.append(validate_write(offset, value))
    if not writes:
        raise ValueError(f"No writes found in {path}")
    return writes


def load_ledger_sequence(path: Path, sequence_id: int) -> list[tuple[int, int]]:
    with path.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            record = json.loads(line)
            if (
                record.get("type") in {"sequence_intent", "sequence_result"}
                and int(record.get("sequence_id", -1)) == sequence_id
            ):
                return [
                    validate_write(int(write["offset"]), int(write["value"]))
                    for write in record["writes"]
                ]
    raise ValueError(f"Sequence {sequence_id} not found in {path}")


def detect_format(path: Path) -> str:
    with path.open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                return "ledger" if line.lstrip().startswith("{") else "legacy"
    raise ValueError(f"Input file is empty: {path}")


class ReplayRunner:
    def __init__(self, backend: PciBackend, log_path: Path):
        self.backend = backend
        self.log_path = log_path

    def replay(
        self,
        writes: list[tuple[int, int]],
        delay: float = 0.05,
        dry_run: bool = False,
    ) -> None:
        for index, (offset, value) in enumerate(writes, 1):
            record = {
                "type": "replay_intent",
                "timestamp": utc_now(),
                "index": index,
                "offset": offset,
                "value": value,
                "dry_run": dry_run,
            }
            append_jsonl(self.log_path, record)
            print(f"[{index}/{len(writes)}] offset=0x{offset:03x} value=0x{value:02x}")
            if dry_run:
                continue
            self.backend.write_byte(offset, value)
            if not self.backend.is_responsive():
                append_jsonl(
                    self.log_path,
                    {
                        "type": "replay_stopped",
                        "timestamp": utc_now(),
                        "index": index,
                        "reason": "device unresponsive",
                    },
                )
                raise RuntimeError(f"Device became unresponsive after replay write {index}")
            if delay:
                time.sleep(delay)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-d", "--device", required=True, help="Target PCI BDF")
    parser.add_argument("-i", "--input", type=Path, required=True)
    parser.add_argument("--format", choices=("auto", "ledger", "legacy"), default="auto")
    parser.add_argument("--sequence-id", type=int, help="Required for Stage 2 ledgers")
    parser.add_argument("--reverse", action="store_true")
    parser.add_argument("--delay", type=float, default=0.05)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="Skip destructive confirmation")
    parser.add_argument("--log", type=Path, default=None)
    args = parser.parse_args()
    if args.delay < 0:
        parser.error("--delay cannot be negative")
    return args


def main() -> None:
    args = parse_args()
    input_format = detect_format(args.input) if args.format == "auto" else args.format
    if input_format == "ledger":
        if args.sequence_id is None:
            raise SystemExit("--sequence-id is required for a Stage 2 ledger")
        writes = load_ledger_sequence(args.input, args.sequence_id)
    else:
        writes = load_legacy_pattern(args.input)
    if args.reverse:
        writes.reverse()

    if not args.dry_run and not args.yes:
        confirmation = input(
            f"Replay {len(writes)} destructive PCIe writes to {args.device}? Type 'replay': "
        )
        if confirmation != "replay":
            raise SystemExit("Replay cancelled")

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = args.log or Path(f"replay_{timestamp}.jsonl")
    backend = SystemPciBackend(args.device)
    ReplayRunner(backend, log_path).replay(writes, args.delay, args.dry_run)


if __name__ == "__main__":
    main()
