# Documentation site for install, configuration and usage

Status: design only, nothing implemented. Every number below was measured on
`origin/main` at `6d8c36385` unless stated otherwise; the measurement is named
beside each one so a later reader can re-take it rather than trust it.

## Why

`README.md` is 524 lines and `README.zh-CN.md` is 485 (`wc -l`). Four of those
sections are reference material that grows with the code rather than prose a
first-time reader needs: self-hosting, launching the web UI, the command
reference, and the repo layout. They push the parts that decide whether a reader
continues -- what Raven is, what it scores, how to install it -- below the fold,
and they grow monotonically, so the crowding gets worse with every release.

A README is also the wrong shape for reference material. It has no navigation, no
search, and no per-page URL, so a reader looking for one Docker flag scrolls
through the Compose walkthrough to find it.

## Scope

Four sections move. Measured with a script that splits each file on `^## ` and
counts the lines between one heading and the next. The two languages differ
because the Chinese prose is more compact, not because the sections carry
different material:

| Section | `README.md` | `README.zh-CN.md` | Destination page |
|---|---|---|---|
| Self-Hosting | 84 | 58 | `self-hosting` |
| Launch WebUI | 16 | 16 | `webui` |
| Command Reference | 38 | 38 | `commands` |
| Repo layout | 68 | 68 | `repo-layout` |
| Total | 206 | 180 | |

`README.md` ends at 318 lines and `README.zh-CN.md` at 305. Built-in Agents,
Connect Third-Party Agents, Core Systems, Architecture, the ecosystem section,
contributing and license stay in the READMEs.

Quick Start is also available as a first-class site page so the documentation
site is self-contained. The README keeps its own install section because
`install.sh` and both READMEs are pinned to each other by
`test_readme_quickstart_matches_the_installer`. Core Systems stays because it
is a five-row positioning table, not a procedure. Architecture stays because
its Mermaid diagram is the shortest accurate answer to "how is this put
together", which is a README's job.

## Site structure

The site source is `docs-site/`, not `docs/`. `docs/README.md` defines `docs/` as
"design notes, dated records, and developer references", and `docs/plans/` is an
archive "deliberately not held to today's layout". A user manual has a different
audience and a different lifecycle: it must describe the tree as it is today,
where an archive must not be updated to match a rename.

```
docs-site/
  mkdocs.yml
  docs/
    index.md          index.zh.md
    quick-start.md    quick-start.zh.md
    self-hosting.md   self-hosting.zh.md
    docker.md         docker.zh.md
    webui.md          webui.zh.md
    commands.md       commands.zh.md
    repo-layout.md    repo-layout.zh.md
```

Both languages carry every page. The two READMEs are section-for-section
symmetric, so an English-only site would leave Chinese readers with 180 fewer
lines and a destination they cannot read. The content for both languages already
exists, so the marginal cost of parity at migration time is zero.

Translations use the `mkdocs-static-i18n` suffix structure (`page.md` beside
`page.zh.md`) rather than parallel directories, so the two languages sit adjacent
in one listing and a missing update is visible without navigating. The suffix is
`.zh.md` where the repository's READMEs use `.zh-CN.md`; the site is configured
for locale `zh` and serves `/zh/<page>/`, so the filename difference is not
reader-visible and is not worth overriding the plugin default for.

