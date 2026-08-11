"""Parse PCI capability regions from a configuration-space snapshot."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class CapabilityRegion:
    kind: str
    capability_id: int
    start: int
    end: int

    def contains(self, offset: int) -> bool:
        return self.start <= offset < self.end

    def serialized(self) -> dict[str, int | str]:
        return asdict(self)


def _byte(snapshot: list[int | None], offset: int) -> int:
    if offset >= len(snapshot) or snapshot[offset] is None:
        return 0
    return int(snapshot[offset])


def _word(snapshot: list[int | None], offset: int) -> int:
    return _byte(snapshot, offset) | (_byte(snapshot, offset + 1) << 8)


def _dword(snapshot: list[int | None], offset: int) -> int:
    return _word(snapshot, offset) | (_word(snapshot, offset + 2) << 16)


def parse_capability_regions(snapshot: list[int | None]) -> list[CapabilityRegion]:
    """Walk conventional and extended linked lists, rejecting malformed loops."""

    regions: list[CapabilityRegion] = []

    conventional: list[tuple[int, int]] = []
    pointer = _byte(snapshot, 0x34) & 0xFC
    visited: set[int] = set()
    while 0x40 <= pointer < min(len(snapshot), 0x100) and pointer not in visited:
        visited.add(pointer)
        capability_id = _byte(snapshot, pointer)
        conventional.append((pointer, capability_id))
        pointer = _byte(snapshot, pointer + 1) & 0xFC

    conventional.sort()
    for index, (start, capability_id) in enumerate(conventional):
        end = conventional[index + 1][0] if index + 1 < len(conventional) else min(0x100, len(snapshot))
        kind = "PM" if capability_id == 0x01 else "STD_CAP"
        regions.append(CapabilityRegion(kind, capability_id, start, max(start + 2, end)))

    extended: list[tuple[int, int]] = []
    pointer = 0x100
    visited.clear()
    while pointer + 4 <= len(snapshot) and pointer not in visited:
        visited.add(pointer)
        header = _dword(snapshot, pointer)
        capability_id = header & 0xFFFF
        if capability_id in (0, 0xFFFF):
            break
        extended.append((pointer, capability_id))
        next_pointer = (header >> 20) & 0xFFF
        if next_pointer == 0 or next_pointer <= pointer:
            break
        pointer = next_pointer

    extended.sort()
    for index, (start, capability_id) in enumerate(extended):
        end = extended[index + 1][0] if index + 1 < len(extended) else len(snapshot)
        kind = "VSEC" if capability_id == 0x000B else "EXT_CAP"
        regions.append(CapabilityRegion(kind, capability_id, start, max(start + 4, end)))

    return regions


def region_for_offset(regions: list[CapabilityRegion], offset: int) -> CapabilityRegion:
    for region in regions:
        if region.contains(offset):
            return region
    start = (offset // 0x100) * 0x100
    return CapabilityRegion("SPATIAL", -1, start, start + 0x100)
