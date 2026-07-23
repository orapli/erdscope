# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.11.3] - 2026-07-24

### Fixed

- **Cross-platform showcase verification.** XLSX artifacts are compared by ZIP
  entries and uncompressed XML rather than environment-specific Deflate bytes,
  and SQLite generator connections are explicitly closed so Windows can remove
  temporary databases after verification.

## [0.11.2] - 2026-07-24

### Fixed

- **Portable showcase drift checks.** The committed SQLite input is now
  compared through a semantic tables/columns/foreign-keys/indexes signature
  instead of raw database-file bytes, which vary across SQLite versions and
  operating systems. Generated HTML, Excel, and schema outputs retain strict
  byte-for-byte checks.

## [0.11.1] - 2026-07-24

### Added

- **Executable provider contracts.** Database adapters and framework overlays
  now validate registration metadata and returned IR at their dispatch
  boundaries. Base-class documentation includes concrete return shapes,
  sparse-versus-complete table rules, typed/untyped model dispatch, and plugin
  override behavior. Invalid third-party output fails early with a path-specific
  message while exceptions raised by provider acquisition remain intact.
- **Committed multi-provider review showcase.** Equivalent SQLite, JSON-config,
  and SQLAlchemy-model inputs now ship with generated HTML, Excel, canonical
  JSON, Digest, DBML, Mermaid, and PlantUML results. A deterministic generator,
  unittest, and CI drift gate require these review artifacts to stay current.

### Changed

- **Reproducible Excel output.** XLSX ZIP entries now use fixed metadata, making
  workbooks byte-deterministic and suitable for committed-output drift checks.
- **Plugin reload behavior.** A later framework overlay registration replaces
  an earlier overlay with the same name, matching database scheme overrides.

### Fixed

- **Edge-size fallback consistency.** Relation routing and self-loop drawing now
  calculate an uncached node's real size instead of assuming a fixed rectangle.
- **Provider diagnostics.** Model detection errors list registered source types
  dynamically, and provider-raised `ValueError` exceptions are no longer
  mislabeled as invalid return values.

## [0.11.0] - 2026-07-23

### Added

- **Viewer: selectable layout orientation.** The overview can now be laid
  out vertically, horizontally, or automatically. Horizontal layout keeps
  each depth-1 branch together on one side of the hub, balances branches
  deterministically by subtree load, and handles mixed node sizes, cycles,
  multiple components, and isolates. Auto evaluates bounded Vertical and
  Horizontal candidates against the viewport, preferring clear layouts,
  then fit scale and edge length, with a stable Vertical preference for
  near ties. Focus mode remains Vertical. The policy is preserved in local
  state, named views, and shared snapshots without disturbing saved
  positions when a view is restored.

### Changed

- **Viewer: straight-first relation routing.** Clear relations use direct
  straight segments, with orthogonal one-bend and bounded two-bend routes
  used around table obstacles. Bézier curves are now the bounded fallback
  rather than the default (self-relations retain their loop). Parallel
  associations no longer bend solely because they share endpoints, and
  through-table labels are placed on the longest segment and moved away
  from table rectangles when necessary. Routing remains deterministic and
  incident edges alone are recalculated while dragging.
- **Viewer documentation and browser coverage.** The English and Japanese
  manuals now describe layout orientation, Auto scoring, Focus behavior,
  undo semantics, and edge-routing priorities. Browser acceptance tests
  cover the new layouts, obstacle avoidance, labels, drag updates, and SVG
  export.

## [0.10.0] - 2026-07-21

### Changed

