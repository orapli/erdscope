# erdscope

Generate a **self-contained, interactive ER diagram** — and an **Excel table-definition
workbook** — from a live MySQL database, with a single-file, zero-dependency Python CLI.

The database is the source of truth (tables, columns, comments, indexes, real foreign
keys). Application code (Rails / Prisma / Django) can optionally be layered on top to
add association semantics the database cannot express (`has_many :through`,
polymorphic, ...).

## Demo

**[Try the live demo →](https://orapli.github.io/erdscope/)** — a small e-commerce
schema with comments, indexes, real FKs and an inferred relation. Everything below
is one self-contained HTML file.

[![erdscope demo](docs/screenshot.png)](https://orapli.github.io/erdscope/)

Regenerate it anytime with `python3 docs/gen_demo.py`.

**[Read the user manual →](https://orapli.github.io/erdscope/manual.html)** — installation,
CLI/config reference, a full viewer guide, and troubleshooting.

## Usage

```bash
python3 erd.py mysql://readonly@127.0.0.1:3306/myapp_production -o erd.html

# enrich with association semantics parsed from application code (optional)
python3 erd.py mysql://readonly@127.0.0.1:3306/myapp_production \
        --models /path/to/rails/app -o erd.html

# also write a table-definition workbook
python3 erd.py mysql://readonly@127.0.0.1:3306/myapp_production \
        --excel table_definitions.xlsx -o erd.html
```

Behind a bastion? Open an SSH tunnel first and point at localhost:

```bash
ssh -N -L 3307:db-host:3306 bastion &
python3 erd.py mysql://readonly@127.0.0.1:3307/myapp_production -o erd.html
```

Use a read-only account, and leave the password out of the URL — if it's not in
`MYSQL_PWD` either, you'll be prompted for it (hidden input, never touches argv or
shell history). See [Dependencies](#dependencies) below for how the DB connection
itself is made.

### Options

| Option | Description |
|---|---|
| `-o FILE` | Output HTML path (default: `erd.html`) |
| `--models PATH` | Merge associations parsed from code: a Rails project (or `app/models` dir), a `schema.prisma`, or a Django project — auto-detected |
| `--excel FILE.xlsx` | Also write a table-definition workbook: an overview sheet plus one sheet per table (columns, defaults, keys, comments, indexes, associations) |
| `--excel-template FILE.xlsx` | Override the workbook's colors/fonts/borders from a template `.xlsx` — see `excel-template.xlsx` and its `Styles` sheet for the 5-cell contract (default: built-in styling) |
| `--max-rows N` | Max column rows shown per table (default: 15; the rest scroll) |
| `--only 'user*,post*'` | Include only tables matching the glob pattern(s) |
| `--exclude '*_logs'` | Exclude tables matching the glob pattern(s) |
| `--infer-fk` | Guess relations from `*_id` column names when no real association/FK backs them (off by default — see below) |
| `--table-map 'Widget=crm_widgets'` | Rails only: override a model's table when static analysis can't determine it (e.g. `table_name` set inside a concern that lives in a gem, not the app). Repeatable; comma-separated lists accepted |
| `--config PATH` | Load defaults from a config file instead of repeating flags — see below. Auto-discovered as `.erdscope.json`/`.yml`/`.yaml` in the current directory if not given |
| `--no-config` | Skip config auto-discovery even if `.erdscope.*` exists in the cwd |

## Config file

Once the flag list above gets long, put it in a config file instead — `.erdscope.json`
(or `.yml`/`.yaml` with PyYAML) next to where you run the tool is picked up automatically.
Most keys mirror a CLI option (an explicit flag always wins over the config); the DB
connection is config-only as `host`/`port`/`user`/`database` — deliberately with no
password field — and `relations` manually declares relations no FK, code, or inference
can find. See the [Config file chapter of the manual](https://orapli.github.io/erdscope/manual.html#config-file) for the full key
list and semantics, and [`erdscope.example.yml`](erdscope.example.yml) for a fully
annotated sample based on the live demo's schema.

## Dependencies

`erd.py` runs with **zero required dependencies** — everything below is optional, and
the tool degrades gracefully (falls back, or fails with a clear message) when a piece
is missing.

| Library | Used for | If not installed |
|---|---|---|
| [PyMySQL](https://pypi.org/project/PyMySQL/) | The DB connection | Falls back to shelling out to the `mysql` CLI (must be on `PATH`) |
| [PyYAML](https://pypi.org/project/PyYAML/) | Reading a `.yml`/`.yaml` config file | A `.json` config still works with no dependency; pointing `--config`/auto-discovery at a `.yml`/`.yaml` file without PyYAML installed exits with a clear error |

Excel output (`--excel`) needs neither — it's written directly via the stdlib
`zipfile`/XML, not a spreadsheet library.

Test-only, and only if you run that particular suite:

| Library | Used for |
|---|---|
| [openpyxl](https://pypi.org/project/openpyxl/) | Roundtrip-verifying `--excel` output in the unit tests (`tests/test_erd.py`); that one test skips itself if it's missing |
| [Playwright](https://playwright.dev/python/) | The browser E2E suite (`tests/test_e2e.py`) — see [Tests](#tests) below |

## What you get

Feature highlights — each link goes to the relevant [manual](https://orapli.github.io/erdscope/manual.html) chapter:

- **Database truth** — tables, columns (full SQL types, defaults, extras), table and
  column comments, indexes, and real FK constraints, read from `information_schema`
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
- **Extras** — dark mode, print stylesheet, resizable/collapsible panes

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
documented at the top of `erd.py`. Adding PostgreSQL support means adding one
`parse_postgres()` that produces that shape.

## License

MIT
