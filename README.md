# RAS-Strike

RAS-Strike explores PCIe configuration-space behavior in two stages:

1. **Stage 1 — behavioral mapping:** exercise individual byte offsets and
   identify writes that produce self-only, cascading, or catastrophic effects.
2. **Stage 2 — structure-guided exploration:** use the Stage 1 map and PCI
   capability layout to generate weighted multi-register sequences.

A separate utility manually replays recorded sequences.

## Safety warning

RAS-Strike performs destructive writes to PCIe configuration space. These
writes can disable a device until power cycle, terminate a VM, or crash a host.
Run hardware-facing commands only inside the dedicated evaluation VM, after
attaching the assigned device, and with the exact BDF supplied for the
experiment. Never run this tool on production, personal, or shared systems.

## Requirements

- Linux with Python 3.10 or newer
- `pciutils` (`setpci` and `lspci`)
- root privileges inside the evaluation VM
- a PCIe device passed through to that VM

The implementation otherwise uses only the Python standard library.

## Get RAS-Strike

Clone the artifact branch and enter the repository:

```bash
git clone --branch RAS-Strike --single-branch \
  https://github.com/IBM/fuzz-testing.git ras-strike
cd ras-strike
```

For artifact evaluation, use the commit identified as the evaluated version in
the top-level RASCrash evaluator guide rather than an unpinned branch tip.

## Supported entry points

```text
discover_interesting_offsets.py  Stage 1
guided_fuzzer.py                 Stage 2
replay_sequence.py               Manual sequence replay
ras_strike/                      Shared implementation
tests/                           Non-hardware tests
```

The repository intentionally contains only the supported implementation,
non-hardware tests, documentation, and license.

## Stage 1: behavioral mapping

Stage 1 captures configuration space before and after every byte write. Each
test is classified as:

- `inert`: no non-excluded change;
- `sensitive_self`: only the target offset changed;
- `sensitive_cascading`: another offset also changed; or
- `catastrophic`: the device stopped responding.

`--skip` offsets are never written. `--exclude` offsets are still written, but
unrelated changes at those noisy offsets are ignored when identifying
cascading behavior.

### Exhaustive mode

Exhaustive mode is the default. It tests `0x00` through `0xff` at every
non-skipped offset:

```bash
sudo python3 discover_interesting_offsets.py \
  --device <BDF> \
  --range extended \
  --mode exhaustive
```

For the 4 KiB extended space, this is approximately 1,048,576 write tests and
is not a quick artifact smoke test.

### Bounded evaluator mode

Fast mode tests one value per offset, defaulting to `0xff`. `--max-tests`
limits the number of completed writes without reducing the captured
configuration-space baseline:

```bash
sudo python3 discover_interesting_offsets.py \
  --device <BDF> \
  --range extended \
  --mode fast \
  --value ff \
  --max-tests 256
```

With the default start offset, this tests `0x000` through `0x0ff` while
retaining a 4 KiB baseline for Stage 2 capability parsing. The limit is
configurable and counts actual writes, so skipped offsets do not consume it.
This run validates workflow mechanics. It does **not** reproduce the paper's
exhaustive campaign or establish that untested offsets are inert or safe.

### Focused evaluator run

Even fast mode can take substantial time when it traverses all 4,096 offsets.
On the evaluation setup, a full-range fast run can take around two hours;
timing varies by device and platform. Exhaustive mode performs 256 writes per
offset and takes substantially longer.

Evaluators can verify the two-stage workflow with a smaller Stage 1 run. The
following command tests 128 offsets from hexadecimal offset `0x100` through
`0x17f` while still capturing the full 4 KiB baseline:

```bash
mkdir -p ae-results/stage1
sudo python3 discover_interesting_offsets.py \
  --device <BDF> \
  --range extended \
  --mode fast \
  --start 100 \
  --max-tests 128 \
  --delay 0 \
  --verbose \
  --output-dir ae-results/stage1
```

The resulting partial map is valid Stage 2 input. Stage 2 uses observations
from the tested offsets for weighting, but it does **not** limit exploration to
`0x100` through `0x17f`: untested, non-skipped offsets remain eligible. The
smaller Stage 1 run therefore reduces evaluation time, not the disruptive risk
of Stage 2.

### Important options

```text
-d, --device BDF             target device
-r, --range RANGE            basic (64), full (256), extended (4096)
-mode, --mode MODE           exhaustive or fast
-v, --value HEX              fast-mode value
-s, --skip HEX,...           offsets never written
-exclude, --exclude HEX,...  noisy offsets ignored in secondary diffs
-start, --start HEX          first offset
-delay, --delay SECONDS      post-write delay
-restore, --restore          restore the target byte after a completed test
-noreset, --no-reset         skip the final device-reset request
--max-tests COUNT            stop after COUNT completed write tests
--verbose                    timestamp each write and result on stdout
--output-dir DIRECTORY       output location
--resume CHECKPOINT          resume an interrupted run
```

Legacy forms such as `-d=41:00.1`, `-r=extended`, and `-s=4,78,9f` remain
accepted. The post-write delay defaults to zero; use `--delay SECONDS` only
when a device requires additional settling time.

