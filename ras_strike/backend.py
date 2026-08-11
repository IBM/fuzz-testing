"""PCI configuration-space access backends."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Protocol


BDF_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{4}:)?[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$")


class BackendError(RuntimeError):
    """Raised when a PCI access command fails."""


class PciBackend(Protocol):
    """Interface used by both stages and by non-hardware test doubles."""

    device_id: str

    def read_byte(self, offset: int) -> int | None: ...

    def write_byte(self, offset: int, value: int) -> None: ...

    def snapshot(self, size: int) -> dict[int, int | None]: ...

    def is_responsive(self) -> bool: ...

    def reset(self) -> bool: ...


def normalize_bdf(device_id: str) -> str:
    if not BDF_PATTERN.fullmatch(device_id):
        raise ValueError(f"Invalid PCI BDF: {device_id!r}")
    return device_id.lower()


class SystemPciBackend:
    """Linux backend implemented with pciutils and sysfs."""

    def __init__(self, device_id: str):
        self.device_id = normalize_bdf(device_id)
        self._prefix = [] if os.geteuid() == 0 else ["sudo"]

    def _run(self, command: list[str]) -> str:
        try:
            completed = subprocess.run(
                [*self._prefix, *command],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            stderr = getattr(exc, "stderr", "") or ""
            raise BackendError(f"{' '.join(command)} failed: {stderr.strip()}") from exc
        return completed.stdout

    def read_byte(self, offset: int) -> int | None:
        try:
            output = self._run(["setpci", "-s", self.device_id, f"{offset:x}.B"])
            return int(output.strip(), 16)
        except (BackendError, ValueError):
            return None

    def write_byte(self, offset: int, value: int) -> None:
        if not 0 <= value <= 0xFF:
            raise ValueError(f"Byte value out of range: {value}")
        self._run(["setpci", "-s", self.device_id, f"{offset:x}.B={value:02x}"])

    def snapshot(self, size: int) -> dict[int, int | None]:
        flag = "-xxxx" if size > 256 else "-xxx"
        try:
            output = self._run(["lspci", "-s", self.device_id, flag])
        except BackendError:
            output = ""

        snapshot: dict[int, int | None] = {}
        for raw_line in output.splitlines():
            line = raw_line.strip()
            match = re.match(r"^([0-9a-fA-F]{2,3}):\s+(.+)$", line)
            if not match:
                continue
            base = int(match.group(1), 16)
            for index, token in enumerate(match.group(2).split()):
                if re.fullmatch(r"[0-9a-fA-F]{2}", token):
                    offset = base + index
                    if offset < size:
                        snapshot[offset] = int(token, 16)

        for offset in range(size):
            if offset not in snapshot:
                snapshot[offset] = self.read_byte(offset)
        return snapshot

    def is_responsive(self) -> bool:
        low = self.read_byte(0)
        high = self.read_byte(1)
        if low is None or high is None:
            return False
        return not (low == 0xFF and high == 0xFF)

    def reset(self) -> bool:
        sysfs_id = self.device_id if self.device_id.startswith("0000:") else f"0000:{self.device_id}"
        reset_path = Path("/sys/bus/pci/devices") / sysfs_id / "reset"
        if not reset_path.exists():
            return False
        try:
            reset_path.write_text("1\n")
        except OSError as exc:
            raise BackendError(f"Unable to reset {self.device_id}: {exc}") from exc
        return True


def diff_snapshots(
    before: dict[int, int | None],
    after: dict[int, int | None],
    written_offset: int,
    excluded_offsets: set[int] | None = None,
) -> dict[str, object]:
    """Return compact before/after changes and self/cascading flags."""

    excluded_offsets = excluded_offsets or set()
    changes: list[dict[str, int | None]] = []
    excluded_changes: list[dict[str, int | None]] = []
    self_changed = False
    other_changed = False

    for offset in sorted(set(before) | set(after)):
        old = before.get(offset)
        new = after.get(offset)
        if old == new:
            continue
        change = {"offset": offset, "before": old, "after": new}
        if offset in excluded_offsets and offset != written_offset:
            excluded_changes.append(change)
            continue
        changes.append(change)
        if offset == written_offset:
            self_changed = True
        else:
            other_changed = True

    return {
        "changes": changes,
        "excluded_changes": excluded_changes,
        "written_offset_changed": self_changed,
        "other_offsets_changed": other_changed,
    }
