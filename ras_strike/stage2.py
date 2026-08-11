"""Stage 2 structure-guided multi-register sequence exploration."""

from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .backend import BackendError, PciBackend, diff_snapshots
from .capabilities import CapabilityRegion, parse_capability_regions, region_for_offset
from .durable import append_jsonl, append_text, atomic_write_json, utc_now


def _offset(value: int | str) -> int:
    return int(value, 16) if isinstance(value, str) else int(value)


def load_stage1_summary(path: Path) -> dict[str, Any]:
    """Normalize current v2 and legacy discovery JSON into a Stage 2 input."""

    with path.open(encoding="utf-8") as source:
        data = json.load(source)
    if data.get("schema_version") == 2:
        return data

    initial = data.get("initial_state", {})
    byte_range = int(data.get("config", {}).get("byte_range", 4096))
    baseline: list[int | None] = [None] * byte_range
    for key, value in initial.items():
        index = _offset(key)
        if index < byte_range:
            baseline[index] = None if value is None else _offset(value)

    summary = data.get("summary", {})
    cascading = summary.get("cascading_offsets", [])
    self_only = summary.get("writable_self_only_offsets", [])
    skip = data.get("config", {}).get("skip_offsets", [])
    catastrophic = summary.get("device_unresponsive_offsets", [])
    sensitive = data.get("interesting_offsets", [])
    return {
        "schema_version": 1,
        "device_id": data.get("device_id"),
        "config": {
            "byte_range": byte_range,
            "exclude_offsets": data.get("config", {}).get("exclude_from_diff", []),
        },
        "baseline": baseline,
        "categories": {
            "sensitive_cascading": cascading,
            "sensitive_self": self_only,
            "catastrophic": catastrophic,
            "skipped": skip,
        },
        "sensitive_offsets": sensitive,
        "anomalous_values": {},
    }


@dataclass
class Stage2Config:
    device_id: str
    iterations: int = 10000
    writes_per_sequence: int = 2
    delay: float = 0
    seed: int = 0
    exclude_offsets: tuple[int, ...] = ()


@dataclass
class Stage2Paths:
    ledger: Path
    corpus: Path
    log: Path
    summary: Path

    @classmethod
    def create(cls, output_dir: Path) -> "Stage2Paths":
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        prefix = output_dir / f"fuzz_guided_{timestamp}"
        return cls(
            ledger=prefix.with_name(f"{prefix.name}_sequences.jsonl"),
            corpus=prefix.with_name(f"{prefix.name}_corpus.jsonl"),
            log=prefix.with_suffix(".log"),
            summary=prefix.with_name(f"{prefix.name}_summary.json"),
        )


class StructureGuidedGenerator:
    def __init__(self, stage1: dict[str, Any], seed: int):
        self.stage1 = stage1
        self.random = random.Random(seed)
        self.baseline = list(stage1.get("baseline", []))
        self.regions = parse_capability_regions(self.baseline)
        categories = stage1.get("categories", {})
        self.cascading = {_offset(item) for item in categories.get("sensitive_cascading", [])}
        self.sensitive = {_offset(item) for item in stage1.get("sensitive_offsets", [])}
        self.sensitive.update(_offset(item) for item in categories.get("sensitive_self", []))
        self.sensitive.update(self.cascading)
        self.skipped = {_offset(item) for item in categories.get("skipped", [])}
        self.skipped.update(_offset(item) for item in categories.get("catastrophic", []))
        self.anomalous_values = {
            _offset(offset): [int(value) for value in values]
            for offset, values in stage1.get("anomalous_values", {}).items()
        }
        byte_range = int(stage1.get("config", {}).get("byte_range", len(self.baseline) or 4096))
        self.eligible = [offset for offset in range(byte_range) if offset not in self.skipped]

    def offset_weight(self, offset: int, previous: int | None = None) -> float:
        weight = 1.0
        if offset in self.sensitive:
            weight += 6.0
        if offset in self.cascading:
            weight += 5.0
        region = region_for_offset(self.regions, offset)
        if region.kind == "VSEC":
            weight += 5.0
        elif region.kind == "PM":
            weight += 4.0
        if previous is not None:
            previous_region = region_for_offset(self.regions, previous)
            if (region.kind, region.start) == (previous_region.kind, previous_region.start):
                weight += 5.0
            elif abs(offset - previous) <= 0x10:
                weight += 2.0
        return weight

    def _pick_offset(self, available: list[int], previous: int | None) -> int:
        weights = [self.offset_weight(offset, previous) for offset in available]
        return self.random.choices(available, weights=weights, k=1)[0]

    def _pick_value(self, offset: int) -> int:
        seeds = self.anomalous_values.get(offset, [])
        if seeds and self.random.random() < 0.7:
            return self.random.choice(seeds)
        if self.random.random() < 0.35:
            return self.random.choice([0x00, 0x01, 0x7F, 0x80, 0xFE, 0xFF])
        return self.random.randint(0, 0xFF)

    def sequence(self, length: int) -> list[tuple[int, int]]:
        if not self.eligible:
            raise ValueError("Stage 1 summary contains no eligible offsets")
        available = list(self.eligible)
        writes: list[tuple[int, int]] = []
        previous: int | None = None
        for _ in range(min(length, len(available))):
            offset = self._pick_offset(available, previous)
            available.remove(offset)
            writes.append((offset, self._pick_value(offset)))
            previous = offset
        return writes