`Launch WebUI` stays a page of its own at 16 lines rather than a section of
self-hosting, because it answers a question a reader arrives with ("how do I open
the web UI") rather than a step within a longer procedure.

## Presentation

The shell is a fixed left rail, 13.7rem wide, carrying the brand, the search
box, the whole navigation tree, and language and repository icons at its foot;
the content column and the footer both begin at the rail's right edge.
Material's default shell puts the navigation under a horizontal header, and
`navigation.tabs` adds a tab row that swaps the sidebar beneath it. Both hide
one group while showing another. Seven pages in two groups fit in a single rail,
so every destination stays on screen and no section is a click away.

The navigation is sectioned rather than flat: `Guide` and `Reference` are group
labels, rendered in the rail as headings above their pages rather than as
entries a reader can land on.

**The brand mark is inlined in the stylesheet as a `data:` URI, not shipped as
`theme.logo`.** Section 7 of `AGENTS.md` blocks image files outside the
application source trees, and `scripts/check_large_files.py` enforces it by
extension, so a committed `.svg` under `docs-site/` fails the gate. Material
draws a logo in two places -- the header above 76.25em and the navigation
drawer below it -- and each is replaced by a `::before` sized to the glyph it
drops. The drawer half of the rule carries `.md-nav__title` because Material
sets `display` on that glyph through a three-class selector, which outranks a
two-class rule whatever the load order.

**Every popup that hangs off a relocated control is relocated with it.**
Material sizes and aims its popups for the header bar it ships: the language
menu opens downward from `top: calc(100% - .2rem)` and centres on its button,
and the search panel typesets its results at a fixed `34.4rem`. In the rail the
language control sits at the foot, so that menu is flipped to open upward,
left-aligned, with its arrow turned over; the search panel is clipped to the
rail, so the rows are sized to it rather than to the width of a header dropdown.
Neither failure is visible to a build or to a DOM query -- a menu below the
viewport and a row clipped mid-line both report a box and answer
`querySelector` -- so `tests/integration/test_docs_site_controls_e2e.py` hit-tests
each control against the rendered page instead. `norecursedirs` excludes
`tests/integration`, so that file is run by hand, not by CI.

The footer carries the previous/next row and nothing else. Material's generator
notice is switched off through the theme's own `extra.generator` flag, which its
`partials/copyright.html` reads; with no `copyright` and no social links
configured the meta bar below that row then holds nothing, so it is not shown.
Configuring a copyright line means dropping the rule that hides it.

The landing page is a directory of linked cards rather than prose. A list of
links reads as a paragraph to be worked through; cards make the four
destinations, and the three ordered steps before them, scannable in one pass.
Both are one screen, so the difference is entirely in how fast a reader who
already knows what they want can leave the page.

**Navigation labels are the one piece of reader-visible text that lives in
`mkdocs.yml` rather than in a page.** They therefore need `nav_translations` per
locale, and no comparison of the two languages' Markdown can detect their
absence: the pages are identical whether or not the labels are translated. The
build reports the count it applied (`Translated 9 navigation elements to 'zh'`),
which is the signal to read. The Chinese pages also drop the brand's italic
serif accent word, because Cormorant Garamond carries no CJK glyphs and the
accent would silently fall back to another face.

The right table of contents draws the heading outline as **one continuous
polyline** and lights the stretch of it the reader is inside.

The line is walked in document order rather than drawn per nesting level. Each
entry contributes a vertical run in the column its heading level owns -- level
one at x=1, each level below it 10px further right -- and the 12px between two
entries is where the line changes column, which renders as a diagonal when the
depths differ and as plain vertical when they do not. A run stops 6px short of
each boundary it shares with a neighbour so that gap is free for the turn; the
first and last runs are flush, having no neighbour above or below. Drawing one
line per level instead leaves a second line running the length of every parent
row, which is the shape this replaced.

An entry is current while its heading is anywhere in the viewport, so several
are current at once and the lit stretch grows and shrinks rather than jumping.
It runs from the first current entry's run to the last without a break, which is
what carries the colour across a diagonal when a parent and the children under
it are on screen together. The lit path is the same path as the skeleton,
clipped to that range, so the two can never disagree about where the line goes.

The indent follows the heading level, not whether an entry has children: a
trailing level-one heading with nothing under it belongs in the same column as
one that does. Material pulls a nested list up into the entry below it, which
the geometry above cannot survive, so that margin is reset -- entries have to
meet exactly for the line to cross their boundary.

`docs-site/docs/javascripts/toc-progress.js` rebuilds the path on load and
resize and moves only the clip rectangle on scroll.

The brand skin in `docs-site/docs/stylesheets/evermind.css` remaps Material's own
CSS custom properties onto the EverMind token set, then resets the corners
Material hardcodes rather than reading from a variable.

**No template overrides.** Material's documented customisation path is
`theme.custom_dir` with Jinja partials under `overrides/`, and those are `.html`:
`scripts/check_large_files.py` holds `.html` in `BLOCKED_ASSET_EXTENSIONS` and
fails any commit adding one outside `bridge/`, `ui-web/` and `ui-tui/`. A future
maintainer reaching for a template override will hit that gate rather than a
review comment, so the constraint is recorded here and not in the stylesheet
alone. `extra_css` and `extra_javascript` are unaffected -- neither extension is
gated -- so behaviour that needs to read the rendered page, as the table of
contents does, is reachable; what stays out of reach is anything that needs new
markup Material does not already emit. What it rules out is everything that needs new markup in the page chrome:
a feedback widget, a last-updated line, a copy-page-as-Markdown control, and any
footer card carrying more than the next page's title.

### Links between site pages

The site keeps documentation links inside `docs-site/`. The Quick Start card
points to `quick-start.md`, Self-Hosting points to `docker.md`, and all pages
use relative links for other site destinations. Repository files are described
as code paths when needed rather than linked to an external GitHub page.

Install commands still contain the URLs they must download from, and the local
runtime addresses remain clickable examples. Neither is a documentation
navigation link.

## What the READMEs become

The four sections are removed outright. Stubs saying "this section has moved"
would restore the table-of-contents noise the move exists to remove.

Two pointers replace them. The `Documentation` section is reordered to lead with
the site, with the design records that live under `docs/` below it. Quick Start
gains one closing line, because a reader who has just finished installing is
exactly the reader who needs to know where the rest went.

Deep links to `#self-hosting` and the three other anchors break for external
referrers. No in-repo reference depends on them: a tree-wide grep for
`README(\.zh-CN)?\.md#<anchor>` returns one hit, in
`benchmarks/proactivity_eval/README.md`, pointing at its own file rather than the
root README.

## Contracts that read the moved content

Four existing contracts touch this content. Their behaviour after the move is not
uniform, and one of them fails in a way no test reports.

**`test_readme_scope_canon.py`** breaks in both of its tests, and both are
repointed at the new page.

`test_repo_layout_table_equals_the_packages_on_disk` locates `## ... Repo layout`
in `README.md` by regex and asserts the table's backticked rows equal the
packages and modules under `raven/`; moving the section makes it fail at
`assert heading is not None`.

`test_key_directories_block_names_the_product_and_plugin_trees` searches the
whole of `README.md` for lines matching `^agents/\s` and `^plugins-dist/\s`.
Those lines are at 337 and 338, inside the Repo layout section rather than
Architecture, so they move with it. This one fails loudly rather than passing
vacuously, because its assertions are positive.

The table, `commitlint.config.cjs` and the tree are three independent things, and
this test is the only thing holding the first two together. `commitlint.config.cjs`
does not read the README: it calls `fs.readdirSync` over `raven/` at config-load
time, "so the enum cannot rot behind a refactor" (its own comment). The
`commit messages` CI job is therefore unaffected by the move.

**`test_container_exposes_no_engine_selector`** in `test_docker_runtime.py`
asserts `"RAVEN_ENGINE" not in root_readme` and the same for the Chinese README.
After the move these become vacuously true -- the READMEs no longer carry any
Docker prose, so they trivially satisfy a negative assertion -- while the Docker
prose itself sits in `docs-site/` unguarded. The assertion follows the content to
its new home in the same change. Without that, the migration silently retires a
guard while every test stays green.

**`test_readme_quickstart_matches_the_installer`** splits each README on `^## `
and asserts `git clone`, `./install.sh` and `RAVEN_LOCAL_SRC` appear in the Quick
Start block. All three strings were confirmed present inside that block in both
files by running the test's own extraction, so the move does not touch it.

**`test_docker_runtime.py`'s `CREDENTIAL_FILES`** is a hardcoded tuple of paths,
not derived from README prose, so the `.env.local` mention moving does not reach
it.

Three prose pointers also name the moved section and are updated with it:
`AGENTS.md` line 172 ("See the `Repo layout` section of `README.md`"), two
comments in `commitlint.config.cjs` (lines 17 and 33), and `raven/README.md`
line 3. All four line numbers were re-confirmed by grep at `6d8c36385`.

## Guards

Written before the content moves, each confirmed to fail first.

1. **The `RAVEN_ENGINE` assertion covers the new pages.** Prevents the vacuous
   pass described above. This is the one guard that prevents a regression rather
   than adding coverage.
2. **No external documentation links in site pages.** A page link that points
   to an external webpage fails, naming the file and the target.
3. **Every page has both languages.** `X.md` implies `X.zh.md` and the reverse.
   This is what keeps full parity from decaying at the first English-only page.
4. **The READMEs neither regrow the sections nor lose the pointer.** Both halves
   are asserted: the first alone would pass on a README that dropped the link
   too.
5. **The repo-layout table equals the packages on disk, in both languages.** The
   Chinese table is unguarded today -- `test_readme_scope_canon.py` reads
   `README.md` only -- and both tables currently hold 40 rows against 40 packages
   and modules on disk (measured by running the test's regex against both files
   and against `raven/`). Duplicating the table into two pages without
   duplicating the guard would carry that gap into the new location.
6. **A brand class a page uses is one the stylesheet defines, and the reverse.**
   The `em-*` names in the pages and the rules in `evermind.css` are a contract
   with no build step between them, so a rename on either side raises nothing and
   reddens nothing; the only symptom is an element rendering unstyled. The guard
   compares the two sets in both directions, which also catches a rule no page
   reaches. Confirmed to fail first from each side in turn.

## Build and deployment

Dependencies install into a dedicated `docs` group
(`uv add --group docs mkdocs-material mkdocs-static-i18n`) rather than `dev`,
because the four unit shards, coverage and lint all install `dev` and have no use
for a site generator.

Two Makefile targets follow the existing `lint-*` / `test-*` / `coverage-*`
naming: `docs-serve` and `docs-build`.

`docs-site/site/` is added to `.gitignore`. It is not ignored at `6d8c36385`, and
MkDocs writes its output there, so a local build would otherwise leave several
hundred HTML files stageable -- which AGENTS.md section 7 forbids committing, and
which `make check-large-files` only catches after the fact.

`.github/workflows/docs.yml` mirrors `ci.yml`'s trigger shape (`push` to `main`
plus `pull_request`, with a concurrency group). It builds on both events, and
deploys via `actions/deploy-pages` on `main` only, under
`environment: github-pages` with `pages: write` and `id-token: write`.

