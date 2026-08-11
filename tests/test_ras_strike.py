from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from replay_sequence import ReplayRunner, load_ledger_sequence, load_legacy_pattern
from ras_strike.backend import diff_snapshots
from ras_strike.capabilities import parse_capability_regions
from ras_strike.durable import atomic_write_json
from ras_strike.stage1 import Stage1Config, Stage1Paths, Stage1Runner
from ras_strike.stage2 import (
    Stage2Config,
    Stage2Paths,
    Stage2Runner,
    StructureGuidedGenerator,
    load_stage1_summary,
)


class FakeBackend:
    def __init__(self, size: int = 512):
        self.device_id = "00:01.0"
        self.memory = bytearray(size)
        self.writes: list[tuple[int, int]] = []
        self.side_effects: dict[tuple[int, int], tuple[int, int]] = {}
        self.responsive = True
        self.reset_count = 0
        self.interrupt_after_writes: int | None = None

    def read_byte(self, offset: int) -> int | None:
        return self.memory[offset] if self.responsive else None

    def write_byte(self, offset: int, value: int) -> None:
        self.writes.append((offset, value))
        self.memory[offset] = value
        effect = self.side_effects.get((offset, value))
        if effect:
            self.memory[effect[0]] = effect[1]
        if len(self.writes) == self.interrupt_after_writes:
            raise KeyboardInterrupt

    def snapshot(self, size: int) -> dict[int, int | None]:
        return {
            offset: self.memory[offset] if self.responsive else None
            for offset in range(size)
        }

    def is_responsive(self) -> bool:
        return self.responsive

    def reset(self) -> bool:
        self.reset_count += 1
        return True


class SnapshotTests(unittest.TestCase):
    def test_diff_tracks_secondary_and_excluded_changes(self) -> None:
        diff = diff_snapshots(
            {0: 1, 1: 2, 2: 3},
            {0: 1, 1: 9, 2: 8},
            written_offset=1,
            excluded_offsets={2},
        )
        self.assertTrue(diff["written_offset_changed"])
        self.assertFalse(diff["other_offsets_changed"])
        self.assertEqual([2], [item["offset"] for item in diff["excluded_changes"]])


class Stage1Tests(unittest.TestCase):
    def test_runtime_defaults_have_no_forced_delay(self) -> None:
        self.assertEqual(0, Stage1Config("00:01.0").delay)
        self.assertEqual(0, Stage2Config("00:01.0").delay)

    def test_fast_mode_detects_cascading_and_honors_skip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend(8)
            backend.side_effects[(1, 0xFF)] = (2, 0xAA)
            config = Stage1Config(
                device_id=backend.device_id,
                byte_range=4,
                start_offset=1,
                skip_offsets=(3,),
                mode="fast",
                delay=0,
                reset_at_end=False,
            )
            paths = Stage1Paths.create(Path(directory), "fast")
            summary = Stage1Runner(backend, config, paths).run()
            self.assertIn("0x001", summary["categories"]["sensitive_cascading"])
            self.assertIn("0x003", summary["categories"]["skipped"])
            self.assertNotIn((3, 0xFF), backend.writes)
            self.assertEqual([0xFF], summary["anomalous_values"]["0x001"])

    def test_exhaustive_mode_tests_all_byte_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend(4)
            config = Stage1Config(
                device_id=backend.device_id,
                byte_range=1,
                mode="exhaustive",
                delay=0,
                reset_at_end=False,
            )
            paths = Stage1Paths.create(Path(directory), "full")
            summary = Stage1Runner(backend, config, paths).run()
            self.assertEqual(256, summary["result_count"])
            self.assertEqual(list(range(256)), [value for _, value in backend.writes])

    def test_resume_skips_unfinished_offset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend(8)
            config = Stage1Config(device_id=backend.device_id, byte_range=8, mode="fast")
            paths = Stage1Paths.create(Path(directory), "resume")
            runner = Stage1Runner(backend, config, paths)
            state = runner.state
            state["in_flight"] = {"offset": 4, "value": 0xFF}
            atomic_write_json(paths.checkpoint, state)
            resumed = Stage1Runner.resume(backend, paths.checkpoint)
            self.assertIn(4, resumed.config.skip_offsets)
            self.assertEqual({"offset": 5, "value_index": 0}, resumed.state["cursor"])
            self.assertIsNone(resumed.state["in_flight"])

    def test_verbose_prints_write_result_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend(1)
            config = Stage1Config(
                device_id=backend.device_id,
                byte_range=1,
                mode="fast",
                delay=0,
                restore=True,
                reset_at_end=False,
            )
            paths = Stage1Paths.create(Path(directory), "verbose")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                Stage1Runner(backend, config, paths, verbose=True).run()

            text = output.getvalue()
            self.assertRegex(
                text,
                r"\[\+\d+\.\d{6}\] write offset=0x000 value=0xff",
            )
            self.assertRegex(
                text,
                r"\[\+\d+\.\d{6}\] result offset=0x000 value=0xff "
                r"category=sensitive_self",
            )
            self.assertRegex(
                text,
                r"\[\+\d+\.\d{6}\] restore offset=0x000 value=0x00",
            )
            self.assertEqual([(0, 0xFF), (0, 0x00)], backend.writes)

    def test_max_tests_bounds_run_and_records_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend(8)
            config = Stage1Config(
                device_id=backend.device_id,
                byte_range=8,
                mode="fast",
                reset_at_end=False,
                max_tests=3,
            )
            paths = Stage1Paths.create(Path(directory), "bounded")
            summary = Stage1Runner(backend, config, paths).run()

            self.assertEqual([(0, 0xFF), (1, 0xFF), (2, 0xFF)], backend.writes)
            self.assertTrue(summary["completed"])
            self.assertEqual("max_tests_reached", summary["stop_reason"])
            self.assertEqual(3, summary["result_count"])
            self.assertEqual(
                ["0x000", "0x001", "0x002"],
                summary["coverage"]["tested_offsets"],
            )
            self.assertFalse(summary["coverage"]["range_traversal_complete"])
            self.assertEqual(8, len(summary["baseline"]))

    def test_interrupt_writes_partial_summary_and_can_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend(8)
            backend.interrupt_after_writes = 1
            config = Stage1Config(
                device_id=backend.device_id,
                byte_range=8,
                mode="fast",
                reset_at_end=False,
                max_tests=3,
            )
            paths = Stage1Paths.create(Path(directory), "interrupted")

            with self.assertRaises(KeyboardInterrupt):
                Stage1Runner(backend, config, paths).run()

            partial = json.loads(paths.summary.read_text())
            checkpoint = json.loads(paths.checkpoint.read_text())
            self.assertFalse(partial["completed"])
            self.assertEqual("interrupted", partial["stop_reason"])
            self.assertEqual(0, partial["result_count"])
            self.assertEqual(0, partial["unresolved_writes"][0]["offset"])
            self.assertEqual("interrupted", checkpoint["stop_reason"])
            self.assertTrue(paths.events.exists())
            self.assertTrue(paths.log.exists())

            backend.interrupt_after_writes = None
            resumed = Stage1Runner.resume(backend, paths.checkpoint)
            final = resumed.run()
            self.assertTrue(final["completed"])
            self.assertEqual("max_tests_reached", final["stop_reason"])
            self.assertEqual(3, final["result_count"])
            self.assertIn("0x000", final["categories"]["skipped"])


