# Records and the evidence behind them

A record is one durable belief about one thing -- a company, a person, a file
Raven produced, a message it sent -- held together with the evidence for that
belief. It is what lets a run that meets the same company a second time find
the first run's record instead of making a second one, which is how a prospect
ends up being told twice that nobody ever contacted them.

This page describes the `records/` library. The current checkout provides the
library, the record store, the state machine, the `records.*` RPC methods, and
the `raven records` command; it does not yet provide a records section in the
WebUI. What follows is the verified behaviour.

## What a record holds

| Part | What it is | Why it is separate |
| --- | --- | --- |
| `type` | `company`, `contact`, `artifact`, `interaction` or `opportunity` | A company and a person may share a name without becoming one row |
| `dedupe_key` | The identity of the *thing* the record is about | The store keeps one row per thing, so a repeated write is a merge rather than a second row |
| `fields` | What the record believes | The claim, kept apart from the evidence for it |
| `provenance` | Why each field is entitled to say what it says | One wrong field can be corrected without discarding the parts that were right |
| `state` | How far the thing has got | Two runs have to agree on it, or the store stops being a record of what happened |

A field with no provenance is a guess the record cannot defend, and
`Record.unsourced_fields()` names them.

## Merging, not multiplying

Writing the same thing twice is not an error. The store merges:

- the **sources** of both readings are kept, each still tied to where it came from;
- the **fields** the newer run read replace the ones it read;
- the **state only moves forward** -- a second run that finds a prospect already
  qualified does not un-qualify them.

The guarantee that makes the merge work is a unique index on
`(type, dedupe_key)`. Without it, a second run would write a second row and
the two rows would disagree.

## The state chains

A record's state moves along a chain, and the chain is a sequence of
guarantees rather than a list of labels. A state may move to the next step, or
to one of the chain's outcomes. It may not skip a step, and it may not move
backwards.

| Kind | Chain | Outcomes |
| --- | --- | --- |
| `opportunity` | new, discovered, qualified, researched, artifact_built, pitched, engaged, replied, followed_up | won, lost -- from anywhere in the chain, because a run can end early |
| `company` | new, discovered, qualified, researched | -- |
| `contact` | new, discovered, verified | -- |
| `artifact` | new, built, qa_passed | delivered -- only from `qa_passed` |
| `interaction` | new, prepared, sent | delivered, bounced, unsubscribed -- only from `sent` |

An artefact cannot be delivered before it passed the QA gate, and a message
cannot be delivered before it was sent. Those are the two rules most worth
having, and they are why the chains are ordered rather than free.

Every move is written to a journal beside the record: when, from where, to
where, and why.

## The RPC surface

Five JSON-RPC methods expose the store to a harness or a tool:

| Method | What it does |
| --- | --- |
| `records.get` | One record, by id |
| `records.upsert` | Write a record, merging into the one already there |
| `records.list` | The records, filtered by kind and by state |
| `records.transition` | Move a record along its chain |
| `records.grants.set` | Record, or withdraw, why one source's terms may be kept for one record |

A refusal answers with its own code and its own reason. A record that is not
there is `record_not_found` (-32032), because "we have never heard of this
record" and "this record is empty" are different answers, and a client that
guesses between them asks a prospect twice. A move the chain does not allow is
`record_transition_refused` (-32033), because a refused move is information
rather than a failure to work around. A field the source's terms do not let us
keep is `source_policy_refused` (-32034), and that refusal names both the
record and the source, so the acceptance can be written and the record written
again.

## The command line

`raven records` is the operator's way in:

| Command | What it does |
| --- | --- |
| `raven records list` | The records, filtered by kind and by state |
| `raven records get <id>` | One record, with the evidence behind it |
| `raven records grant <id> <source> --basis "..."` | Record why one source's terms may be kept for one record |
| `raven records revoke-grant <id> <source>` | Withdraw that acceptance |

The grant is the decision the store asks for. Where a source's terms forbid
keeping its data, the store refuses the write and **names this command** in the
refusal, so the operator is told what to run rather than only what went wrong.

## Where the store lives

One SQLite file per agent home, under `<home>/records/records.db`, with a
sibling `records.db.lock` as the writer lock. The lock is the repository's
portable advisory lock rather than `fcntl` directly, because `fcntl` degrades
to *unlocked* on Windows, and a concurrent write there would be lost rather
than serialized.

```python
from raven.records import Provenance, Record, RecordType, records_store

store = records_store()
store.put(
    Record(
        type=RecordType.COMPANY,
        dedupe_key="acme",
        fields={"name": "Acme", "employees": 120},
        provenance=[
            Provenance(
                field="name",
                source_url="https://www.sec.gov/edgar/full-text-search",
                source="sec_edgar",
                method="fetch",
            )
        ],
    )
)
```

## What may be kept

A record keeps what a source lets a deployment keep. Where a source's terms
forbid storing its data, the store refuses the write -- and a deployment that
has decided otherwise records that decision first, against the record it
applies to. See [Sources and their terms](sources.md).