The repository's `main` ruleset carries no required status checks, so a failing
docs build does not block a merge. The gate is a human reading the checks.

GitHub Pages is not enabled on the repository (`has_pages: false` at
`6d8c36385`). It must be enabled with **Source: GitHub Actions**, not "Deploy
from a branch" -- under the branch setting the workflow's artifact is ignored and
nothing publishes, with no error surfaced. The resulting URL is
`https://evermind-ai.github.io/Raven/`, which `mkdocs.yml` must repeat as
`site_url` for canonical links and the sitemap to be correct. Moving later to a
custom subdomain means changing `site_url` and adding a CNAME together.

## Delivery order

Two pull requests, because enabling Pages is a repository setting rather than a
change a pull request can carry.

**First:** `docs-site/`, the workflow, the dependency group, and the `.gitignore`
entry. Neither README changes. Merging this publishes the site, which can then be
read: language switching, the eight rewritten links, and the rendering of each
page.

**Second:** the removal from both READMEs, the pointers to a URL already
confirmed reachable, the five guards, and the three prose-pointer updates.

Collapsing these into one pull request would have the READMEs point at a possibly
unopened Pages site from the moment it merged, with the fix gated on someone
else's availability. Splitting them confines that dependency to the gap between
the two.

Each pull request also reverts cleanly on its own: the first removes a directory
nothing references, the second restores the README content from history. Squash
merge makes each a single commit on `main`.

