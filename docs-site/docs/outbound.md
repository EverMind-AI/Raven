# Outbound: a send that cannot happen twice

Raven's own documentation says the quiet thing out loud: *"A retry can repeat
external side effects."* A run that writes to somebody, dies before it records
that it did, and is retried writes to that somebody twice. For a prospect that
is not a retry. It is a stranger who contacted them twice.

This page describes the `outbound/` package, which is the answer to that
sentence. It sends nothing itself and decides nothing. It writes down what was
attempted, refuses a second attempt while the first is unresolved, and hands
the rest to a person.

**What this does and does not change yet.** The guard is a library, and the
send path that would use it — the outreach vertical — is not built. Until
that path is wired to the guard, a send made through any other route can
still repeat, and the sentence in the troubleshooting guide is still true of
them. What exists now is the thing that makes a repeat impossible *for
whoever adopts it*, and a way for a person to see the sends that are waiting
on a decision.

## What a bare send does not have

A bare `channel.send(text)` has three gaps, and they are the same three gaps:

- **Nothing is written before the effect.** The only record that a person was
  contacted is the one a later run may or may not make.
- **A second attempt looks like the first.** Nothing distinguishes "sending
  again because the first attempt was lost" from "sending again because it
  was forgotten".
- **"I do not know" has no spelling.** A send whose outcome is unknown has to
  be either sent or not sent, and the guess that is wrong writes twice.

The package closes them with three things.

## Before the effect

A send is prepared as an `interaction` record **before** the caller is handed
anything to send. The record is written in the `prepared` state, and the
question "did we already write to this person" is then answered by the store
rather than by a retry's optimism.

The store's own interaction chain already says what this needs: `new ->
prepared -> sent`, with `delivered`, `bounced` and `unsubscribed` reachable
only from `sent`. A message is delivered only once it was sent. The guard
writes that chain rather than inventing a parallel one.

```python
from raven.outbound import guard_send

with guard_send(
    store,
    subject="acme-co",
    subject_type="company",
    channel="email",
    body="Hello there",
) as prepared:
    channel.send(prepared.subject, prepared.message_with_token())
```

The record exists while the block runs, not after it returns. If the block
raises, the send is left `prepared` rather than being marked sent or
forgotten -- an exception from a send does not mean the message did not go
out, and a timeout that lost its reply looks exactly like one that did.

While a send is unresolved, a second attempt at the same subject is
**refused**:

```
there is already a send to 'acme-co' whose outcome is unknown
(rv-10D6AABCA0). Reconcile it before sending again.
```

## When nobody can tell

A prepared send nobody can resolve is an **ambiguous outcome**. It is
surfaced, never auto-resent and never quietly forgiven -- "probably did not go
out" is the guess that double-sends.

Reconciliation asks a probe about one prepared send at a time. The probe
answers `sent`, `not_sent`, or anything else for "nobody can tell", and the
default probe cannot tell, so **nothing is ever resolved automatically** unless
a caller says it knows:

| The probe says | What happens | Why |
| --- | --- | --- |
| `sent` | The record moves to `sent` | The message went out, and the store must keep saying so |
| `not_sent` | The record is removed | A record of a message that never left is a claim that does not belong in the store |
| anything else | It stays `prepared` and is reported | Nobody can tell, so nobody guesses |

A person resolves the rest by hand.

## The correlation token

Every send carries a **correlation token**: a short, quotable handle, `rv-`
and ten hex characters. It goes in the message, and a reply that quotes it
resolves back to the interaction that sent it -- and from there to the record
the message was about.

```python
from raven.outbound import find_by_token, token_from_text

token = token_from_text(reply_body)
interaction = find_by_token(store, token)
```

It is a handle and not a secret. It is quoted in the body of a message that
anybody may read, so knowing one tells you which message a reply belongs to
and nothing about the record behind it.

## The command line

`raven outbound` is the operator's side of the guard:

| Command | What it does |
| --- | --- |
| `raven outbound list` | The outbound interactions: what was sent, to whom |
| `raven outbound ambiguous` | The sends whose outcome nobody knows |
| `raven outbound resolve <token> --sent \| --not-sent` | A person's decision about one of them |

The two decisions are not symmetric. `--sent` moves the record to `sent`, so
the message counts as one that was written. `--not-sent` removes it, because a
record of a message that never left is a claim that does not belong in the
store -- and leaving it would say we wrote to somebody we did not.

See also [Records and the evidence behind them](records.md) and
[Sources and their terms](sources.md).
