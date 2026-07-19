# Security Policy

## Reporting a vulnerability

Please report security issues privately using
[GitHub Security Advisories](https://github.com/orapli/erdscope/security/advisories/new)
rather than a public issue. Include the erdscope version, a minimal
reproduction, and the impact you believe it has. We'll acknowledge the report
and work with you on a fix and disclosure timeline.

## Things to know before you connect erdscope to a database

- **The generated HTML is not sanitized for sharing.** It embeds your schema
  in full — table and column names, comments, indexes, and any `notes:`/
  `groups:` content from your config — as readable JSON in the page. Treat a
  generated diagram the same way you'd treat a schema dump: fine for your
  team, not something to post publicly or attach to a public issue without
  reviewing its contents first.
- **Use a read-only database account.** erdscope only ever reads schema
  metadata (and, for the SQLite/MySQL/PostgreSQL adapters, catalog
  information) — it never writes to the database — but a read-only account
  limits the blast radius if a connection string ends up somewhere it
  shouldn't, and is good practice regardless of which tool is using it.
- **erdscope runs entirely locally and sends nothing over the network**
  beyond the database connection you give it (and, for `--models`/`sources`,
  reading files from disk). There is no telemetry, no phone-home, and no
  external service involved in generating a diagram.