## Risk

The content carries no risk of loss; it is in git history and recoverable by
revert.

The exposure is the intermediate state: after the second pull request merges, a
site that goes down -- Pages disabled, workflow broken -- leaves the READMEs
without the content and pointing at a 404.

The site pages carry the complete user-facing content and link only to one
another. The repository remains the source for installation commands and code,
but reading the documentation does not require leaving the site.

The README sections move as whole blocks and their line counts are re-measured
against the branch base rather than carried forward, because `main` edits both
READMEs frequently: between `a0cedc452` and `c65c20598`, three merged pull
requests touched them, changing the totals without touching any of the four
sections that move.

## Known gaps left open

The self-hosting content asserts things about the tree that nothing verifies: the
Makefile targets `docker-build`, `docker-up`, `docker-down`, `install-deps` and
`build-ui`; the files `docker/README.md`, `docker/.env` and
`docker/docker-compose.yml`; and port 18793. All were confirmed true at the time
of writing (`grep -E "^<target>:" Makefile`, path existence, and
`grep 1879[0-9] docker/docker-compose.yml`), and none is held by a test.

This is left as it stands. The claims are equally unguarded before and after the
move, so closing the gap is separable work rather than part of relocating the
content. It is recorded here so a later reader knows it was seen and left, not
missed.

Search spans both languages from one index. `mkdocs-static-i18n` 1.3.1 builds a
single `search/search_index.json` for the whole site: `reconfigure_search_index`
stacks every language's entries into it, and `reconfigure_search` only drops
entries that are identical in title and text, which is the untranslated case.
A reader searching the English site therefore sees Chinese pages among the
results. Splitting the index needs a per-language index file and a theme
override to point each language's search worker at its own -- and the override
is an `.html` partial, which section 7 blocks under `docs-site/` for the same
reason as the logo. Left as it stands.
