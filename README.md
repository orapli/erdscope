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

Connections use PyMySQL when installed and otherwise fall back to the `mysql` CLI —
the tool itself has no dependencies. Use a read-only account, and leave the password
out of the URL — if it's not in `MYSQL_PWD` either, you'll be prompted for it
(hidden input, never touches argv or shell history).

### Options

| Option | Description |
|---|---|
| `-o FILE` | Output HTML path (default: `erd.html`) |
| `--models PATH` | Merge associations parsed from code: a Rails project (or `app/models` dir), a `schema.prisma`, or a Django project — auto-detected |
| `--excel FILE.xlsx` | Also write a table-definition workbook: an overview sheet plus one sheet per table (columns, defaults, keys, comments, indexes, associations) |
| `--max-rows N` | Max column rows shown per table (default: 15; the rest scroll) |
| `--only 'user*,post*'` | Include only tables matching the glob pattern(s) |
| `--exclude '*_logs'` | Exclude tables matching the glob pattern(s) |
| `--no-infer-fk` | Disable inferring relations from `*_id` columns |

## What you get

- **Database truth** — tables, columns (full SQL types, defaults, extras), table and
  column **comments**, **indexes** (with uniqueness), and real FK constraints
- **Code semantics on top** — with `--models`, framework associations are merged in;
  DB FKs already covered by an explicit association are dropped, the rest get a
  "DB FK" badge. Models without a matching table are flagged "no schema info"
- **Three kinds of edges, visually distinct** — declared associations (solid),
  DB FK constraints (solid, badged), name-based inference (`*_id`, faint dotted)
- **Interactive exploration** — locate/focus with depth and dependency direction,
  per-table deep-dive, two-level hiding, table *and column* search, named views,
  share links (state embedded in the URL)
- **Readable layouts** — viewport-aware packing with crossing reduction, elliptical
  hub-and-spoke focus view, edges detour around nodes, join-table chains, auto-tidy,
  drag-to-snap with guide lines, multi-select (shift/ctrl-click) with
  align-left/top/center/middle and distribute-horizontal/vertical, and
  layout undo/redo (Ctrl/Cmd+Z)
- **Exports** — PNG (clipboard, 2x), SVG, Mermaid `erDiagram`, and the Excel workbook
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