class CapabilityAndStage2Tests(unittest.TestCase):
    def test_capability_parser_finds_pm_and_vsec(self) -> None:
        snapshot: list[int | None] = [0] * 512
        snapshot[0x34] = 0x40
        snapshot[0x40] = 0x01
        snapshot[0x41] = 0
        header = 0x000B | (1 << 16)
        for index in range(4):
            snapshot[0x100 + index] = (header >> (index * 8)) & 0xFF
        kinds = {region.kind for region in parse_capability_regions(snapshot)}
        self.assertIn("PM", kinds)
        self.assertIn("VSEC", kinds)

    def test_generator_is_deterministic_weighted_and_skips_catastrophic(self) -> None:
        summary = {
            "schema_version": 2,
            "config": {"byte_range": 32},
            "baseline": [0] * 32,
            "categories": {
                "sensitive_cascading": ["0x004"],
                "sensitive_self": ["0x005"],
                "catastrophic": ["0x006"],
                "skipped": ["0x007"],
            },
            "sensitive_offsets": ["0x004", "0x005"],
            "anomalous_values": {"0x004": [0xA5]},
        }
        first = StructureGuidedGenerator(summary, 123)
        second = StructureGuidedGenerator(summary, 123)
        self.assertEqual(first.sequence(4), second.sequence(4))
        self.assertNotIn(6, first.eligible)
        self.assertNotIn(7, first.eligible)
        self.assertGreater(first.offset_weight(4), first.offset_weight(10))
        values = {first._pick_value(4) for _ in range(30)}
        self.assertIn(0xA5, values)

    def test_v1_summary_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            path.write_text(
                json.dumps(
                    {
                        "device_id": "00:01.0",
                        "config": {"byte_range": 4, "skip_offsets": ["0x003"]},
                        "initial_state": {"0x000": "0x00"},
                        "interesting_offsets": ["0x001"],
                        "summary": {
                            "cascading_offsets": ["0x001"],
                            "writable_self_only_offsets": [],
                            "device_unresponsive_offsets": ["0x002"],
                        },
                    }
                )
            )
            normalized = load_stage1_summary(path)
            self.assertEqual(["0x001"], normalized["sensitive_offsets"])
            self.assertEqual(["0x002"], normalized["categories"]["catastrophic"])

    def test_stage2_runner_writes_complete_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend(32)
            summary = {
                "schema_version": 2,
                "device_id": backend.device_id,
                "config": {"byte_range": 32},
                "baseline": [0] * 32,
                "categories": {
                    "sensitive_cascading": ["0x004"],
                    "sensitive_self": ["0x005"],
                    "catastrophic": [],
                    "skipped": [],
                },
                "sensitive_offsets": ["0x004", "0x005"],
                "anomalous_values": {"0x004": [0xA5]},
            }
            paths = Stage2Paths.create(Path(directory))
            result = Stage2Runner(
                backend,
                Stage2Config(backend.device_id, iterations=3, writes_per_sequence=2, delay=0, seed=9),
                summary,
                paths,
            ).run()
            records = [json.loads(line) for line in paths.ledger.read_text().splitlines()]
            self.assertEqual(3, result["iterations_completed"])
            self.assertEqual(3, len([item for item in records if item["type"] == "sequence_intent"]))
            self.assertEqual(3, len([item for item in records if item["type"] == "sequence_result"]))
            self.assertGreater(result["corpus_size"], 0)
            self.assertEqual(
                result["corpus_size"],
                len(paths.corpus.read_text().splitlines()),
            )

    def test_stage2_verbose_prints_each_write_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend(8)
            summary = {
                "schema_version": 2,
                "device_id": backend.device_id,
                "config": {"byte_range": 8},
                "baseline": [0] * 8,
                "categories": {},
                "sensitive_offsets": [],
                "anomalous_values": {},
            }
            paths = Stage2Paths.create(Path(directory))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                Stage2Runner(
                    backend,
                    Stage2Config(
                        backend.device_id,
                        iterations=1,
                        writes_per_sequence=2,
                        delay=0,
                        seed=9,
                    ),
                    summary,
                    paths,
                    verbose=True,
                ).run()

            text = output.getvalue()
            self.assertRegex(
                text,
                r"\[\+\d+\.\d{6}\] sequence 1/1: 2 writes",
            )
            for write_index, (offset, value) in enumerate(backend.writes, start=1):
                self.assertRegex(
                    text,
                    r"\[\+\d+\.\d{6}\] "
                    + f"sequence 1 write {write_index}/2: "
                    + f"offset=0x{offset:03x} value=0x{value:02x}",
                )
            self.assertRegex(
                text,
                r"\[\+\d+\.\d{6}\] sequence 1 result:",
            )

    def test_stage2_interrupt_writes_all_partial_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend(8)
            backend.interrupt_after_writes = 1
            summary = {
                "schema_version": 2,
                "device_id": backend.device_id,
                "completed": False,
                "stop_reason": "interrupted",
                "coverage": {
                    "configured_byte_range": 8,
                    "tested_offsets": ["0x000"],
                    "range_traversal_complete": False,
                },
                "config": {"byte_range": 8},
                "baseline": [0] * 8,
                "categories": {},
                "sensitive_offsets": [],
                "anomalous_values": {},
            }
            paths = Stage2Paths.create(Path(directory))
            runner = Stage2Runner(
                backend,
                Stage2Config(
                    backend.device_id,
                    iterations=5,
                    writes_per_sequence=2,
                    seed=9,
                ),
                summary,
                paths,
            )

            with self.assertRaises(KeyboardInterrupt):
                runner.run()

            result = json.loads(paths.summary.read_text())
            records = [json.loads(line) for line in paths.ledger.read_text().splitlines()]
            self.assertFalse(result["completed"])
            self.assertEqual("interrupted", result["stop_reason"])
            self.assertEqual(0, result["iterations_completed"])
            self.assertFalse(result["stage1_completed"])
            self.assertEqual(summary["coverage"], result["stage1_coverage"])
            self.assertEqual(
                ["sequence_intent", "sequence_interrupted"],
                [record["type"] for record in records],
            )
            self.assertTrue(paths.corpus.exists())
            self.assertEqual("", paths.corpus.read_text())
            self.assertTrue(paths.log.exists())


