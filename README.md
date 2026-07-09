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

[![erdscope demo](docs/screenshot.svg)](https://orapli.github.io/erdscope/)

Regenerate it anytime with `python3 docs/gen_demo.py`.

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
| `--max-rows N` | Max column rows shown per table (default: 15; the rest scroll) |
| `--only 'user*,post*'` | Include only tables matching the glob pattern(s) |
| `--exclude '*_logs'` | Exclude tables matching the glob pattern(s) |
| `--infer-fk` | Guess relations from `*_id` column names when no real association/FK backs them (off by default — see below) |
| `--table-map 'Widget=crm_widgets'` | Rails only: override a model's table when static analysis can't determine it (e.g. `table_name` set inside a concern that lives in a gem, not the app). Repeatable; comma-separated lists accepted |
| `--config PATH` | Load defaults from a config file instead of repeating flags — see below. Auto-discovered as `.erdscope.json`/`.yml`/`.yaml` in the current directory if not given |
| `--no-config` | Skip config auto-discovery even if `.erdscope.*` exists in the cwd |

## Config file

Once the flag list above gets long, put it in a file instead — `.erdscope.json` next to
where you run the tool is picked up automatically (no `--config` needed). JSON works
with zero dependencies; YAML works too if PyYAML happens to be installed. Most keys
mirror a CLI option above (snake_case); `host`/`port`/`user`/`database` (the DB
connection) and `relations` (manual FK declarations) are config-only, with no CLI flag
equivalent:

```jsonc
{
  // connection, broken into parts rather than one mysql:// URL string —
  // there's no password field, on purpose (see below). One file per
  // database (erdscope.staging.json, erdscope.prod.json, ...) is the
  // intended way to point --config at different targets.
  "host": "127.0.0.1",
  "port": 3306,
  "user": "readonly",
  "database": "myapp_production",

  "output": "erd.html",
  "models": "../myapp",
  "max_rows": 15,
  "infer_fk": true,
  "only": ["user*", "post*"],
  "table_map": { "Widget": "crm_widgets" },

  // manually declare a relation no source (real FK, *_id inference, or
  // --models code parsing) can find — an oddly-named column, or one a
  // gem-provided concern/dynamic association hides from static analysis.
  // Works standalone, with no --models at all: you can build a complete
  // relation graph from a config file alone, e.g. before the models exist.
  "relations": [
    { "table": "orders", "column": "buyer_code", "references": "users" },
    { "table": "profiles", "column": "person_ref", "references": "users",
      "one_to_one": true, "name": "owner" }
  ]
}
```

An explicit CLI flag always wins over the same config key, replacing it entirely
(list-valued keys like `only` are not merged with the config's). There's deliberately
no `password`/`url` key — `host`/`port`/`user`/`database` are separate fields specifically
so there's nowhere to paste a password into. Leave it out of the config the same way
you would the CLI: `MYSQL_PWD`, `~/.my.cnf`, or the interactive prompt. If the CLI
argument is given, it wins over the config's connection fields entirely.

See [`erdscope.example.yml`](erdscope.example.yml) for a fully annotated sample (based
on the live demo's schema) with every key explained and the situational ones commented
out — copy it to `.erdscope.yml`/`.erdscope.json` and adapt.

Precedence for a manually declared relation is the same as a code-parsed association:
it's applied before `--infer-fk` runs (so it also suppresses a wrong name-based guess
for that column) and takes priority over a real DB FK constraint for the same column.
An unknown table/column/target in `relations` is always a typo, so it's a hard error,
not a silent no-op.

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

- **Database truth** — tables, columns (full SQL types, defaults, extras), table and
  column **comments**, **indexes** (with uniqueness), and real FK constraints
- **Code semantics on top** — with `--models`, framework associations are merged in;
  DB FKs already covered by an explicit association are dropped, the rest get a
  "DB FK" badge. Models without a matching table are flagged "no schema info"
- **Three kinds of edges, visually distinct** — declared associations (solid),
  DB FK constraints (solid, badged), name-based inference (`*_id`, faint dotted,
  needs `--infer-fk`). The column-list "FK" badge is grounded the same way — a
  `*_id` name alone never earns it, only a real association does
- **Interactive exploration** — locate/focus with depth and dependency direction,
  per-table deep-dive, two-level hiding, table *and column* search, named views,
  share links (state embedded in the URL). A separate toolbar "Highlight" search
  marks matches everywhere (nodes, table list, right pane) without filtering
  anything — Enter cycles through hits, and the highlight survives into PNG/SVG
  exports, for pasting into docs
- **Readable layouts** — viewport-aware packing with crossing reduction (the same
  packing focus mode uses too, so focusing never looks worse than the overview),
  edges detour around nodes, join-table chains, auto-tidy, drag-to-snap with guide
  lines, multi-select (shift/ctrl-click or shift-drag a rubber-band) with
  align-left/top/center/middle and distribute-horizontal/vertical, and
  layout undo/redo (Ctrl/Cmd+Z)
- **Exports** — PNG (clipboard or file download, 2x), SVG, Mermaid `erDiagram`, and the Excel workbook
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
