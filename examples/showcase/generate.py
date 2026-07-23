#!/usr/bin/env python3
"""Regenerate or verify committed showcase inputs and outputs.

Usage:
    python3 examples/showcase/generate.py
    python3 examples/showcase/generate.py --check
"""
import filecmp
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
INPUT = HERE / 'input'
OUTPUT = HERE / 'output'
ERD = ROOT / 'erd.py'

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  email VARCHAR(255) NOT NULL UNIQUE,
  name VARCHAR(100) NOT NULL
);
CREATE TABLE profiles (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL UNIQUE REFERENCES users(id),
  bio TEXT
);
CREATE TABLE posts (
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  parent_id INTEGER REFERENCES posts(id),
  title VARCHAR(200) NOT NULL,
  body TEXT
);
CREATE INDEX idx_posts_user_id ON posts(user_id);
CREATE TABLE tags (
  id INTEGER PRIMARY KEY,
  label VARCHAR(50) NOT NULL UNIQUE
);
CREATE TABLE post_tags (
  post_id INTEGER NOT NULL REFERENCES posts(id),
  tag_id INTEGER NOT NULL REFERENCES tags(id),
  PRIMARY KEY (post_id, tag_id)
);
"""

FORMATS = {
    '--excel': 'definitions.xlsx',
    '--emit-json': 'schema.json',
    '--emit-digest': 'digest.md',
    '--emit-dbml': 'schema.dbml',
    '--emit-mermaid': 'schema.mmd',
    '--emit-plantuml': 'schema.puml',
}


def build_db(path):
    if path.exists():
        path.unlink()
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA_SQL)


def run_variant(name, source_args, destination):
    out = destination / name
    out.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(ERD), *source_args,
               '-o', str(out / 'diagram.html'), '--no-open']
    for flag, filename in FORMATS.items():
        command.extend((flag, str(out / filename)))
    subprocess.run(command, cwd=ROOT, check=True)


def generate(destination, db_path):
    run_variant('sqlite', [f'sqlite:///{db_path.resolve()}',
                           '--config', str(INPUT / 'presentation.json')], destination)
    run_variant('config', ['--config', str(INPUT / 'schema.json')], destination)
    run_variant('models', ['--models', str(INPUT / 'models.py'),
                           '--config', str(INPUT / 'presentation.json')], destination)


def files_below(path):
    return sorted(p.relative_to(path) for p in path.rglob('*') if p.is_file())


def check_tree(expected, actual):
    expected_files, actual_files = files_below(expected), files_below(actual)
    if expected_files != actual_files:
        missing = sorted(set(expected_files) - set(actual_files))
        extra = sorted(set(actual_files) - set(expected_files))
        raise SystemExit(f'showcase drift: missing={missing}, extra={extra}')
    changed = [p for p in expected_files
               if not filecmp.cmp(expected / p, actual / p, shallow=False)]
    if changed:
        raise SystemExit('showcase drift: regenerate with '
                         '`python3 examples/showcase/generate.py`; changed: '
                         + ', '.join(map(str, changed)))


def main():
    check = sys.argv[1:] == ['--check']
    if sys.argv[1:] not in ([], ['--check']):
        raise SystemExit('usage: generate.py [--check]')
    if check:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            expected_db = tmp / 'showcase.db'
            expected_output = tmp / 'output'
            build_db(expected_db)
            generate(expected_output, expected_db)
            if not filecmp.cmp(expected_db, INPUT / 'showcase.db', shallow=False):
                raise SystemExit('showcase.db drift: regenerate with '
                                 '`python3 examples/showcase/generate.py`')
            check_tree(expected_output, OUTPUT)
        print('showcase inputs and outputs are up to date')
        return
    INPUT.mkdir(parents=True, exist_ok=True)
    build_db(INPUT / 'showcase.db')
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    generate(OUTPUT, INPUT / 'showcase.db')
    print(f'generated showcase under {HERE.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