class ReplayTests(unittest.TestCase):
    def test_legacy_and_ledger_parsers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "pattern"
            legacy.write_text("0000:00:01.0 @100 ff\n0000:00:01.0 @104 01\n")
            self.assertEqual([(0x100, 0xFF), (0x104, 0x01)], load_legacy_pattern(legacy))

            ledger = root / "ledger.jsonl"
            ledger.write_text(
                json.dumps(
                    {
                        "type": "sequence_result",
                        "sequence_id": 9,
                        "writes": [
                            {"offset": 0x10, "value": 0x20},
                            {"offset": 0x11, "value": 0x21},
                        ],
                    }
                )
                + "\n"
            )
            self.assertEqual([(0x10, 0x20), (0x11, 0x21)], load_ledger_sequence(ledger, 9))

    def test_replay_order_reverse_and_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            backend = FakeBackend(512)
            runner = ReplayRunner(backend, Path(directory) / "replay.jsonl")
            writes = [(0x10, 1), (0x11, 2)]
            runner.replay(writes, delay=0)
            self.assertEqual(writes, backend.writes)

            backend.writes.clear()
            runner.replay(list(reversed(writes)), delay=0)
            self.assertEqual(list(reversed(writes)), backend.writes)

            backend.writes.clear()
            runner.replay(writes, delay=0, dry_run=True)
            self.assertEqual([], backend.writes)


if __name__ == "__main__":
    unittest.main()
