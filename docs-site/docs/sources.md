# Sources and their terms

The sources an agent reaches for are not interchangeable. One lets you read
its data and build on it. One asks for its name to travel with the result. One
hands it over under a licence. One does not let you have it at all unless you
say why. A deployment that treats them the same is a deployment that finds
out the difference in a notice from one of them.

This page describes the `sources/` registry. The current checkout provides the
registry, the operator's opt-in record, the guard in the web tool's outbound
path, the `sources.*` RPC methods, and the `raven sources` command; it does
not yet provide a settings page. What follows is the verified behaviour.

## The four tiers

| Tier | What it means |
| --- | --- |
| `public` | Free to read and to build on. Nothing has to be carried with it and no licence has to be held |
| `attributed` | Usable, but the source's terms ask for its name to travel with whatever comes out |
| `licensed` | Usable under a licence the operator holds, or has to get one for |
| `restricted` | Not usable until the operator records why this deployment may use it |

A tier is not a quality score. It is a statement about permission.

## The order of the two things

The rule is consulted **before** the request, not after it. A refusal after the
fact would be useless: the data would already have left the machine.

Every outbound call to a registered source goes through one place --
`raven.sources.transport.send` -- so the runtime's own web tool and the
research flow's web tool are both covered by the same rule. A source that is
not in the registry is refused: a source added to the code without a row in
the table has not been looked at, and looking at it is the whole point.

When a source is refused, the answer says which rule was hit and what would
change it, because the person who can change it is the operator and not the
model:

> Tavily refused by source policy. The source's terms do not permit the use
> as it stands. Tell the user: record why this deployment may use tavily
> (`raven sources optin tavily --basis "..."`), or pick another source. The
> source's terms are at https://tavily.com/.

## Conditions on a source

Some sources are usable, but not for every use. The condition is named rather
than folded into the tier, so a reader can see exactly what is closed:

| Source | The use that is closed |
| --- | --- |
| `google_places` | `derivation` -- the Maps Platform terms forbid creating content from Maps content. `store` -- only the place id may be kept, not names, addresses or reviews |
| `crunchbase` | `generative_pipeline` -- the terms forbid using the data to train models, so a pipeline needs a licence that says so |
| `cloudflare_radar` | `commercial` -- the data is published under CC BY-NC 4.0, which permits non-commercial use only |
| `nominatim` | `platform` -- the usage policy forbids no-code, low-code and vibe-coding platforms from building on the public API, which is what a harness is. Overpass is the supported way to read the same data |
| `g2` | `no_contract` -- the Data API is entitlement-gated, so an account without a contract cannot reach it at all |
| `capterra` | `reviews` -- there is no public API; the data is a Gartner Digital Markets product reachable only under a commercial feed agreement |
| `linkedin` | `profile_data` -- member data is not licensed for collection |
| `amazon_paapi` | `api` -- the Product Advertising API v5 is deprecated in favour of the Creators API |
| `google_cse` | `api` -- the Custom Search JSON API is closed to new customers |

## The opt-in

A source whose terms do not permit a use can still be used by an operator who
has a reason: a licence they hold, a contract they are party to, a decision
they made and can point at later. What the registry cannot do is decide that
for them, and what it must not do is let the use happen with no trace.

So the reason is written down next to the source and the use it covers, and
the registry reads it back before it allows the call. An opt-in with no reason
behind it is refused: it would let a later reader conclude that the deployment
had been checked when it had not.

An opt-in covers the use it names and nothing else. "Yes to this source, in
this deployment" is not "yes to every use of it".

## The RPC surface

Three JSON-RPC methods expose the registry:

| Method | What it does |
| --- | --- |
| `sources.list` | The rows: what each source's terms permit |
| `sources.check` | May this deployment use this source for this use, and if not, why not |
| `sources.optin.set` | Record, or withdraw, why this deployment may use a source |

`sources.check` never refuses. The question has an answer either way, and a
caller that has to catch an exception to learn "no" will catch it and carry
on. So the answer is always a payload: `allowed`, the tier, the reason, and
the remedy that says what would change the answer.

The one refusal that does raise is on `sources.optin.set`: recording an
acceptance for a source that is not in the registry is `source_policy_refused`
(-32034). Recording an acceptance for terms nobody has looked at would put a
permission on record for a source the registry has never seen, and the next
reader would take it for a checked deployment.

## The command line

`raven sources` is the operator's way in:

| Command | What it does |
| --- | --- |
| `raven sources list` | The rows: what each source's terms permit |
| `raven sources check <source> --use research` | May this deployment use it, and if not, why |
| `raven sources optin <source> --basis "..."` | Record why this deployment may use it |
| `raven sources revoke-optin <source>` | Withdraw that acceptance |

A refusal names `raven sources optin` in its remedy, so the operator is told
what to run rather than only what went wrong.

## What may be kept

The same reasoning reaches the record store. Where a source's terms forbid
storing its data, a record may not hold it -- and a deployment that has
decided otherwise records that decision against the record it applies to, and
the store checks it before the write lands. See
[Records and the evidence behind them](records.md).
