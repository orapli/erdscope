# Examples

A ready-to-run sample so you can try erdscope in one command — no database
server to set up.

If you `pip install`ed erdscope rather than cloning this repository, `erdscope demo`
is the fastest path — it builds this same sample database in a temp directory and
opens the diagram for you, no files to fetch. Everything below is for working with
the committed copy directly (e.g. after cloning the repo).

## `demo_shop.db` — a sample SQLite database

`demo_shop.db` is a small e-commerce schema (the same one as the [hosted
demo](https://orapli.github.io/erdscope/)), but as a **real SQLite database**
with actual foreign keys, unique constraints, and indexes. SQLite support is
built into Python (the `sqlite3` stdlib module), so this works with **zero
dependencies** — nothing to install.

### Try it

From the repository root:

```bash
python3 erd.py sqlite:///examples/demo_shop.db -o shop.html
# then open shop.html in your browser
```

Or, if you installed the CLI (`pip install erdscope`):

```bash
erdscope sqlite:///examples/demo_shop.db -o shop.html
```

The URL is SQLAlchemy-style: `sqlite:///relative/path.db` is relative to the
current directory, `sqlite:////absolute/path.db` is an absolute path.

### Things to notice in the diagram

- **`shipments` ↔ `orders` is 1:1** — `shipments.order_id` is `UNIQUE`, so the
  edge is drawn as one-to-one rather than the default many-to-one.
- **`categories` self-references** via `parent_id` (a category tree).
- **Composite primary keys** on the join tables `product_categories` and
  `order_coupons`.
- **`activity_logs.user_id` / `order_id` have no foreign keys on purpose.** Add
  `--infer-fk` to have erdscope guess those edges from the column names:

  ```bash
  python3 erd.py sqlite:///examples/demo_shop.db --infer-fk -o shop.html
  ```

- Also write an Excel table-definition workbook with `--excel`:

  ```bash
  python3 erd.py sqlite:///examples/demo_shop.db --excel shop.xlsx -o shop.html
  ```

### Rebuilding the sample

`demo_shop.db` is committed so you can run it directly, but it is generated —
regenerate it any time with:

```bash
python3 examples/build_demo_db.py
```

See [`build_demo_db.py`](build_demo_db.py) for the full schema (plain SQLite
DDL).