### Outputs and crash recovery

Stage 1 creates:

```text
discover_offsets_<timestamp>.log
discover_offsets_<timestamp>_events.jsonl
discover_offsets_<timestamp>_checkpoint.json
discover_offsets_<timestamp>.json
```

Before each hardware write, Stage 1 atomically stores the in-flight offset and
value, appends an intent record, and flushes and `fsync`s its checkpoint,
ledger, and log. Pressing Ctrl+C writes a partial summary with
`completed: false` and `stop_reason: "interrupted"` before exiting. Resume an
interrupted or crashed run from its machine-readable checkpoint, not its text
log:

```bash
sudo python3 discover_interesting_offsets.py \
  --resume discover_offsets_<timestamp>_checkpoint.json
```

The unfinished write is conservatively recorded as unresolved, its offset is
added to the run's skip set, and execution continues at the following offset.
A bounded resume continues toward the original `--max-tests` target. The
summary JSON contains category lists, sensitive offsets, anomalous cascading
values, skipped/catastrophic writes, the baseline snapshot, tested-offset
coverage, completion status, and stop reason.

## Stage 2: structure-guided exploration

Run Stage 2 with a completed or partial Stage 1 JSON:

```bash
sudo python3 guided_fuzzer.py \
  --device <BDF> \
  --stage1 discover_offsets_<timestamp>.json \
  --iterations 10000 \
  --writes 2 \
  --seed 12345
```

Stage 2:

- walks conventional and extended PCI capability chains;
- recognizes Power Management and VSEC regions;
- raises sampling weights for sensitive and cascading offsets;
- reuses anomalous values observed during Stage 1;
- groups offsets in the same capability or nearby spatial region;
- excludes skipped and catastrophic offsets; and
- retains seeded randomness for ordering exploration.

Each sequence intent and result is appended and `fsync`ed to:

```text
fuzz_guided_<timestamp>_sequences.jsonl
fuzz_guided_<timestamp>_corpus.jsonl
```

The sequence ledger records every attempt; the corpus retains sequences that
reached a previously unseen configuration-space state. The corresponding
summary records the seed, iteration counts, observed states, capability
regions, and any sequence after which the device became unresponsive. Reusing
the same Stage 1 input, options, and seed produces the same generated sequence
stream.

For partial Stage 1 input, all non-skipped and non-catastrophic offsets remain
eligible. Sensitive and cascading offsets, related capability regions, and
nearby offsets receive higher sampling weights. Untested offsets are not
treated as inert or safe. The Stage 2 summary copies the Stage 1 coverage
metadata so this distinction remains explicit.

Pressing Ctrl+C records an interrupted sequence when necessary and writes a
partial Stage 2 summary with `completed: false`,
`stop_reason: "interrupted"`, and the completed iteration count. Stage 2 does
not resume an interrupted campaign; its completed ledger entries remain
available for deterministic replay.

Pass `--verbose` to either stage to print each configuration-space write and
the resulting classification or sequence state with a monotonic elapsed-time
prefix such as `[+00012.345678]`. Stage 1 also prints restoration writes when
`--restore` is enabled. Durable file recording remains enabled regardless of
this flag.

Use `--exclude HEX,...` to override the noisy-offset list inherited from Stage
1 when computing Stage 2 state differences.

## Manual sequence replay

Replay a Stage 2 ledger entry only after checking that its device assignment
and recovery procedure match the current experiment.

Inspect without writing:

```bash
python3 replay_sequence.py \
  --device <BDF> \
  --input fuzz_guided_<timestamp>_sequences.jsonl \
  --sequence-id 42 \
  --dry-run
```

Replay after explicit confirmation:

```bash
sudo python3 replay_sequence.py \
  --device <BDF> \
  --input fuzz_guided_<timestamp>_sequences.jsonl \
  --sequence-id 42
```

For an evaluator script that has already performed its own confirmation, add
`--yes`. The utility also accepts legacy `@ offset value` pattern files:

```bash
sudo python3 replay_sequence.py \
  --device <BDF> \
  --input pattern_file \
  --format legacy
```

`--reverse` replays either format in reverse order. Replay intent is durably
logged before each write, and replay stops if the access fails or the device
becomes unresponsive.

## Non-hardware verification

The test suite uses an in-memory PCI backend and performs no device access:

```bash
python3 -m unittest tests.test_ras_strike -v
```

It verifies exhaustive and fast modes, skip/exclude handling, cascading
changes, interrupted-run recovery, legacy JSON compatibility, capability
parsing, deterministic weighted generation, durable Stage 2 output, pattern
parsing, replay order, reverse mode, and dry-run behavior.

## Artifact evaluation

The RASCrash evaluator guide defines the required E1 configuration, pinned
commit, authorized machine and BDF, exact commands, expected outputs,
acceptance criteria, and result collection procedure. Follow that guide for
the required evaluation run. This README is the tool reference for alternative
Stage 1 modes, Stage 2 configuration, interruption recovery, and sequence
replay.

## Output handling

Keep run outputs outside the repository when possible. Before terminating a
CloudLab experiment, copy required summaries, ledgers, checkpoints, and replay
logs off the ephemeral node.
