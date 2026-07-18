# erdscope

[![CI](https://github.com/orapli/erdscope/actions/workflows/ci.yml/badge.svg)](https://github.com/orapli/erdscope/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/erdscope)](https://pypi.org/project/erdscope/)

**Interactive ER diagrams and documented schema definitions.** Generate a
**self-contained, interactive ER diagram** — and an **Excel table-definition
workbook** — from a live MySQL, PostgreSQL, or SQLite database, with a single-file,
zero-dependency Python CLI. Turn an ER diagram into a documented schema: attach design
decisions, operational rules, and ADR links to tables and relationships with config
[`notes:`](#notes-attach-design-decisions-to-the-diagram) — plain text and http(s) links
only, validated against the real schema so a note can never point at something that
doesn't exist — and draw a rounded, titled frame around a set of related tables with config
[`groups:`](#groups-draw-a-frame-around-related-tables).

```bash
pip install erdscope
erdscope demo   # no database of your own? try it right now — opens a sample diagram

erdscope mysql://readonly@127.0.0.1:3306/myapp_production -o erd.html
erdscope postgres://readonly@127.0.0.1:5432/myapp_production -o erd.html
erdscope sqlite:///path/to/app.db -o erd.html
```

**Three input sources — any one of them is enough** (a database is no longer required):

- **Database** (MySQL / PostgreSQL / SQLite) — the source of truth for tables, columns,
  comments, indexes, and real foreign keys.
- **Application code** (`--models`: Rails / Prisma / Django, or a Rails `db/schema.rb` —
  see [Typed input sources](#typed-input-sources-sources) below) — adds association
  semantics the database cannot express (`has_many :through`, polymorphic, ...), and can
  stand on its own when there is no DB to point at.
- **Config file** (`tables:`) — declare or patch a schema by hand: add tables, columns,
  indexes, and associations, or override and delete what the DB or code got wrong.

They merge in that order — **database → code → config**, each layer refining the previous
(the database wins on physical facts like column types; code and config win on
associations and logical names; config always has the final say). With no database URL,
nothing is connected and no password is prompted.

## Demo

**[Try the live demo →](https://orapli.github.io/erdscope/)** — a small e-commerce
schema with comments, indexes, real FKs and an inferred relation. Everything below
is one self-contained HTML file.

[![erdscope demo](docs/screenshot.png)](https://orapli.github.io/erdscope/)

Regenerate it anytime with `python3 docs/gen_demo.py`.

**[Read the user manual →](https://orapli.github.io/erdscope/manual.html)** — installation,
CLI/config reference, a full viewer guide, and troubleshooting. ([日本語版 →](https://orapli.github.io/erdscope/manual.ja.html))

## Install & usage

`pip install erdscope` (or `pipx install erdscope`) gives you the `erdscope` command.
Prefer not to install anything? `erd.py` is a single, dependency-free file — grab it
and run it with any Python 3.9+:

```bash
curl -O https://raw.githubusercontent.com/orapli/erdscope/main/erd.py
python3 erd.py ...   # identical to the erdscope command below
```

No database of your own to point it at yet? `erdscope demo` builds a small sample
e-commerce SQLite database in a temp directory, generates the diagram, and opens it in
your browser — nothing to download or set up:

```bash
erdscope demo
```

It's a normal run under the hood, so every other flag still applies
(`erdscope demo --excel defs.xlsx`, `erdscope demo --only 'order*'`, ...); add
`--no-open` to skip launching the browser.

```bash
erdscope mysql://readonly@127.0.0.1:3306/myapp_production -o erd.html

# PostgreSQL: same thing (schema defaults to public; override with ?schema=name)
erdscope postgres://readonly@127.0.0.1:5432/myapp_production -o erd.html

# SQLite: just point at the file (no server, nothing to install — uses stdlib sqlite3)
erdscope sqlite:///path/to/app.db -o erd.html

# enrich with association semantics parsed from application code (optional)
erdscope mysql://readonly@127.0.0.1:3306/myapp_production \
        --models /path/to/rails/app -o erd.html

# also write a table-definition workbook
erdscope mysql://readonly@127.0.0.1:3306/myapp_production \
        --excel table_definitions.xlsx -o erd.html

# no database — generate straight from application code
erdscope --models /path/to/rails/app -o erd.html

# no database — generate from a hand-written config schema (see Config file below)
erdscope --config schema.yml -o erd.html

# multiple code sources merge in order, later wins (--models is repeatable)
erdscope --models /path/to/rails/app --models /path/to/schema.prisma -o erd.html
```

Cloned the repository instead of installing from PyPI? A ready-to-run SQLite sample
(the same schema `erdscope demo` uses) also ships committed in
[`examples/`](examples/) — `python3 erd.py sqlite:///examples/demo_shop.db -o shop.html`.
See [examples/README.md](examples/README.md).

Behind a bastion? Open an SSH tunnel first and point at localhost:

```bash
ssh -N -L 3307:db-host:3306 bastion &
erdscope mysql://readonly@127.0.0.1:3307/myapp_production -o erd.html
```

Use a read-only account, and leave the password out of the URL — if it's not in
`MYSQL_PWD` either, you'll be prompted for it (hidden input, never touches argv or
shell history). See [Dependencies](#dependencies) below for how the DB connection
itself is made.

### Options

| Option | Description |
|---|---|
| `demo` | Positional value: generate from a bundled sample database instead of a real one — no database of your own needed. Every other flag below still applies |
| `-o FILE` | Output HTML path (default: `erd.html`; `erd_demo.html` for `demo`, so it never overwrites a real run's output) |
| `--models PATH` | Merge associations parsed from code: a Rails project (or `app/models` dir), a `schema.prisma`, or a Django project — auto-detected. **Repeatable** — multiple sources merge in the order given (later wins). Usable with no database URL |
| `--excel FILE.xlsx` | Also write a table-definition workbook: an overview sheet plus one sheet per table (columns, defaults, keys, comments, indexes, associations) |
| `--excel-template FILE.xlsx` | Override the workbook's colors/fonts/borders from a template `.xlsx` — see `excel-template.xlsx` and its `Styles` sheet for the 5-cell contract (default: built-in styling) |
| `--max-rows N` | Max column rows shown per table (default: 15; the rest scroll) |
| `--only 'user*,post*'` | Include only tables matching the glob pattern(s) |
| `--exclude '*_logs'` | Exclude tables matching the glob pattern(s) |
| `--infer-fk` | Guess relations from `*_id` column names when no real association/FK backs them (off by default — see below) |
| `--table-map 'Widget=crm_widgets'` | Rails only: override a model's table when static analysis can't determine it (e.g. `table_name` set inside a concern that lives in a gem, not the app). Repeatable; comma-separated lists accepted |
| `--config PATH` | Load defaults from a config file instead of repeating flags — see below. Auto-discovered as `.erdscope.json`/`.yml`/`.yaml` in the current directory if not given |
| `--no-config` | Skip config auto-discovery even if `.erdscope.*` exists in the cwd |
| `--no-open` | Skip automatically opening a browser after generating. Only relevant to `demo` (which opens one by default); accepted but has no effect on a normal run |

## Config file

Once the flag list above gets long, put it in a config file instead — `.erdscope.json`
(or `.yml`/`.yaml` with PyYAML) next to where you run the tool is picked up automatically.
Most keys mirror a CLI option (an explicit flag always wins over the config); the DB
connection is config-only as `engine`/`host`/`port`/`user`/`database` — deliberately with
no password field — and `relations` manually declares relations no FK, code, or inference
can find. `engine` can also be `"sqlite"`, in which case `database` is a local file path
(not a database name) and `host`/`port`/`user` don't apply. See the [Config file chapter of the manual](https://orapli.github.io/erdscope/manual.html#config-file) for the full key
list and semantics, and [`erdscope.example.yml`](erdscope.example.yml) for a fully
annotated sample based on the live demo's schema.

### Config as a schema source

Beyond settings, the config can carry a **`tables:`** section that is itself a full input
source — enough to generate a diagram with no database and no code at all, or to patch
what the other sources produce. It merges as the highest-priority layer, so it can add
new tables/columns/indexes/associations, override attributes, or delete them:

```yaml
title: billing
tables:
  customers:
    comment: Customer accounts
    primary_key: id
    columns:
      - { name: id,    type: bigint,  primary: true }
      - { name: email, type: varchar, nullable: false, comment: Login address }
    associations:
      - { type: has_many, name: invoices, target: invoices }
  invoices:
    columns:
      - { name: id,          type: bigint, primary: true }
      - { name: customer_id, type: bigint }
    associations:
      - { type: belongs_to, name: customer, target: customers, foreign_key: customer_id }
```

Patch an existing DB/code result instead of declaring from scratch — override a column,
delete a stale one, drop a whole table, or replace a table's column list wholesale:

```yaml
tables:
  orders:
    columns:
      - { name: status,      comment: Order status }   # override just this attribute
      - { name: legacy_flag, drop: true }              # delete a column
  temp_scratch:
    drop: true                                         # delete a whole table
  reports:
    columns_mode: replace                              # discard lower-layer columns first
    columns:
      - { name: id,   type: bigint, primary: true }
      - { name: body, type: text }
```

Config is validated in two passes: **syntactically at load** (unknown keys, bad types,
malformed operations) and **semantically at run time** (a `drop`/reference must point at
something that actually exists once all sources are merged) — typos never pass silently.
The full `tables:` schema, the `drop`/`replace` operations, and the precedence rules are
documented in the [manual](https://orapli.github.io/erdscope/manual.html#config-file).

### Typed input sources (`sources:`)

`--models`/config `models` auto-detect what kind of project a path is. The config-only
**`sources:`** list is the typed alternative: each entry names its own `type`, so nothing
needs detecting and several inputs can be declared explicitly and unambiguously —
including **`rails.schema`**, which statically parses a Rails `db/schema.rb` file
(columns, indexes, real foreign keys) with **no live database and no Ruby execution**:

```yaml
version: 1
sources:
  - id: schema
    type: rails.schema
    path: db/schema.rb
  - id: app
    type: rails.models         # every registered overlay gets its own <name>.models type
    path: app/models
```

(The optional top-level `version: 1` is a config-format marker — currently the only
supported value, with no other effect; it's for future config-format changes to key off.)

A typed source that parses **nothing** — say, a Prisma project accidentally declared as
`rails.models` — is a hard error naming the source id and the layout the type expected,
never a silently empty diagram. If an empty result is genuinely intended (a scaffolded
but still-empty `app/models`, for example), opt in per source with `allow_empty: true`;
a `rails.project` entry passes the flag down to both of its expanded halves.

A **`rails.project`** entry is a macro for a whole Rails app root: it expands to both the
`rails.schema` (`<root>/db/schema.rb`) and `rails.models` (`<root>/app/models`) halves,
whichever exist —

```yaml
sources:
  - id: app
    type: rails.project
    path: ../myapp
```

is equivalent to declaring both `rails.schema` and `rails.models` entries above. A
`schema.rb`-derived foreign key merges as a **schema FK** (a teal badge in the viewer,
distinct from a live `DB FK`), and the merge authority order extends to
**Config > live DB > rails.schema > models** (physical facts) — a `schema.rb` dump is
closer to the real database than code is, but a live DB read still wins when both are
present. See the [Input sources chapter of the manual](https://orapli.github.io/erdscope/manual.html#input-sources)
for the full `sources[]` reference.

### Notes: attach design decisions to the diagram

Config **`notes:`** attaches short, plain-text write-ups — design decisions, operational
rules, ADR links — to a table, a specific relation, or the whole diagram. Notes never
touch the schema itself (no effect on columns, associations, or merge precedence); they're
a read-only sidecar validated against the final merged schema and rendered next to it:

```yaml
notes:
  - id: user-retention
    target: { type: table, table: users }
    title: Retention policy
    text: Suspended accounts are kept for 1 year, then anonymized.
    links:
      - { label: ADR-004, url: https://example.com/adr/004 }

  - id: order-ownership
    target: { type: relation, source_table: orders, target_table: users, foreign_key: user_id }
    text: Orders are kept after a user is anonymized (financial record-keeping).

  - id: diagram-conventions
    target: { type: global }
    title: How to read this diagram
    text: The dotted amber edge is an inferred relation, not a real FK.
```

- Every note needs a config-unique `id` and non-empty `text`; `title` and `links` are
  optional. Link `url`s must be `http://` or `https://` — anything else (`javascript:`,
  `data:`, a bare string) is rejected at load time.
- `target.type` is `table`, `relation`, or `global`. A `relation` note is identified by
  `source_table` (the side that **holds** the association — the `belongs_to`/FK-holding
  side for a `belongs_to`, the owning side for a `has_many`) and `target_table`;
  `foreign_key`/`name`/`assoc_type`/`through`/`polymorphic` are optional narrowing keys for
  when a table has more than one relation to the same target (`assoc_type` is the
  association kind — `has_many`/`belongs_to`/`has_one`/`has_and_belongs_to_many` — which
  tells apart, say, a `has_many` and a `has_one` that share a name and target). A note that
  matches no relation, or more than one, is a hard error naming the note's `id` — never a
  silent guess.
- Validation runs **twice**, like `tables:` above: syntax at load time, then semantically
  against the schema that results **after** DB/`--models`/config `tables:` are all merged
  — so a note can reference a table or relation that config itself adds, and a note on
  something config *removes* is correctly an error.
- Rendered as plain, HTML-escaped text only — no Markdown, no raw HTML, no scripts — in
  the table's detail panel, next to the matching relation, or in the diagram's legend for
  a `global` note. Notes are also searchable (title, text, link labels). A note whose
  table is hidden from the current view simply doesn't appear in the (now absent) detail
  panel; a `global` note's legend entry is unaffected.
- `write_excel` accepts the same notes but doesn't use them yet in this release — an Excel
  Notes sheet is a possible future addition, not implemented today.

See the [Notes chapter of the manual](https://orapli.github.io/erdscope/manual.html#notes)
for the full reference.

### Groups: draw a frame around related tables

Config **`groups:`** draws a rounded frame with a title around a set of related tables in
the diagram — a lightweight way to call out a domain ("Billing", "Orders") without changing
the schema or the layout. Like `notes:`, it's a read-only sidecar validated against the
final merged schema:

```yaml
groups:
  - id: billing
    title: Billing
    tables: [invoices, payments, coupons]
    color: "#0d9488"

  - id: catalog
    tables: [products, categories, product_categories]
```

- Every group needs a config-unique `id` and non-empty `tables`; `title` (defaults to `id`)
  and `color` (a hex string, e.g. `#0d9488`) are optional.
- A table may belong to **at most one** group — claiming the same table from two groups is
  a hard error naming both group `id`s and the table, not a silently-picked winner. There's
  no support for overlapping/nested groups in this release.
- Validation runs twice, like `notes:`/`tables:` above: syntax at load time, then
  semantically against the final merged schema, so a group can reference a table config
  itself adds, and a group naming something config *removes* is correctly an error.
- Purely visual: groups never affect layout, merge precedence, or associations — the frame
  is drawn around wherever its member tables already ended up. In the viewer, drag a
  group's title to move every member together; a "Groups" toolbar toggle shows/hides the
  frames, and both PNG and SVG exports include them. `--only`/`--exclude` narrow a group's
  membership down to the surviving tables, dropping the group entirely if none remain.

## Dependencies

`erd.py` runs with **zero required dependencies** — everything below is optional, and
the tool degrades gracefully (falls back, or fails with a clear message) when a piece
is missing. If you installed via pip, extras pull them in for you:
`pip install 'erdscope[mysql]'` (PyMySQL), `'erdscope[postgres]'` (psycopg),
`'erdscope[yaml]'` (PyYAML), or `'erdscope[all]'`.

| Library | Used for | If not installed |
|---|---|---|
| [PyMySQL](https://pypi.org/project/PyMySQL/) | MySQL connections | Falls back to shelling out to the `mysql` CLI (must be on `PATH`) |
| [psycopg](https://pypi.org/project/psycopg/) (or psycopg2) | PostgreSQL connections | Falls back to shelling out to the `psql` CLI (must be on `PATH`) |
| _(none)_ | **SQLite** connections (`sqlite:///file.db`) | Uses Python's built-in `sqlite3` — always available, nothing to install or fall back to |
| [PyYAML](https://pypi.org/project/PyYAML/) | Reading a `.yml`/`.yaml` config file | A `.json` config still works with no dependency; pointing `--config`/auto-discovery at a `.yml`/`.yaml` file without PyYAML installed exits with a clear error |

Excel output (`--excel`) needs none of these — it's written directly via the stdlib
`zipfile`/XML, not a spreadsheet library.

Test-only, and only if you run that particular suite:

| Library | Used for |
|---|---|
| [openpyxl](https://pypi.org/project/openpyxl/) | Roundtrip-verifying `--excel` output in the unit tests (`tests/test_erd.py`); that one test skips itself if it's missing |
| [Playwright](https://playwright.dev/python/) | The browser E2E suite (`tests/test_e2e.py`) — see [Tests](#tests) below |

## Verified versions

The input formats erdscope parses change rarely, so exact versions matter less than
they might seem — newer releases are expected to keep working, though constructs
beyond the tested ones may be ignored rather than parsed. For the record, this is what
each input is actually verified against:

| Input | Verified against |
|---|---|
| MySQL | **8.4** — real-server integration tests in CI (`information_schema`); developed against 8.x |
| PostgreSQL | **16** — real-server integration tests in CI (`pg_catalog`/`information_schema`) |
| SQLite | the `sqlite3` module bundled with CPython (any supported 3.x) |
| Rails `schema.rb` | the format Rails **7.x / 8.x** writes (`ActiveRecord::Schema[7.x]`, and the classic un-versioned header) |
| Rails models | the association DSL as of Rails **7.x** — `has_many`/`has_one`/`belongs_to`/`has_and_belongs_to_many`, `through:`, `polymorphic:`, STI, concerns, custom base classes; also exercised against Mastodon's real codebase. Dynamically computed definitions (and `structure.sql`) are out of scope |
| Prisma | the schema language as of Prisma **5 / 6** — `@map`/`@@map`, enums, named relations, implicit and explicit m2m, self-relations, composite `@@id`/`@@unique`, `@@schema` |
| Django | models as of Django **4.2 / 5.x** — FK / OneToOne / M2M (incl. `through=`), abstract bases, `db_table`/`db_column`, `GenericForeignKey` (kept as a polymorphic marker); a swappable `AUTH_USER_MODEL` FK keeps its column and skips the edge |
| Python | 3.9+ (`requires-python`); CI runs the latest CPython 3.x |

## What you get

Feature highlights — each link goes to the relevant [manual](https://orapli.github.io/erdscope/manual.html) chapter:

- **Database truth** — tables, columns (full SQL types, defaults, extras), table and
  column comments, indexes, and real FK constraints, read from the database catalog
  (`information_schema` on MySQL, `pg_catalog` on PostgreSQL)
- **Code semantics on top** — `--models` merges Rails / Prisma / Django associations;
  declared, DB-FK, schema FK (a statically-parsed `rails.schema` foreign key), and
  inferred edges stay [visually distinct](https://orapli.github.io/erdscope/manual.html#viewer-edges)
- **[Interactive exploration](https://orapli.github.io/erdscope/manual.html#viewer-guide)** — focus with depth and dependency
  direction, two-level hiding, table *and column* search (with regex/case toggles), a
  non-filtering Highlight search that survives into exports, named views, share links
- **[Readable layouts](https://orapli.github.io/erdscope/manual.html#viewer-layout)** — viewport-aware packing with crossing
  reduction, drag-to-snap with guide lines, multi-select align/distribute, Auto-tidy,
  layout undo/redo
- **[Exports](https://orapli.github.io/erdscope/manual.html#exports)** — PNG (2x), SVG, Mermaid, and PlantUML, each with its own
  copy and download buttons and image options, plus the Excel workbook
  (customizable via `--excel-template`)
- **[Logical names](https://orapli.github.io/erdscope/manual.html#viewer-names)** — a table's DB comment doubles as a searchable
  logical name (e.g. `users（Customer accounts）`), with independent display modes for
  the live view and exports
- **Extras** — a built-in `?` shortcuts/help popup, dark mode, print stylesheet,
  resizable/collapsible panes
- **[Notes](#notes-attach-design-decisions-to-the-diagram)** — attach design decisions,
  operational rules, and ADR links to a table, a relation, or the whole diagram, validated
  against the real schema and searchable alongside tables/columns
- **[Groups](#groups-draw-a-frame-around-related-tables)** — draw a rounded, titled frame
  around a set of related tables to call out a domain, purely visual, draggable by its
  title, with its own toolbar toggle and export support

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The IR builders and the Excel writer are covered by pure unit tests; the overlay
parsers have fixtures under `tests/fixture_*` — basic plus advanced constructs per
framework — and every input provider is additionally held to one shared contract
(`tests/test_provider_contract.py`): typed dispatch, standalone HTML/Excel generation,
merging over a DB layer, 1:N / 1:1 / M:N / self-reference, association provenance, and
empty-input diagnostics, all over the same small domain. No database is required to
run the tests.

`tests/test_e2e.py` drives the generated HTML's client-side JS (grid layout,
multi-select align/distribute, drag-to-snap, Auto-tidy) in a real headless browser. It's optional and skips
itself if not set up:

```bash
pip install playwright && playwright install chromium
python3 -m unittest tests.test_e2e -v
```

## Performance

Measured against synthetic SQLite schemas (`benchmarks/gen_schema.py`, ~2 FK edges per
table) at three sizes:

| Tables | Edges | CLI gen (median) | HTML size | Initial paint (median) | Re-layout (median) | Python RSS | JS heap |
|---|---|---|---|---|---|---|---|
| 100   | 190   | 0.05 s | 292 KB  | 91 ms  | 37 ms  | 37 MB | 2.0 MB |
| 300   | 592   | 0.06 s | 533 KB  | 206 ms | 131 ms | 42 MB | 1.9 MB |
| 1,000 | 2,015 | 0.11 s | 1.35 MB | 1.18 s | 1.04 s | 59 MB | 3.2 MB |

Notes: measured in a sandboxed Linux environment against headless Chromium
(Playwright), not dedicated hardware — treat these as relative/order-of-magnitude
numbers rather than absolute guarantees for your machine. "CLI gen" and the browser
timings are each the median of 3 runs; Python RSS/JS heap are single best-effort
samples. Generation itself (`erd.py sqlite:///... -o out.html`) stays fast at every
size tested — SQLite parsing isn't the bottleneck.

**Recommendation:** the constraint at scale is the browser-side interactive
re-layout, not generation. Linear interpolation between the 300- and 1,000-table
measurements above puts the 1-second crossing at roughly 970 tables; we round that
down to **~900 tables** as a deliberately conservative rule of thumb, since these
are single-environment numbers (initial paint itself stays comfortably under 3
seconds through 1,000 tables). Past that rough threshold, narrow the diagram with
`--only`/`--exclude` instead of
rendering the whole schema at once — see the [manual's Large
schemas section](https://orapli.github.io/erdscope/manual.html#large-schemas) for
details.

Reproduce or re-run at other sizes with:

```bash
python3 benchmarks/gen_schema.py --tables 300 --out /tmp/bench_300.db
python3 benchmarks/run_bench.py --tables 100,300,1000   # generates, benchmarks, prints JSON + a summary table
```

`run_bench.py` is a manual script (not part of the `unittest` suite) — see its
docstring for the full methodology and the Playwright venv it expects.

## Extending

Everything downstream (UI, layouts, exports) consumes the intermediate representation
documented at the top of `erd.py`. Both input layers are pluggable: a **database
adapter** turns a URL scheme into that shape, and a **framework overlay** turns an
application-code project into it. Each is a small class registered under the scheme /
project kind it handles, so adding one never touches the dispatch code.

### Custom adapters and overlays (a plugin file)

Write a plain Python file that subclasses the base and registers itself, then load it
with `--adapter path/to/plugin.py` (or config `adapters: [...]`). No need to rebuild
`erd.py` — the plugin registers into the running process, and a plugin works the same
against the single-file `erd.py` or a `pip install erdscope`.

```python
# my_sqlite.py — a custom database adapter
from erd import DBAdapter, register_adapter, mysql_ir

@register_adapter
class SqliteAdapter(DBAdapter):
    schemes = ('sqlite',)     # the URL scheme(s) it answers to
    name = 'sqlite'           # provider id recorded in the output
    label = 'SQLite'          # pretty name for the progress line

    def fetch(self, url):
        # ...read the schema for `url` and return the IR (the `tables` dict).
        # mysql_ir() builds it from information_schema-shaped rows for you.
        return mysql_ir(table_rows, col_rows, fk_rows, index_rows)
```

```bash
python3 erd.py sqlite:///app.db --adapter my_sqlite.py -o erd.html
```

A **framework overlay** is the same idea against a `--models` path:

```python
# my_framework.py
from erd import FrameworkOverlay, register_overlay, make_provider_result

@register_overlay
class SequelizeOverlay(FrameworkOverlay):
    name = 'sequelize'
    priority = 5              # lower detect()s first; first match wins

    def detect(self, root):
        return root.is_dir() and (root / 'models' / 'index.js').exists()

    def build(self, root, table_map):
        tables = ...          # parse `root` into the IR
        return make_provider_result('framework', 'sequelize', tables, location=str(root))
```

The built-in `db/mysql.py`, `db/postgres.py` and `frameworks/{rails,prisma,django}.py`
are the working examples of both patterns.

### Building `erd.py`

The shipped `erd.py` is a **build artifact**: the development source lives under
`src/erdscope/`. The Python is organised into concern-named fragments (`ir.py`,
`merge.py`, `providers.py`, `exporters.py`, `config.py`, `cli.py`, …) plus two
self-assembling **folders** — `db/` (the DB adapters) and `frameworks/` (the overlays)
— whose files are all included automatically (`base.py` first, then sorted), so adding a
built-in adapter or overlay is just dropping a new file in the folder. They are
concatenated in order, and the ~3,600-line embedded viewer (HTML/CSS/JS) lives in
`viewer.html`, out of the Python. Edit the relevant file, then regenerate the single file:

```bash
python3 tools/build_single_file.py          # rewrites erd.py from the source
python3 tools/build_single_file.py --check   # CI-style check that erd.py is in sync
```

The fragments are an amalgamation of one flat module (SQLite-style) — no cross-module
imports — and the viewer is inlined into a one-line sentinel, so the whole build is pure
textual assembly and `erd.py` stays a self-contained, zero-dependency single file:
grabbing and running it, or `pip install erdscope`, is unchanged. CI runs the `--check`
above, so a hand-edit of `erd.py` or a forgotten rebuild fails the build.

## License

MIT