- **Viewer: Auto-tidy layout quality.** The overview's automatic layout
  (`Auto-tidy` and the `↺` re-layout button) picks the best of a few bounded
  row-width candidates instead of one fixed heuristic, and isolated
  (unrelated) tables now shelf-pack into multiple columns instead of one
  ever-growing column — both aimed at the diagram spreading too far
  vertically. Group frames no longer act as layout obstacles while hidden,
  and node-node overlaps left behind by group-frame obstacle avoidance are
  now corrected (including when two different groups' members conflict).
  `↺` now always performs exactly one layout pass and always re-fits the
  viewport, regardless of the Auto-tidy setting. Auto-tidy itself no longer
  re-packs the overview when neither the displayed tables nor their sizes
  actually changed.
- **Viewer: compact ROOT indicator.** The table list's "ROOT" text tag
  (a checked table that's currently a live Auto-expand traversal root) is
  now a small green `◎` symbol instead of a word, freeing up horizontal
  space next to the table name/logical-name columns. `AUTO`/`KEPT` tags and
  the diagram node's own `✓` badge are unchanged.

## [0.9.1] - 2026-07-21

0.9.x is a stabilization pass — no new input sources, exports, or CLI
capabilities. It closes the gap between what erdscope actually supports and
what its CLI help, packaging metadata, and docs claimed, and adds the
support/release-safety scaffolding a public PyPI package should have had
from the start.

### Added

- **`erdscope --version` / `-V`** — the version string only ever lived in
  `pyproject.toml`, so there was no way to check what a running `erd.py`
  actually was. `__version__` now lives in `src/erdscope/header.py`
  (included in the single-file build, so `erd.py` still works standalone)
  and is kept in sync with `pyproject.toml` by `tests/test_version.py`.
- **`.github/ISSUE_TEMPLATE/bug_report.yml`** — structured bug report form
  (erdscope/Python/OS versions, which input source(s) are involved, a
  secret-free repro command, minimal repro input, actual vs. expected).
- **`SECURITY.md`** — vulnerability reports go through GitHub Security
  Advisories; also documents that generated HTML embeds the full schema
  (treat it like a schema dump before sharing), that a read-only DB account
  is recommended, and that erdscope makes no network calls of its own.
- **Release safety gate** (`release.yml`) — a `verify` job now runs between
  `build` and `publish` on every `v*` tag push, and must pass before
  anything reaches PyPI: the tag must match `pyproject.toml`'s version,
  `CHANGELOG.md` must have a heading for that version, and the built wheel
  must actually install and pass `erdscope --version` / `erdscope demo`.
- **CI now runs on Python 3.9 and Windows** (`ci.yml`) — the
  zero-dependency unit test pass (only) now also runs on Python 3.9 (the
  oldest version `pyproject.toml` promises) and on `windows-latest` (plus
  an `erd.py demo` smoke test there). Full E2E/playwright and the
  MySQL/PostgreSQL integration tests remain Ubuntu + latest-Python only.
- **README: "Who this helps" / "Why trust it with your schema"** — three
  concrete use cases (onboarding onto an existing DB, DB-less code review,
  client/audit handoff) and the trust properties (local-only, no
  telemetry, read-only account is enough, self-contained output), added
  right after the quickstart in `README.md`/`README.ja.md`.

### Fixed

- **SQLAlchemy/Laravel were invisible in `--help` and packaging metadata**
  — both frameworks were fully supported (0.8.0/earlier) but the CLI
  `description`, the `--models` help text, `pyproject.toml`'s `description`
  and `keywords`, and the manual's intro copy / `--table-map` FAQ still
  only mentioned Rails/Prisma/Django, and `--table-map`'s help called
  itself "Rails only" despite Laravel consuming it too. All now list every
  registered framework; a new test asserts every registered framework
  overlay name appears in `--help`, so a future overlay addition can't
  silently repeat this drift.

- **Viewer: coupons/order_coupons still landing far apart after the L
  layout fix** — a follow-up to the earlier group-obstacle direction fix
  (0.8.0): gridLayout's depth-1 row, when it wraps into 2 physical
  sub-rows, alternates them above/below the hub with no notion of whether
  a depth-1 node has its own depth-2+ children. A childless depth-1
  sibling flipped to the "up" band costs nothing, but a depth-1 node that
  IS a parent gets stranded from its children, which always flow downward
  regardless of which band the parent landed in. Depth-1's row is now
  stable-partitioned (parents-with-descendants first) right before the
  wrap decides sub-row membership. Demo measurement: the
  coupons/order_coupons gap went from 716px to 216px (and now sit almost
  directly above/below each other on the x axis too).

- **Viewer: turning Auto-expand off dropped every table it had pulled in**
  — `getDisplayTables()` re-evaluated the display set the instant the
  toggle flipped, so a still-unchecked auto-expanded table vanished
  immediately, reading as "throw away what was shown" instead of "stop
  expanding further". A new `retainedExpandedTables` set now freezes
  exactly those tables at the ON→OFF instant so they stay on screen — with
  a finer-dashed `KEPT` border and node/list tooltips distinct from a live
  `AUTO` table — until promoted (the node's `＋`, or its now-unlocked list
  checkbox), removed (`⊖`), banned, or the overview is reset. Repeatedly
  toggling Auto-expand on and off no longer creeps the display outward:
  turning it back on clears the kept set and recomputes the expansion from
  scratch. Persisted through `localStorage`, named views, and share links
  (old saves/links without the new field, or with a corrupted/foreign
  value, load as an empty kept set rather than failing).
- **Viewer: a table could reappear off-screen after "None" then re-checking
  it** — `refreshView()` decided whether a table was "newly visible" (and
  therefore worth re-fitting the viewport for) by checking `nodePos`'s own
  keys, but `nodePos` deliberately keeps a table's last coordinate even
  after it leaves the display set (so a manually-dragged layout survives
  toggling Auto-tidy-off tables in and out). A table that had been shown
  before and was re-checked therefore still had a stale leftover entry
  there, so it was wrongly treated as already on screen and the viewport
  was never refit — silently leaving it wherever the layout happened to
  place it, sometimes off-screen with no visible cue to scroll or pan.
  `refreshView()` now tracks the actual previously-rendered display set
  instead, always re-fits going from 0 displayed tables to 1+, and always
  re-fits after Auto-tidy repacks the whole layout.

## [0.9.0] - 2026-07-19

### Added

- **`sources[].type: sqlalchemy.models`** (backlog F1) — a new typed input
  source that statically parses SQLAlchemy declarative models (a single
  `.py` file or a directory, recursively) with AST analysis only — no
  import, no execution, no SQLAlchemy dependency. Recognizes both the
  classic `declarative_base()` and the 2.0 `DeclarativeBase` styles:
  `Column(...)`/`mapped_column(...)` with explicit type objects,
  `ForeignKey('table.col')` → belongs_to (has_one when the column is
  unique), and `relationship(secondary=...)` → many-to-many.
  `--models`/`models` auto-detection recognizes SQLAlchemy projects by
  content. Known limits: an annotation-only column (`Mapped[int]` with no
  type argument) keeps the column with an empty type, and an
  annotation-only `relationship()` (target named only in the annotation,
  no first argument) is not picked up — declare the target explicitly.
- **`sources[].type: laravel.models`** (backlog F2) — a new typed input
  source that statically parses a directory of Laravel Eloquent model
  `*.php` files (typically `app/Models`; `vendor/` always excluded) with
  regexes over comment-stripped source — no PHP runtime, never executes
  the input. `hasMany`/`hasOne`/`belongsTo`/`belongsToMany` (pivot table
  kept as `through`) and the `morph*` family (polymorphic) become
  associations. Association-only / DB-first, like Rails models: pair it
  with a live database (or another physical source) for the column layer.

### Changed

- **Viewer: obstacle avoidance picks the cheapest direction** (backlog L) —
  when auto-placement must push a table out of a group frame it overlaps,
  the push now goes down, left, or right, whichever clears the overlap
  with the least travel (previously always down). A table shallowly
  overlapping a tall frame's side edge no longer travels the full frame
  height; related tables land beside their group instead of far below it.

## [0.8.0] - 2026-07-19

### Added

- **`--emit-mermaid FILE.mmd`** — writes a
  [Mermaid](https://mermaid.js.org/syntax/entityRelationshipDiagram.html)
  `erDiagram` export of the final schema alongside the HTML (`-` for
  stdout; the HTML is still generated either way): tables, columns (coarse
  `type`, PK/FK markers), and relationships (crow's-foot cardinality —
  one-to-one/one-to-many/many-to-many). This is the same lightweight
  diagram content the viewer's own Mermaid Copy/Download buttons already
  produce for whatever subset of tables is currently on screen, now
  available non-interactively for the whole schema (or whatever `--only`/
  `--exclude` narrowed it to). Does not include notes/groups.
- **`--emit-plantuml FILE.puml`** — writes a
  [PlantUML](https://plantuml.com/ie-diagram) entity-relationship export of
  the final schema alongside the HTML (`-` for stdout; the HTML is still
  generated either way): entities, columns (coarse `type`, PK/FK markers),
  and relationships (crow's-foot cardinality), mirroring the viewer's own
  PlantUML Copy/Download buttons but non-interactively for the whole schema
  (or whatever `--only`/`--exclude` narrowed it to). Does not include
  notes/groups.
- **`sources[].type: dbml`** (backlog P4) — a new typed input source that
  statically parses a [DBML](https://dbml.dbdiagram.io/docs/) file: tables,
  columns, indexes, primary keys (including composite, via an
  `indexes { (a, b) [pk] }` entry), and `Ref` relationships (all four
  cardinality symbols, inline on a column or as a standalone/block
  statement) — no DBML library dependency. Same authority rank as
  `rails.schema` (a declared physical schema document); a `Ref`-derived
  foreign key carries the same schema-FK provenance. `TableGroup`, a
  standalone `Note` object, and a composite (multi-column) `Ref` are out of
  scope this round — recognized and skipped with a file:line warning, never
  silently dropped.
- **`sources[].type: mermaid.er`** (backlog P5) — a new typed input source
  that statically parses a Mermaid `erDiagram`: entity blocks (columns,
  `PK`/`UK`; `FK` is a display-only hint) and relationship lines
  (crow's-foot cardinality mapped onto belongs_to/has_one/
  has_and_belongs_to_many, the label becoming the association name — no
  `foreign_key`, since a relationship line names no column). The lowest-
  authority input source (below even `--models` code parsing), since a
  Mermaid column's type is free text jotted down while sketching. MVP
  scope: a single standalone `.mmd`/`.mermaid` file, no Markdown-fence
  extraction yet.
- **Viewer: Align Right / Align Bottom** (backlog V1) — the multi-select
  panel's Align section gains two buttons alongside the existing Left/Top/
  Center/Middle ones.
- **Viewer: promote an auto-expanded table to an explicit selection**
  (backlog V2) — a table shown only because auto-expand pulled it in as a
  neighbor (dashed border, its list checkbox previously locked) can now be
  promoted to an explicit root: a new ＋ button on the node header, and the
  list checkbox itself unlocks for this specific case (still locked while
  focused, where checkboxes are ignored for visibility entirely).
- **Viewer: groups as an auto-layout obstacle** (backlog V3) — gridLayout
  and the incremental single-table-placement path now nudge a freshly
  auto-placed table clear of any other group's frame it would otherwise
  land inside, instead of leaving it to visually overlap the group. Never
  touches an already-positioned table (a manual drag is never overridden);
  a schema with no groups configured is completely unaffected.

## [0.7.2] - 2026-07-18

### Added

- **Notes/Groups in the Excel workbook** (backlog #4) — `--excel` now renders
  configured notes and groups instead of silently ignoring them (the
  `write_excel(notes=, groups=)` parameters were wired but unused since notes/
  groups Phase 1). A **Notes** sheet (one row per note: id/scope/target/
  title/text/links, sorted by id) and a **Groups** sheet (one row per group:
  id/title/color/tables, sorted by id) are appended when either is
  configured; the overview sheet gains a trailing **Group** column (the
  table's group title, or blank) when any groups are configured. Both
  additions are omitted entirely — not left present-but-empty — when there
  are no notes/groups, so a run with neither produces byte-identical output
  to before this feature.
- **`--emit-dbml FILE.dbml`** (backlog #5) — writes a minimal
  [DBML](https://dbml.dbdiagram.io/docs/) export of the final schema
  alongside the HTML (`-` for stdout; the HTML is still generated either
  way): tables, columns (with `sql_type` preferred over the coarse `type`
  shorthand for fidelity), primary keys, indexes, single-column-FK
  relations (`Ref:`), and table comments (`Note:`). This is the export half
  of DBML support — DBML as an *input* source is a later, separate piece of
  work that this fixes the IR↔DBML mapping for. Deliberately minimal:
  `notes`/`groups` and DBML's own `Project`/`TableGroup` blocks are out of
  scope this round. `Ref:` generation only ever considers a `belongs_to`
  with a foreign key — never `has_one`/`has_many`/
  `has_and_belongs_to_many` — because a `has_one`'s `foreign_key` is
  ambiguous across providers (most treat it as a column on the declaring
  table, but Rails' hand-written `has_one foreign_key:` names a column on
  the *other* table instead) and the schema alone can't tell which provider
  produced a given one; a polymorphic `belongs_to` is skipped silently, and
  a `belongs_to` whose target has no single-column primary key is skipped
  with a stderr warning (never a hard failure). Deterministic: the same
  schema always renders the same DBML text. Purely additive: existing
  HTML/Excel/`--emit-json`/`--emit-config`/`--emit-digest` output is
  untouched; not combinable with `--diff` (a usage error, exit 2), same as
  every other output-generating flag.

## [0.7.1] - 2026-07-18

### Added

- **`--emit-digest FILE.md`** (backlog #3) — writes a token-efficient Markdown
  digest of the final schema, with design notes, meant for pasting into or
  reading by an LLM/agent instead of the raw schema or the full `--emit-json`
  snapshot (`-` for stdout; the HTML is still generated either way). Projects
  the same canonical schema `--emit-json` does (same deterministic ordering,
  same dangling-association pruning), but drops provenance/`sources` and, by
  default, a column's `nullable`/`default`/`sql_type` to spend the token
  budget on meaning rather than every DB-level nuance (`--digest-verbose`
  adds those three back). Global/table/relation notes — the one thing a
  digest carries that the raw schema can't — render inline: a global note as
  an intro paragraph, a table note under its table's heading, a relation note
  appended to the association line it targets. `groups` is intentionally
  never rendered (a viewer layout aid, not schema meaning). Each table
  becomes a heading, one bullet per column, and one compressed `Rel:` line
  summarizing its associations; a table with no associations omits that
  line. Deterministic: the same schema always renders the same Markdown.
  Purely additive: existing HTML/Excel/`--emit-json`/`--emit-config` output
  is untouched; not combinable with `--diff` (a usage error, exit 2), same as
  every other output-generating flag.

## [0.7.0] - 2026-07-18

### Added

- **`--emit-json FILE.json`** — writes a canonical, machine-readable JSON
  projection of the final schema (tables/columns/indexes/associations plus
  resolved notes/groups) alongside the HTML, wrapped with a `sha256` content
  fingerprint (`-` for stdout; the HTML is still generated either way). A
  fixed key allowlist (no internal/plugin keys), deterministic column/index/
  association ordering, and 5-value association provenance (declared/manual/
  db_fk/schema_fk/inferred) make the snapshot diff-friendly and stable across
  table/notes/groups/sources reordering — same input, byte-identical output.
  Purely additive: existing HTML/Excel output is untouched.
- **`--emit-config FILE`** — writes the final merged schema as a
  config-authoring file (YAML if `.yml`/`.yaml`, JSON if `.json`, `-` for
  stdout as JSON) that can be re-imported via `--config` for a
  semantically-equivalent ("level1") round trip: same tables, columns,
  types/nullability/defaults, primary-key column sets (composite PKs
  re-derived from column flags, not the possibly-truncated `primary_key`
  field), indexes as (columns, unique) sets, associations as (type, target,
  foreign_key, through, polymorphic) tuples, comments, notes, and groups.
  Not a byte-identical round trip: provenance, `sources`, and config
  drop/`*_mode` operations don't survive one pass through the merged IR. A
  handful of config-load/validation rules were relaxed to make the reimport
  possible: a non-drop index no longer requires a `name`; a relation note's
  `foreign_key`/`name`/`through`/`assoc_type`/`polymorphic` now distinguish
  an explicit `null` (must be absent on the match) from an omitted key
  (wildcard); a polymorphic association's target is no longer required to
  name a real table, and a polymorphic relation note survives `--only`/
  `--exclude` filtering as long as its source table does; a Rails-only
  (`schema_missing`) table's association foreign_key is no longer required
  to name a real column. The YAML writer is deterministic (sorted keys,
  literal block style for multi-line note text) and always quotes any plain
  scalar that YAML's own implicit-typing rules would otherwise misread as a
  bool/int/float on the next load (the "Norway problem"). Purely additive:
  existing HTML/Excel/`--emit-json` output is untouched.
- **`--diff SNAPSHOT.json`** — a CI drift gate: compares this run's schema
  against a previously-saved `--emit-json` snapshot at "level1" (materially
  the same schema, not byte-identical — the same notion `--emit-config`
  round trips to) and exits instead of generating any output. `added` =
  present in this run only, `removed` = present in the snapshot only, at
  every level (tables, then each common table's columns/indexes/
  associations, plus notes/groups); indexes match on `(columns, unique)`
  (name-blind), associations match on `(type, target, name, foreign_key,
  through, polymorphic)` with provenance/sources ignored unless
  `--diff-provenance` is passed. Exit 0 when identical, 1 when different
  (`--diff-exit-zero` to report without failing), 2 on a usage error or an
  unreadable/invalid snapshot. `--diff-format text` (default) or `json`
  (deterministic, for scripting). A matching snapshot `fingerprint` short-
  circuits the comparison. Not combinable with `--emit-json`/`--emit-config`/
  `--excel` (a `--diff` run is comparison-only). Like `--emit-config`, an
  empty-string default vs. no default at all isn't distinguished by any
  provider, so it's the one change `--diff` can't detect — a known level1
  limit, called out in `--diff --help` and the manual.
- The `--emit-json` snapshot shape and its `sha256` fingerprint are the stable
  **format 1** contract, versioned by the top-level `format` field — a breaking
  change to the projection bumps it (to `format` 2, …) rather than silently
  altering what existing consumers parse. Two properties of that contract worth
  stating outright: provenance is **association-limited** (a table, column, or
  index records no "which input won it" marker — only associations do); and an
  empty-string column default and no default at all are indistinguishable to
  every provider on read, so that one difference round-trips through neither
  `--emit-config` nor `--diff`.

## [0.6.0] - 2026-07-18

### Added

- **notes (Phase 1)** — a new config `notes:` list attaches design decisions,
  operational rules, and ADR links to a table, a relationship, or the whole
  diagram. A pure sidecar: it never touches physical schema, merge precedence,
  or association provenance. Two-stage validation (syntax at load, semantics
  against the final merged IR); every error names the offending note id.
  Plain-text only, HTML-escaped, with link URLs restricted to http(s). The
  viewer shows table/relation notes in the detail panel and a global note in
  the legend, and folds note text into both the filter and the Highlight
  search; hidden tables suppress their notes. `--only`/`--exclude` drop any
  note whose endpoint table(s) didn't survive. A notes-free config stays
  byte-for-byte identical (no `notes` key), so the sidecar is inert for
  existing use.
- **groups (Phase 1)** — a new config `groups:` list (`{id, title?, tables,
  color?}`) draws a labeled, rounded frame behind a set of related tables,
  mirroring notes end to end as a pure sidecar. Two-stage validation (unique
  id, non-empty `tables`, no within-group duplicate, hex `color`; every member
  exists in the merged IR; no table may belong to two groups). The viewer
  draws backmost frames sized to their displayed members and follows node
  drags live; drag a group by its title chip to move every member at once;
  a toolbar toggle shows/hides all frames; PNG/SVG export includes them.
  `--only`/`--exclude` trims membership and drops any group left empty. A
  groups-free config stays byte-for-byte identical (no `groups` key).
- Typed `sources[]` entries that parse **nothing** (e.g. a Prisma project declared as
  `rails.models`) are now a hard error naming the source id and the layout the type
  expected, instead of a silently empty success. A new per-source `allow_empty: true`
  opts back into accepting an empty result; `rails.project` passes the flag down to
  both of its expanded halves.
- Prisma: composite `@@id([...])` becomes a composite primary key (list form, with
  `primary` flags on the member columns); a single-field `@@unique([x])` now carries
  the same 1:1 signal as an inline `@unique`; a relation whose `fields:` column is
  `@map`ped now reports the real column name as its foreign key.
- Django: `GenericForeignKey` surfaces as a polymorphic `belongs_to` (details-pane
  only, no edge — same treatment as a Rails polymorphic association); an FK whose
  target can't be resolved statically (swappable `AUTH_USER_MODEL`, contenttypes'
  `ContentType`, ...) keeps its column and skips only the edge; two apps defining
  models with the same class name now both keep their tables (previously one
  silently overwrote the other), with bare references preferring the same app.
- A shared provider contract suite (`tests/test_provider_contract.py`) now holds every
  input provider (`rails.models`, `prisma.models`, `django.models`, `rails.schema`) to
  the same checks — typed dispatch, standalone HTML/Excel generation, DB-layer merge,
  1:N / 1:1 / M:N / self-reference, association provenance, empty-input diagnostics —
  over one common fixture domain, plus advanced per-framework fixtures (composite
  keys, named/self relations, explicit m2m, app collisions, swappable models).
- README and the manual (EN/JA) document the **verified versions** of every input:
  MySQL 8.4 / PostgreSQL 16 (CI, real servers), CPython's bundled SQLite, the Rails
  7.x/8.x `schema.rb` format, the Rails 7.x association DSL, Prisma 5/6 schema
  language, Django 4.2/5.x models.

### Fixed

- Prisma: a 1:N **self-relation** (`parent`/`replies`) and mixed **named relations**
  are no longer misread as implicit many-to-many — the m2m pairing now matches
  `@relation` names on both sides instead of "the other model declares some list".
- Prisma: a typed `prisma.models` source pointing at a directory without a
  `schema.prisma` now exits with a clear message instead of a `StopIteration`
  traceback.

## [0.5.0] - 2026-07-17

### Added

- New `rails.schema` provider: statically parses a Rails `db/schema.rb` file (columns,
  primary keys, indexes, real foreign keys) into a new `schema` layer kind — no live
  database and, crucially, **no Ruby execution** required. A `schema.rb`-derived
  foreign key merges as a **schema FK** (a new teal badge in the viewer, its own
  "schema FK" column in the Excel `--excel` export), reconciled against a covering
  declared/DB-FK association exactly like a live DB FK already is.
- Config gains a typed **`sources:`** list — `{ id, type, path }` entries that name
  their own input kind explicitly instead of relying on `--models`/`models`
  auto-detection. Every registered framework overlay gets a `<name>.models` type for
  free (`rails.models`, `prisma.models`, `django.models`, and any `--adapter`-registered
  overlay's own), plus the new `rails.schema` type and a `rails.project` macro that
  expands a Rails app root into both its `rails.schema` and `rails.models` halves.
  `sources` and `models`/`--models` are independent and both apply if both are given.
- Merge authority order extends for the new `schema` layer: physical facts (column
  types, indexes, primary keys) follow **Config > live DB > rails.schema > models**;
  associations/comments (logical names) follow **Config > models > rails.schema > DB**.
- An untyped `--models`/config `models` path pointing directly at a `schema.rb` file
  now auto-detects as `rails.schema` (with a stderr note suggesting `sources[].type`
  to make it explicit), and ambiguous auto-detection (a path matching more than one
  framework overlay) now prints which frameworks matched and which one won, instead of
  resolving silently.

## [0.4.1] - 2026-07-16

### Fixed

- Viewer: the table list's relation-count badge and 🚫 ban button now stay pinned to
  the row's right edge regardless of table name length or whether a logical name is
  present (previously they drifted, sitting right after the name when no logical
  name filled the row). Long table names wrap instead of being truncated with an
  ellipsis.

## [0.4.0] - 2026-07-16

### Added

- `erdscope demo`: try erdscope instantly with no database of your own. Builds the
  sample e-commerce database (the same schema as `examples/demo_shop.db`) in a temp
  directory, runs it through the normal pipeline, and opens the result in a browser.
  Fixes the pip-installed quickstart: `pyproject.toml` ships this project as a single
  `erd.py` module, so `examples/demo_shop.db` was never actually reachable after
  `pip install erdscope`. Every other flag still applies (`--only`, `--excel`, ...);
  config auto-discovery is force-disabled and an explicit `--config` is ignored with a
  warning, so the demo is deterministic regardless of the cwd. New `--no-open` flag
  skips the automatic browser launch (a no-op outside `demo`)
- CI: a `db-integration` job runs `tests/test_db_integration.py` against real MySQL 8.4
  and PostgreSQL 16 service containers, in parallel with the existing `test` job.
  Verifies table/comment/index/FK extraction (including 1:1 promotion from a unique FK,
  composite primary keys/indexes, and PostgreSQL expression indexes) and, for each
  engine, asserts the PyMySQL/psycopg driver path and the dependency-free mysql/psql
  CLI fallback path produce byte-identical output — the regression test for the NULL
  default bug below. Skips cleanly (no database required) unless `ERDSCOPE_IT_MYSQL_URL`
  / `ERDSCOPE_IT_POSTGRES_URL` are set, so it's safe inside a plain `unittest discover`
- Config file: `engine: "sqlite"` is now accepted alongside `"mysql"`/`"postgres"`.
  `database` is then a local file path (relative or absolute), not a database name,
  assembled into a `sqlite:///` URL that round-trips exactly with the CLI's
  `sqlite:///` handling. `host`/`port`/`user` don't apply to a local file and are
  rejected with a clear error rather than silently ignored; paths containing `?`,
  `#`, or control characters are rejected up front because the path is pasted into
  a URL that is re-parsed downstream

### Fixed

- MySQL CLI fallback (no PyMySQL installed): a column with no default was getting a
  literal `default: 'NULL'` (the four-character string) instead of no default at all.
  `mysql --batch` prints SQL NULL as the bare, unescaped word `NULL` (the `\N`
  escape is the `SELECT ... INTO OUTFILE` / `mysqldump` spelling, not the batch
  client's) — which `_unescape_mysql_field()` didn't recognize, so it passed
  straight through as data.
  Only the CLI fallback path was affected; PyMySQL already mapped `None` to `''`. Fixed
  by also mapping the bare `NULL` literal to `''` in `_unescape_mysql_field()` (accepted
  trade-off, documented in code: a column whose comment/default text is genuinely the
  word "NULL" is now indistinguishable from an actual SQL NULL on this path)

### Changed

- Project metadata and module docstring updated to reflect SQLite support and the
  three-source (DB / code / config) model

## [0.3.0] - 2026-07-14

### Added

- SQLite support: `erdscope sqlite:///path/to/app.db`. Reads tables, columns,
  primary keys, foreign keys, unique/1:1 constraints, and indexes via Python's
  built-in `sqlite3` module — zero dependencies, nothing to install. A ready-to-run
  sample database ships in `examples/` (`examples/demo_shop.db`), with
  `examples/README.md` explaining how to try it
- Pluggable database adapters and framework overlays: engines and code parsers are
  now registered classes (`DBAdapter` / `FrameworkOverlay`). Load a custom one at
  runtime from a plugin file with `--adapter path/to/plugin.py` (or config
  `adapters:`) — no rebuild, works against the single-file `erd.py` or the pip install
- Three input sources, any one sufficient (a database is no longer required): the
  DB, application code (`--models`), and a config `tables:` schema now merge as
  layered providers (database → code → config). Config can declare or patch a full
  schema — add/override/drop tables, columns, indexes, and associations — with
  two-phase (syntactic + semantic) validation
- `--models` is repeatable and merges multiple frameworks in order

### Changed

- `erd.py` is now a build artifact assembled from split sources under `src/erdscope/`
  by `tools/build_single_file.py` (CI verifies it stays in sync). Distribution is
  unchanged: still a single, zero-dependency file

## [0.2.0] - 2026-07-10

### Added

- PostgreSQL support: `erdscope postgres://user@host:5432/db` (or `postgresql://`),
  with an optional `?schema=name` (default `public`). Reads tables, columns, comments,
  indexes, and FK constraints from `pg_catalog`; identity/serial columns are marked
  like MySQL's auto_increment; views and individual partitions are excluded
- Connects via psycopg (v3) or psycopg2 when installed, falling back to the `psql`
  CLI (`COPY … TO STDOUT`) with zero dependencies — both paths produce byte-identical
  output. New pip extra: `erdscope[postgres]`
- Config files gain an `engine` key (`"mysql"`, the default, or `"postgres"`, which
  also switches the default port to 5432)

## [0.1.0] - 2026-07-10

### Added

- Single-file, zero-dependency Python CLI for generating interactive ER diagrams from MySQL databases
- Database-first design: reads tables, columns (with SQL types, defaults, extras), comments, indexes, and real FK constraints from `information_schema`
- Code semantics: merge Rails, Prisma, or Django associations with `--models` flag; visually distinct edge types (declared, DB-FK, inferred)
- Interactive HTML viewer with focus/depth/dependency filtering, two-level hiding, and comprehensive table/column search (regex and case-toggle support)
- Named views and shareable links to preserve specific diagram states
- Readable automatic layouts with viewport-aware packing, crossing reduction, drag-to-snap with guide lines, multi-select align/distribute, and Auto-tidy
- Multiple export formats: PNG (2x), SVG, Mermaid, PlantUML, and Excel table-definition workbooks with customizable templates
- Logical names feature: display database comments alongside physical table names with independent modes for live view and exports
- Config file support (`.erdscope.json`, `.yml`, `.yaml`) with auto-discovery and CLI flag overrides
- Excel workbook generation with per-table detail sheets (columns, defaults, keys, comments, indexes, associations)
- Dark mode, print stylesheet, and resizable/collapsible panes
- Built-in `?` help popup in the generated HTML (shortcuts, mouse gestures, link to the online manual)
- PyPI package distribution with `erdscope` CLI entry point
- Comprehensive test suite (unit and E2E browser automation) with optional openpyxl and Playwright support
