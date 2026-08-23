"""Stage 1 exhaustive single-offset behavioral mapping."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .backend import BackendError, PciBackend, diff_snapshots
from .durable import append_jsonl, append_text, atomic_write_json, load_jsonl, utc_now


@dataclass
class Stage1Config:
    device_id: str
    byte_range: int = 4096
    start_offset: int = 0
    skip_offsets: tuple[int, ...] = ()
    exclude_offsets: tuple[int, ...] = ()
    mode: str = "exhaustive"
    value: int = 0xFF
    delay: float = 0
    restore: bool = False
    reset_at_end: bool = False
    max_tests: int | None = None

    def values(self) -> list[int]:
        return list(range(256)) if self.mode == "exhaustive" else [self.value]


@dataclass
class Stage1Paths:
    summary: Path
    events: Path
    checkpoint: Path
    log: Path

    @classmethod
    def create(cls, output_dir: Path, timestamp: str | None = None) -> "Stage1Paths":
        timestamp = timestamp or datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        prefix = output_dir / f"discover_offsets_{timestamp}"
        return cls(
            summary=prefix.with_suffix(".json"),
            events=prefix.with_name(f"{prefix.name}_events.jsonl"),
            checkpoint=prefix.with_name(f"{prefix.name}_checkpoint.json"),
            log=prefix.with_suffix(".log"),
        )

    @classmethod
    def from_checkpoint(cls, checkpoint: Path, state: dict[str, Any]) -> "Stage1Paths":
        paths = state["paths"]
        return cls(
            summary=Path(paths["summary"]),
            events=Path(paths["events"]),
            checkpoint=checkpoint,
            log=Path(paths["log"]),
        )

    def serialized(self) -> dict[str, str]:
        return {
            "summary": str(self.summary),
            "events": str(self.events),
            "checkpoint": str(self.checkpoint),
            "log": str(self.log),
        }


class Stage1Runner:
    def __init__(
        self,
        backend: PciBackend,
        config: Stage1Config,
        paths: Stage1Paths,
        checkpoint_state: dict[str, Any] | None = None,
        verbose: bool = False,
    ):
        self.backend = backend
        self.config = config
        self.paths = paths
        self.state = checkpoint_state or self._initial_state()
        self.verbose = verbose
        self.started_at = time.monotonic()

    def _initial_state(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "stage": 1,
            "config": asdict(self.config),
            "paths": self.paths.serialized(),
            "cursor": {"offset": self.config.start_offset, "value_index": 0},
            "tests_completed": 0,
            "in_flight": None,
            "unresolved": [],
            "completed": False,
            "stop_reason": None,
        }

    @classmethod
    def resume(
        cls,
        backend: PciBackend,
        checkpoint: Path,
        verbose: bool = False,
    ) -> "Stage1Runner":
        import json

        with checkpoint.open(encoding="utf-8") as source:
            state = json.load(source)
        config_data = state["config"]
        config_data["skip_offsets"] = tuple(config_data.get("skip_offsets", ()))
        config_data["exclude_offsets"] = tuple(config_data.get("exclude_offsets", ()))
        config = Stage1Config(**config_data)
        paths = Stage1Paths.from_checkpoint(checkpoint, state)
        recorded_results = sum(
            1 for record in load_jsonl(paths.events) if record.get("type") == "result"
        )
        state["tests_completed"] = max(
            int(state.get("tests_completed", 0)),
            recorded_results,
        )
        state.setdefault("stop_reason", None)
        runner = cls(backend, config, paths, state, verbose=verbose)
        if runner.state.get("in_flight"):
            runner._resolve_in_flight("run resumed with an unfinished write")
            runner._checkpoint()
        return runner

    def _checkpoint(self) -> None:
        self.state["config"] = asdict(self.config)
        self.state["paths"] = self.paths.serialized()
        atomic_write_json(self.paths.checkpoint, self.state)

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
        for path in (self.paths.events, self.paths.log):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)

    def _resolve_in_flight(self, reason: str) -> None:
        in_flight = self.state.get("in_flight")
        if not in_flight:
            return
        unresolved = self.state.setdefault("unresolved", [])
        if in_flight not in unresolved:
            unresolved.append(in_flight)
        skipped = set(self.config.skip_offsets)
        skipped.add(int(in_flight["offset"]))
        self.config.skip_offsets = tuple(sorted(skipped))
        self.state["cursor"] = {"offset": int(in_flight["offset"]) + 1, "value_index": 0}
        self.state["in_flight"] = None
        append_jsonl(
            self.paths.events,
            {
                "type": "unresolved",
                "timestamp": utc_now(),
                "offset": int(in_flight["offset"]),
                "value": int(in_flight["value"]),
                "reason": reason,
            },
        )

    def _finalize_interrupted(self) -> dict[str, Any]:
        self._resolve_in_flight("run interrupted with an unfinished write")
        self.state["completed"] = False
        self.state["stop_reason"] = "interrupted"
        self._checkpoint()
        summary = self._build_summary()
        atomic_write_json(self.paths.summary, summary)
        self._log(f"Stage 1 interrupted; partial summary written to {self.paths.summary}")
        return summary

    def run(self) -> dict[str, Any]:
        self._ensure_output_files()
        try:
            return self._run()
        except KeyboardInterrupt:
            self._finalize_interrupted()
            raise

    def _run(self) -> dict[str, Any]:
        if self.state.get("completed"):
            return self._build_summary()
        if not self.backend.is_responsive():
            raise BackendError(f"Device {self.config.device_id} is not responsive")

        baseline = self.backend.snapshot(self.config.byte_range)
        self.state["baseline"] = [baseline.get(index) for index in range(self.config.byte_range)]
        self._checkpoint()
        self._log(
            f"Stage 1 starting: device={self.config.device_id}, mode={self.config.mode}, "
            f"range={self.config.byte_range}"
        )

        values = self.config.values()
        excluded = set(self.config.exclude_offsets)
        skipped = set(self.config.skip_offsets)
        cursor = self.state.get("cursor", {})
        first_offset = int(cursor.get("offset", self.config.start_offset))
        first_value_index = int(cursor.get("value_index", 0))
        tests_completed = int(self.state.get("tests_completed", 0))
        stop_after_failure = False
        limit_reached = False

        for offset in range(first_offset, self.config.byte_range):
            if offset in skipped:
                continue
            value_start = first_value_index if offset == first_offset else 0
            for value_index in range(value_start, len(values)):
                if (
                    self.config.max_tests is not None
                    and tests_completed >= self.config.max_tests
                ):
                    limit_reached = True
                    break
                value = values[value_index]
                before = self.backend.snapshot(self.config.byte_range)
                original = before.get(offset)
                intent = {
                    "type": "intent",
                    "timestamp": utc_now(),
                    "offset": offset,
                    "value": value,
                }
                self.state["in_flight"] = {"offset": offset, "value": value}
                self._checkpoint()
                append_jsonl(self.paths.events, intent)
                append_text(
                    self.paths.log,
                    f"INTENT offset=0x{offset:03x} value=0x{value:02x}",
                )
                original_display = "unreadable" if original is None else f"0x{original:02x}"
                self._verbose(
                    f"write offset=0x{offset:03x} value=0x{value:02x} "
                    f"(previous={original_display})"
                )

                try:
                    self.backend.write_byte(offset, value)
                except (BackendError, ValueError) as exc:
                    self._verbose(
                        f"write failed offset=0x{offset:03x} value=0x{value:02x}: {exc}"
                    )
                    append_jsonl(
                        self.paths.events,
                        {
                            "type": "access_error",
                            "timestamp": utc_now(),
                            "offset": offset,
                            "value": value,
                            "error": str(exc),
                        },
                    )
                    self.state["in_flight"] = None
                    self._checkpoint()
                    raise

                if self.config.delay:
                    import time

                    time.sleep(self.config.delay)

                if not self.backend.is_responsive():
                    append_jsonl(
                        self.paths.events,
                        {
                            "type": "result",
                            "timestamp": utc_now(),
                            "offset": offset,
                            "value": value,
                            "category": "catastrophic",
                            "changes": [],
                        },
                    )
                    skipped.add(offset)
                    self.config.skip_offsets = tuple(sorted(skipped))
                    self.state["in_flight"] = None
                    self.state["cursor"] = {"offset": offset + 1, "value_index": 0}
                    self._checkpoint()
                    self._verbose(
                        f"result offset=0x{offset:03x} value=0x{value:02x} "
                        "category=catastrophic"
                    )
                    self._log(f"Device became unresponsive after offset=0x{offset:03x}")
                    stop_after_failure = True
                    break

                after = self.backend.snapshot(self.config.byte_range)
                diff = diff_snapshots(before, after, offset, excluded)
                if diff["other_offsets_changed"]:
                    category = "sensitive_cascading"
                elif diff["written_offset_changed"]:
                    category = "sensitive_self"
                else:
                    category = "inert"

                append_jsonl(
                    self.paths.events,
                    {
                        "type": "result",
                        "timestamp": utc_now(),
                        "offset": offset,
                        "value": value,
                        "original_value": original,
                        "category": category,
                        "changes": diff["changes"],
                        "excluded_changes": diff["excluded_changes"],
                    },
                )
                self._verbose(
                    f"result offset=0x{offset:03x} value=0x{value:02x} "
                    f"category={category} changes={len(diff['changes'])}"
                )

                if self.config.restore and original is not None:
                    self._verbose(
                        f"restore offset=0x{offset:03x} value=0x{original:02x}"
                    )
                    self.backend.write_byte(offset, original)

                if value_index + 1 < len(values):
                    next_cursor = {"offset": offset, "value_index": value_index + 1}
                else:
                    next_cursor = {"offset": offset + 1, "value_index": 0}
                self.state["in_flight"] = None
                self.state["cursor"] = next_cursor
                tests_completed += 1
                self.state["tests_completed"] = tests_completed
                self._checkpoint()

            if stop_after_failure or limit_reached:
                break

        self.state["completed"] = not stop_after_failure
        if stop_after_failure:
            self.state["stop_reason"] = "device_unresponsive"
        elif limit_reached:
            self.state["stop_reason"] = "max_tests_reached"
        else:
            self.state["stop_reason"] = "range_complete"
        self.state["in_flight"] = None
        self._checkpoint()
        summary = self._build_summary()
        atomic_write_json(self.paths.summary, summary)

        if self.config.reset_at_end and self.backend.is_responsive():
            try:
                self.backend.reset()
            except BackendError as exc:
                self._log(f"Device reset failed: {exc}")
        self._log(f"Stage 1 summary written to {self.paths.summary}")
        return summary

    def _build_summary(self) -> dict[str, Any]:
        categories: dict[str, set[int]] = {
            "inert": set(),
            "sensitive_self": set(),
            "sensitive_cascading": set(),
            "catastrophic": set(),
            "skipped": set(self.config.skip_offsets),
        }
        anomalous_values: dict[str, set[int]] = {}
        tested_offsets: set[int] = set()
        result_count = 0
        for record in load_jsonl(self.paths.events):
            if record.get("type") == "result":
                result_count += 1
                category = str(record["category"])
                offset = int(record["offset"])
                tested_offsets.add(offset)
                categories.setdefault(category, set()).add(offset)
                if category == "sensitive_cascading":
                    anomalous_values.setdefault(f"0x{offset:03x}", set()).add(int(record["value"]))
            elif record.get("type") == "unresolved":
                categories["catastrophic"].add(int(record["offset"]))

        sensitive = categories["sensitive_self"] | categories["sensitive_cascading"]
        unresolved = self.state.get("unresolved", [])
        baseline = self.state.get("baseline", [])
        return {
            "schema_version": 2,
            "stage": 1,
            "device_id": self.config.device_id,
            "mode": self.config.mode,
            "config": {
                **asdict(self.config),
                "skip_offsets": [f"0x{item:03x}" for item in self.config.skip_offsets],
                "exclude_offsets": [f"0x{item:03x}" for item in self.config.exclude_offsets],
            },
            "baseline": baseline,
            "categories": {
                name: [f"0x{item:03x}" for item in sorted(offsets)]
                for name, offsets in categories.items()
            },
            "sensitive_offsets": [f"0x{item:03x}" for item in sorted(sensitive)],
            "anomalous_values": {
                offset: sorted(values) for offset, values in anomalous_values.items()
            },
            "unresolved_writes": unresolved,
            "events_file": str(self.paths.events),
            "checkpoint_file": str(self.paths.checkpoint),
            "result_count": result_count,
            "completed": bool(self.state.get("completed")),
            "stop_reason": self.state.get("stop_reason"),
            "coverage": {
                "configured_byte_range": self.config.byte_range,
                "start_offset": self.config.start_offset,
                "max_tests": self.config.max_tests,
                "tests_completed": result_count,
                "tested_offset_count": len(tested_offsets),
                "tested_offsets": [
                    f"0x{item:03x}" for item in sorted(tested_offsets)
                ],
                "range_traversal_complete": self.state.get("stop_reason")
                == "range_complete",
            },
            "interesting_offsets": [f"0x{item:03x}" for item in sorted(sensitive)],
            "interesting_offsets_decimal": sorted(sensitive),
        }
