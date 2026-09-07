# Resource admission for ops jobs - design

Date: 2026-09-03
Status: implemented 2026-09-03 evening on feat/exec_machine_and_background (uncommitted); owner review pending
Scope: `raven/ops/connections.py` (registry row fields and validation),
`agents/raven-oncall/plugins/oncall-flow/oncall_flow/tools/ops_declare.py`,
`.../tools/ops.py` (`ops_submit`), `.../occupancy.py`, `.../process_backend.py`
(launcher environment, pre-launch device check, spend width),
`.../openfoam_backend.py` (after-the-fact core count), `.../ledger.py`
(`resources_held` on a record), `.../budget.py` (width per span, unchanged API),
tests under `tests/test_agents_oncall_flow_*.py`.
Companion: `docs/plans/2026-09-03-launcher-custody-and-detached-jobs.md` item 4
points here.

## Problem

The occupancy gate admits jobs by count: a registry row says `concurrency: 1`
and `capacity_refusal` refuses a second non-terminal job on the machine. That is
the wrong unit in both directions, and run11 (2026-09-03) showed the cost.

- `conn_gpu_a800` has two A800 cards and `concurrency: 1`. The main agent asked
  for both cards; the gate refused a second job; the oncall launched the second
  card's jobs through `exec machine=` instead, off the ledger. Three jobs ran
  with no budget debit and no occupancy record.
- A job count cannot express a job that needs more than one device. RL
  training of an 8B model holds all eight cards of an 8-card host; "one job at a
  time" happens to be right there, and "one job per card" would admit seven
  more.
- The gate has no idea which card a job is on. With `concurrency: 2` two
  configs both saying `gpu: 0` are admitted and the second dies of the first's
  memory -- the CUDA OOM the gate was built against (measured 2026-08-31).
