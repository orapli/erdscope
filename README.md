# erdscope

[![CI](https://github.com/orapli/erdscope/actions/workflows/ci.yml/badge.svg)](https://github.com/orapli/erdscope/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/erdscope)](https://pypi.org/project/erdscope/)

Generate a **self-contained, interactive ER diagram** — and an **Excel table-definition
workbook** — from a live MySQL, PostgreSQL, or SQLite database, with a single-file,
zero-dependency Python CLI.

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
- **Application code** (`--models`: Rails / Prisma / Django) — adds association semantics
  the database cannot express (`has_many :through`, polymorphic, ...), and can stand on
  its own when there is no DB to point at.
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
can find. See the [Config file chapter of the manual](https://orapli.github.io/erdscope/manual.html#config-file) for the full key
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

## What you get

Feature highlights — each link goes to the relevant [manual](https://orapli.github.io/erdscope/manual.html) chapter:

- **Database truth** — tables, columns (full SQL types, defaults, extras), table and
  column comments, indexes, and real FK constraints, read from the database catalog
  (`information_schema` on MySQL, `pg_catalog` on PostgreSQL)
- **Code semantics on top** — `--models` merges Rails / Prisma / Django associations;
  declared, DB-FK, and inferred edges stay [visually distinct](https://orapli.github.io/erdscope/manual.html#viewer-edges)
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

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The IR builders and the Excel writer are covered by pure unit tests; the overlay
parsers have minimal fixtures under `tests/fixture_*`. No database is required to
run the tests.

`tests/test_e2e.py` drives the generated HTML's client-side JS (grid layout,
multi-select align/distribute, drag-to-snap, Auto-tidy) in a real headless browser. It's optional and skips
itself if not set up:

```bash
pip install playwright && playwright install chromium
python3 -m unittest tests.test_e2e -v
```

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