def _state_hash(snapshot: dict[int, int | None]) -> str:
    serialized = bytes(
        0 if snapshot.get(offset) is None else int(snapshot[offset])
        for offset in sorted(snapshot)
    )
    return hashlib.sha256(serialized).hexdigest()


class Stage2Runner:
    def __init__(
        self,
        backend: PciBackend,
        config: Stage2Config,
        stage1: dict[str, Any],
        paths: Stage2Paths,
        verbose: bool = False,
    ):
        self.backend = backend
        self.config = config
        self.stage1 = stage1
        self.paths = paths
        self.generator = StructureGuidedGenerator(stage1, config.seed)
        self.verbose = verbose
        self.started_at = time.monotonic()
        self.completed_iterations = 0
        self.corpus_size = 0
        self.crash_sequence: int | None = None
        self.seen_states: set[str] = set()
        self.current_sequence: dict[str, Any] | None = None

    def _log(self, message: str) -> None:
        append_text(self.paths.log, message)
        print(self._display(message), flush=self.verbose)

    def _verbose(self, message: str) -> None:
        if self.verbose:
            print(self._display(message), flush=True)

    def _display(self, message: str) -> str:
        if not self.verbose:
            return message
        return f"[+{time.monotonic() - self.started_at:012.6f}] {message}"

    def _ensure_output_files(self) -> None:
        for path in (self.paths.ledger, self.paths.corpus, self.paths.log):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)

    def _build_summary(self, completed: bool, stop_reason: str) -> dict[str, Any]:
        coverage = self.stage1.get("coverage")
        if coverage is None:
            coverage = {
                "configured_byte_range": int(
                    self.stage1.get("config", {}).get(
                        "byte_range",
                        len(self.stage1.get("baseline", [])),
                    )
                ),
                "tested_offsets": self.stage1.get("sensitive_offsets", []),
                "coverage_metadata_available": False,
            }
        return {
            "schema_version": 2,
            "stage": 2,
            "device_id": self.config.device_id,
            "seed": self.config.seed,
            "iterations_requested": self.config.iterations,
            "iterations_completed": self.completed_iterations,
            "writes_per_sequence": self.config.writes_per_sequence,
            "unique_states": len(self.seen_states),
            "corpus_size": self.corpus_size,
            "crash_sequence": self.crash_sequence,
            "completed": completed,
            "stop_reason": stop_reason,
            "ledger": str(self.paths.ledger),
            "corpus": str(self.paths.corpus),
            "stage1_completed": bool(self.stage1.get("completed", True)),
            "stage1_stop_reason": self.stage1.get("stop_reason"),
            "stage1_coverage": coverage,
            "capability_regions": [
                region.serialized() for region in self.generator.regions
            ],
        }

    def _write_summary(self, completed: bool, stop_reason: str) -> dict[str, Any]:
        summary = self._build_summary(completed, stop_reason)
        atomic_write_json(self.paths.summary, summary)
        return summary

    def _finalize_interrupted(self) -> dict[str, Any]:
        if self.current_sequence is not None:
            append_jsonl(
                self.paths.ledger,
                {
                    "type": "sequence_interrupted",
                    "timestamp": utc_now(),
                    **self.current_sequence,
                    "reason": "run interrupted before sequence completion",
                },
            )
        summary = self._write_summary(False, "interrupted")
        self._log(f"Stage 2 interrupted; partial summary written to {self.paths.summary}")
        return summary

    def run(self) -> dict[str, Any]:
        self._ensure_output_files()
        try:
            return self._run()
        except KeyboardInterrupt:
            self._finalize_interrupted()
            raise

    def _run(self) -> dict[str, Any]:
        if not self.backend.is_responsive():
            raise BackendError(f"Device {self.config.device_id} is not responsive")

        size = int(self.stage1.get("config", {}).get("byte_range", 4096))
        self.seen_states = {_state_hash(self.backend.snapshot(size))}
        self._log(
            f"Stage 2 starting: device={self.config.device_id}, seed={self.config.seed}, "
            f"iterations={self.config.iterations}"
        )

        for sequence_id in range(1, self.config.iterations + 1):
            writes = self.generator.sequence(self.config.writes_per_sequence)
            serialized_writes = [
                {"offset": offset, "value": value} for offset, value in writes
            ]
            before = self.backend.snapshot(size)
            append_jsonl(
                self.paths.ledger,
                {
                    "type": "sequence_intent",
                    "timestamp": utc_now(),
                    "sequence_id": sequence_id,
                    "seed": self.config.seed,
                    "writes": serialized_writes,
                },
            )
            self.current_sequence = {
                "sequence_id": sequence_id,
                "seed": self.config.seed,
                "writes": serialized_writes,
            }
            self._verbose(
                f"sequence {sequence_id}/{self.config.iterations}: "
                f"{len(writes)} writes"
            )

            try:
                for write_index, (offset, value) in enumerate(writes, start=1):
                    self._verbose(
                        f"sequence {sequence_id} write {write_index}/{len(writes)}: "
                        f"offset=0x{offset:03x} value=0x{value:02x}"
                    )
                    self.backend.write_byte(offset, value)
                    if self.config.delay:
                        time.sleep(self.config.delay)
            except BackendError as exc:
                self._verbose(f"sequence {sequence_id} write failed: {exc}")
                append_jsonl(
                    self.paths.ledger,
                    {
                        "type": "sequence_error",
                        "timestamp": utc_now(),
                        "sequence_id": sequence_id,
                        "writes": serialized_writes,
                        "error": str(exc),
                    },
                )
                raise

            alive = self.backend.is_responsive()
            after = self.backend.snapshot(size) if alive else {}
            changes = (
                diff_snapshots(
                    before,
                    after,
                    writes[-1][0],
                    set(self.config.exclude_offsets),
                )["changes"]
                if alive
                else []
            )
            state_hash = _state_hash(after) if alive else None
            is_new_state = bool(alive and state_hash not in self.seen_states)
            if state_hash:
                self.seen_states.add(state_hash)
            regions = [
                region_for_offset(self.generator.regions, offset).kind
                for offset, _ in writes
            ]
            append_jsonl(
                self.paths.ledger,
                {
                    "type": "sequence_result",
                    "timestamp": utc_now(),
                    "sequence_id": sequence_id,
                    "seed": self.config.seed,
                    "writes": serialized_writes,
                    "regions": regions,
                    "changes": changes,
                    "state_hash": state_hash,
                    "new_state": is_new_state,
                    "device_alive": alive,
                },
            )
            self._verbose(
                f"sequence {sequence_id} result: device_alive={alive} "
                f"new_state={is_new_state} changes={len(changes)}"
            )
            if is_new_state:
                append_jsonl(
                    self.paths.corpus,
                    {
                        "sequence_id": sequence_id,
                        "writes": serialized_writes,
                        "changes": changes,
                        "state_hash": state_hash,
                    },
                )
                self.corpus_size += 1
            self.completed_iterations = sequence_id
            self.current_sequence = None
            if not alive:
                self.crash_sequence = sequence_id
                self._log(f"Device unresponsive after sequence {sequence_id}")
                break
            if sequence_id % 100 == 0:
                self._log(
                    f"Progress {sequence_id}/{self.config.iterations}; "
                    f"unique states={len(self.seen_states)}"
                )

        completed = self.completed_iterations == self.config.iterations
        stop_reason = "iterations_complete" if completed else "device_unresponsive"
        summary = self._write_summary(completed, stop_reason)
        self._log(f"Stage 2 summary written to {self.paths.summary}")
        return summary