- The budget layer bills concurrent GPU jobs as one (`budget.SHARED`: "the card
  was busy, once"). That assumes a single device. Two cards running in parallel
  are two GPU-minutes per minute; the ledger would record one.

CPU machines have the same gate with the same unit. A 32-core box at
`concurrency: 1` runs one 8-core CFD solve at a time; the openfoam backend
already bills cores x wall clock and reads the real core count off
`decomposePar`'s output, so the accounting knows about width while the gate does
not.

## Owner rulings (2026-09-03)

1. One job at a time on a two-GPU machine is wrong.
2. One job per card is also wrong: a large training job may need every card for
   one model.
3. Therefore the unit is neither the job nor the card. **A job declares how much
   of a resource it holds; a machine declares how much it has; the gate admits
   by what is free.**
4. The declaration happens when the campaign is declared (`ops_declare`): the
   oncall must know how many devices one job uses before anything runs.
5. The same design covers CPU-only science (CFD, FEA): the resource is cores,
   optionally memory.

## Decisions taken

1. **Capacity is a machine field; the request is a campaign field with a
   per-config override.** Cards per job is a property of how the job is
   launched (`torchrun --nproc_per_node`, the length of a `CUDA_VISIBLE_DEVICES`
   list, `decomposeParDict`'s `numberOfSubdomains`), and every trial of a
   campaign launches the same way. So the number is said once, at
   `ops_declare`, and a config passed to `ops_submit` may override it for the
   one case that needs it (a 1-card smoke before the 8-card run).

2. **The system assigns devices; the model never names a card.** `ops_submit`
   picks concrete device ids from the free set and the launcher exports
   `CUDA_VISIBLE_DEVICES` for them. The command template must not set it. This
   removes the "which card is idle" judgement from the model, which is where
   tonight's `gpu: 0` / `gpu: 1` bookkeeping lived.

3. **CPU cores are counted, not pinned.** A CUDA process takes a card's memory
   and a second one dies; ranks on cores do not, the kernel schedules them. The
   CPU gate guards oversubscription (a second 32-rank solver on a busy 32-core
   box halves both and bills 32 core-minutes twice for no more output), so it
   counts free cores and injects nothing. `taskset` is not part of this design.

4. **Memory is an optional second dimension.** FEA direct solvers hit memory
   before cores; GPU hosts' data loaders can too. A campaign that declares
   `memory_per_job` is checked against the row's `memory`; one that does not is
   not checked. Same for GPU and CPU rows.

5. **Spend is width x duration, and gate-assigned devices pay separately.**
   `budget.accumulate` already takes a width per span; width becomes the devices
   or cores held. A job the gate handed device ids holds them alone, so two such
   jobs overlapping in time are on different cards and each pays: their spans go
   through `accumulate(..., ADDITIVE)`. Jobs without assigned ids (a legacy row,
   a CPU campaign, a template that pins a device it did not choose) keep the
   campaign's declared overlap rule, where an overlap really is one card busy
   once. Same accumulator, two piles. (Implementation note, 2026-09-03: a single
   pile under `shared` billed two parallel cards as one -- caught by the test,
   not the design.)

6. **A job that exceeds the machine is refused, not queued.** "This machine has
   8, you asked for 16 -- another machine." A campaign that can never run is not
   created (checked at declare), and a config that can never run is not
   submitted.

7. **Multi-node work is out of scope.** Two 8-card hosts for one 16-card job,
   MPI spanning two boxes: this ledger is one machine. Coordination across
   machines is another layer.

## Data model

### Registry row (`connections.json`, validated in `raven/ops/connections.py`)

| field | rows | meaning | validation |
|---|---|---|---|
| `gpus` | `kind: gpu` | number of devices this row may hand out | whole number >= 1; if absent, parsed from `device: "N x ..."` when the prefix is a number, else a non-blocking Problem "row has kind gpu but no gpus count" |
| `cores` | any | core count (exists today, shown to the model) | whole number >= 1 when present |
| `memory` | any | RAM (exists today, free text like `"232 GB"`) | when used for admission, parsed as GiB; unparseable memory disables the memory check with a non-blocking Problem |
| `concurrency` | `kind: cpu` rows without `cores` | legacy job count | unchanged validation; see Migration |

`gpus` joins `_SHOWN` (the model sees capacity when picking a machine). `gpu`
already aliases to `device` in `_MISSPELLED`; `gpus` is a distinct key and is
not aliased.

### Campaign meta (`meta.json`, written by `ops_declare`)

```
"resources": {"gpus_per_job": 1}                    # GPU campaign
"resources": {"cores_per_job": 8, "memory_per_job_gb": 64}   # CFD/FEA campaign
```

Exactly one of `gpus_per_job` / `cores_per_job` is required, matching the row's
kind. `memory_per_job_gb` is optional on either.

### Ledger record (`ledger.py`)

`JobRecord` gains `resources_held: dict | None`:

```
{"gpus": 2, "device_ids": ["0", "1"]}          # GPU job
{"cores": 8}                                   # CPU job
{"cores": 8, "memory_gb": 64}
```

Written at submit, alongside `config`; serialized by `_record_to_dict`, read
back by `_record_from_dict` with `None` for records that predate the field.

## Behaviour by interface

### `ops_declare`

New parameters: `gpus_per_job` (integer), `cores_per_job` (integer),
`memory_per_job_gb` (integer, optional). Checks, in order, each a REFUSED with
the reason and nothing written:

1. The row's kind decides which is required. A `kind: gpu` row without
   `gpus_per_job`: "this machine hands out devices; say how many one job holds
   (torchrun --nproc_per_node, or the length of the CUDA_VISIBLE_DEVICES list
   the code side used)". A `kind: cpu` row without `cores_per_job`: the same
   sentence for cores (`mpirun -np`, `numberOfSubdomains`).
2. Request above capacity: "GPU machine has 2 devices; one job holding 8 can
   never start here. Declare on a machine with 8, or split the work."
3. The command template assigns devices itself: any `CUDA_VISIBLE_DEVICES=` is
   refused ("the system picks the cards and exports this variable; the template
   must not"). `--nproc_per_node=K` present and K != `gpus_per_job` is refused
   as a contradiction; K == `gpus_per_job` is allowed (the launcher's variable
   and torchrun agree).
4. `seed_config` carrying a `gpu` or `CUDA_VISIBLE_DEVICES` key earns a note in
   the declaration reply -- "the system does not read this key; cards are
   assigned at submit" -- not a refusal, since a config may carry it for the
   job's own logging.

Tonight's template `CUDA_VISIBLE_DEVICES=$(... config 'gpu') ./r1_run.sh` stops
at check 3.

### `ops_submit`

Per config: `gpus_needed` / `cores_needed` / `memory_needed_gb` override the
campaign values when present. Then admission, replacing `capacity_refusal`'s
count with a sum:

```
held   = sum(rec.resources_held[unit] for rec in running_on(machine))
free   = capacity[unit] - held
admit  iff requested <= free   (and the same for memory when declared)
```

Refusal text names the unit and the holders, as today's does:

```
REFUSED: GPU machine has 2 devices and 1 is held:
  r1-multiseed: DATA_SEED_OFFSET1_..._e3db4397 (running, devices 0)
This config asks for 2. Submit one that fits in 1, or wait -- your wake
returns when a job lands. Nothing was submitted.
```

On admission for a GPU row: pick `requested` ids from `all_ids - held_ids`,
lowest first, and write them into the record's `resources_held` before the
launcher is staged. The ids ride to the backend in `JobSpec.labels`
(`{"device_ids": "0,1"}`), keeping `JobSpec` backend-agnostic.

Occupancy release is unchanged: a record leaving the non-terminal set frees what
it held. This is why item 1 of the companion plan (the launcher refusing a
self-detaching command) is a prerequisite -- a job recorded terminal at t+0.4 s
frees its cards while still running.

### Launcher (`process_backend.py`)

- Environment: when `device_ids` is present in the labels, the launcher's child
  runs as `CUDA_VISIBLE_DEVICES=<ids> bash -o pipefail -c ...`. No other
  environment is touched.
- Pre-launch check, GPU rows only, on the machine, immediately before the
  child starts: `nvidia-smi --query-gpu=index,memory.used --format=csv,noheader
  -i <ids>`. A chosen card with `memory.used` above `_FOREIGN_USE_MIB` (default
  1024; a resting A800 shows 2-5 MiB) is held by someone outside the ledger. The
  launcher writes `result.json` failed with `error: "device 1 held by a process
  outside the ledger (41209 MiB in use) -- the machine is shared; nothing was
  started"` and exits 0 minutes. The gate then sees a terminal record and the
  oncall's wake reads the reason; the oncall resubmits and gets another id if
  one is free. No `nvidia-smi` on the box (a CPU row, or a missing binary) skips
  the check.
- Spend: `spent_minutes` reads width and whether ids were assigned from the
  staged `.raven-resources` file (written at submit next to `config.json`, so a
  restart loses nothing), builds spans `width` wide, and bills assigned-device
  spans additively and the rest under the declared overlap (decision 5). The
  openfoam backend keeps its own measured
  width and, when the measured `processor*` count differs from the declared
  `cores_per_job`, records `{"declared": 8, "measured": 16}` on the record and
  the campaign reply says so once. Measured wins for billing (the box was busy
  that wide); the declaration is what the gate trusted, so the disagreement is
  worth a line.

### `ops_kill`, terminal states

No change to the kill itself. Any transition to a terminal status releases the
record's resources by virtue of `running_on` reading non-terminal records only.

### `ops_tune_status`

Next to the budget line: `Machine: <name> has N device(s); H held, F free.`,
then one line per non-terminal job on the machine across campaigns -- this
campaign's or another's, how many units it holds and on which ids. A row
admitted by job count prints nothing here; it has no capacity to state.

## Migration

Three registered machines today, all at `concurrency: 1`. The moment the new
code runs, each must behave deterministically.

| row state | behaviour |
|---|---|
| `kind: gpu`, has `gpus` | new admission by devices; `concurrency` ignored with a one-time non-blocking Problem "concurrency is not read on a gpu row; gpus decides" |
| `kind: gpu`, no `gpus`, `device` starts with `N x` | `gpus = N`, same as above; the Problem says which N was inferred |
| `kind: gpu`, no `gpus`, no parsable `device` | capacity unknown: admit as today by `concurrency` (job count), with a blocking Problem on any campaign declaring `gpus_per_job > 1` ("this row does not say how many devices it has") |
| `kind: cpu`, has `cores` | new admission by cores; `concurrency` ignored with the same one-time Problem |
| `kind: cpu`, no `cores` | as today, by `concurrency` |

Campaign metas without `resources` (every campaign declared before this ships):
treated as `gpus_per_job: 1` on a gpu row and `cores_per_job: 1` on a cpu row,
so an in-flight campaign keeps running and holds one unit. Ledger records
without `resources_held` count as one unit of the row's kind for admission and
width 1 for spend, which is exactly what they were billed before.

For the owner's instance this means editing three rows once: `gpus: 2` on
`conn_gpu_a800`; `conn_cpu_32c` and `local-mac` already have `cores` and need
nothing.

## Out of scope, stated

- Multi-node jobs across machines.
- Core pinning (`taskset`, `numactl`).
- Reading real GPU utilisation to admit "half a card" -- devices are whole.
- A scheduler that queues a refused submit and starts it later. The wake
  mechanism already brings the oncall back when a job lands; it resubmits.

## Tests

- `connections.py`: `gpus` validation; inference from `device: "2 x ..."`;
  the migration Problems above; `gpus` in `_SHOWN`.
- `ops_declare`: the four checks, each with its refusal text; a template with
  `--nproc_per_node=2` and `gpus_per_job: 2` passes; `=8` against `2` refuses.
- `occupancy.py`: admission by sum of held units; free-id selection lowest
  first; a record without `resources_held` counts as one; refusal names the
  holders and their ids.
- `process_backend.py`: launcher body carries `CUDA_VISIBLE_DEVICES=<ids>` when
  labels have `device_ids` and nothing otherwise; the pre-launch `nvidia-smi`
  check writes the failed result with the reason (FakeHost returning a busy
  card); spans use width from `resources_held`; two 1-device jobs overlapping
  bill two minutes per minute.
- `openfoam_backend.py`: declared vs measured disagreement is recorded once.
- `ledger.py`: `resources_held` round-trips; an old record reads back as `None`.
- `budget.py`: unchanged; existing width tests already cover the accumulator.

## Evidence

- run11 refusal at 19:47:32 ("GPU machine runs 1 job(s) at a time ... 0 is/are
  already on it") followed by the off-ledger `exec` launch at 19:47:54 --
  `~/.raven-main/traces/logs/acp-frames/2026-09-03/Raven-Oncall-111106482791.jsonl`.
- Registry rows: `~/.raven-main/connections.json` (`conn_gpu_a800` `concurrency: 1`,
  `device: "2 x NVIDIA A800-SXM4-80GB"`).
- Existing width accounting: `oncall_flow/openfoam_backend.py` (`cores_used`,
  `processor*` count), `oncall_flow/budget.py` (`accumulate(spans, overlap)`),
  `oncall_flow/process_backend.py` `spent_minutes` (constant width `1.0` with the
  comment "a training job here has the device to itself").
