"""Provider contract tests.

Every input provider reachable through a typed `sources[].type` must honour the
same minimal contract, checked here over one shared domain (users 1:N posts,
users 1:1 profiles, posts M:N tags, posts self-reference) that each provider's
fixture under tests/fixture_contract/ expresses in its own native syntax:

  * typed dispatch resolves the type and produces a non-empty ProviderResult;
  * the source can generate HTML and Excel on its own (no DB);
  * its tables and associations merge over a DB layer;
  * 1:N, 1:1, M:N, and self-referencing relations all come through;
  * merged associations carry the provider's provenance and sources;
  * an input the type cannot parse anything from is a hard error naming the
    source id (never a silently empty success).

Adding a provider (e.g. a future laravel.models) means: add its fixture
expressing the same domain, then subclass _ProviderContract the same way the
four below do.

Run from the repository root:
    python3 -m unittest tests.test_provider_contract -v
"""
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = Path(__file__).resolve().parent / 'fixture_contract'

spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)


def _minimal_db_layer():
    """A hand-built DB IR fragment with users + posts, so merging over a DB
    layer is observable (provider associations land on DB tables; provider-only
    tables surface alongside them)."""
    def t(cols):
        return {'columns': [{'name': c, 'type': 'bigint', 'nullable': False,
                             'primary': c == 'id'} for c in cols],
                'associations': [], 'indexes': [], 'primary_key': 'id'}
    return erd.make_provider_result(
        'db', 'mysql', {'users': t(['id']), 'posts': t(['id', 'user_id'])})


class _ProviderContract:
    """Mixin defining the contract; subclasses bind a provider to it."""
    TYPE = ''             # sources[].type value
    PATH = None           # fixture path (dir or file)
    PROVENANCE = 'declared'   # expected provenance of a declared association
    M2M_STYLE = 'habtm'   # 'habtm' | 'join_table' (schema sources have no habtm)

    @classmethod
    def setUpClass(cls):
        specs = erd.normalize_input_specs(
            [], [{'id': 'src', 'type': cls.TYPE, 'path': str(cls.PATH)}])
        with redirect_stderr(io.StringIO()):
            [cls.result] = erd.run_input_specs(specs, {})
        cls.tables = cls.result['tables']

    # -- helpers ----------------------------------------------------------
    def _assocs(self, tables=None):
        for tname, t in (tables or self.tables).items():
            for a in t.get('associations', []):
                yield tname, a

    def _linked(self, src, types, target, tables=None):
        return any(tn == src and a['type'] in types and a['target'] == target
                   for tn, a in self._assocs(tables))

    # -- 1. typed dispatch ------------------------------------------------
    def test_typed_dispatch_produces_named_provider_result(self):
        self.assertTrue(self.tables)
        self.assertIn('kind', self.result['source'])
        self.assertTrue(self.result['source']['provider'])
        for table in ('users', 'profiles', 'posts', 'tags'):
            self.assertIn(table, self.tables)

    # -- 2. standalone HTML + Excel generation ----------------------------
    def test_standalone_html_and_excel(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / 'c.json'
            cfg.write_text(json.dumps({'sources': [
                {'id': 'src', 'type': self.TYPE, 'path': str(self.PATH)}]}))
            out, xlsx = Path(tmp) / 'out.html', Path(tmp) / 'defs.xlsx'
            argv = sys.argv
            try:
                sys.argv = ['erd.py', '--config', str(cfg),
                            '-o', str(out), '--excel', str(xlsx)]
                with redirect_stderr(io.StringIO()):
                    erd.main()
            finally:
                sys.argv = argv
            html = out.read_text()
            for table in ('users', 'posts'):
                self.assertIn(f'"{table}"', html)
            self.assertTrue(zipfile.is_zipfile(xlsx))

    # -- 3. merge over a DB layer -----------------------------------------
    def test_merges_over_a_db_layer(self):
        with redirect_stderr(io.StringIO()):
            merged = erd.merge_ir([_minimal_db_layer(), self.result])
        # provider-only table joins the DB tables
        self.assertIn('tags', merged)
        # a provider association lands on a DB table
        self.assertTrue(
            self._linked('posts', {'belongs_to'}, 'users', merged)
            or self._linked('users', {'has_many'}, 'posts', merged))

    # -- 4. cardinalities -------------------------------------------------
    def test_one_to_many(self):
        self.assertTrue(self._linked('posts', {'belongs_to'}, 'users')
                        or self._linked('users', {'has_many'}, 'posts'))

    def test_one_to_one(self):
        self.assertTrue(self._linked('profiles', {'has_one'}, 'users')
                        or self._linked('users', {'has_one'}, 'profiles'))

    def test_many_to_many(self):
        if self.M2M_STYLE == 'habtm':
            self.assertTrue(
                self._linked('posts', {'has_and_belongs_to_many'}, 'tags')
                or self._linked('tags', {'has_and_belongs_to_many'}, 'posts'))
        else:  # join_table: the join table holds a belongs_to to each side
            self.assertTrue(self._linked('posts_tags', {'belongs_to'}, 'posts'))
            self.assertTrue(self._linked('posts_tags', {'belongs_to'}, 'tags'))

    def test_self_reference(self):
        self.assertTrue(self._linked('posts', {'belongs_to', 'has_many'}, 'posts'))

    # -- 5. provenance / sources after merge ------------------------------
    def test_association_provenance_and_sources(self):
        with redirect_stderr(io.StringIO()):
            merged = erd.merge_ir([self.result])
        src = self.result['source']
        found = [a for tn, a in self._assocs(merged)
                 if tn == 'posts' and a['target'] == 'users']
        self.assertTrue(found)
        for a in found:
            self.assertEqual(a['provenance'], self.PROVENANCE)
            self.assertEqual(a['sources'],
                             [{'kind': src['kind'], 'provider': src['provider']}])

    # -- 6. empty input is diagnosed, never a silent empty success --------
    def _write_empty_variant(self, tmp):
        """Create an input of the right shape that parses to nothing; return
        the path to declare as this TYPE."""
        raise NotImplementedError

    def test_empty_input_is_a_hard_error_naming_the_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_empty_variant(Path(tmp))
            specs = erd.normalize_input_specs(
                [], [{'id': 'empty-src', 'type': self.TYPE, 'path': str(path)}])
            with self.assertRaises(SystemExit) as cm, \
                    redirect_stderr(io.StringIO()):
                erd.run_input_specs(specs, {})
            msg = str(cm.exception)
            self.assertIn("'empty-src'", msg)
            self.assertIn('found nothing to parse', msg)


class TestRailsModelsContract(_ProviderContract, unittest.TestCase):
    TYPE = 'rails.models'
    PATH = CONTRACT / 'rails'

    def _write_empty_variant(self, tmp):
        return tmp  # a directory with no .rb files at all


class TestPrismaContract(_ProviderContract, unittest.TestCase):
    TYPE = 'prisma.models'
    PATH = CONTRACT / 'prisma'

    def _write_empty_variant(self, tmp):
        (tmp / 'schema.prisma').write_text(
            'generator client {\n  provider = "prisma-client-js"\n}\n')
        return tmp

    def test_directory_without_schema_is_a_clean_error(self):
        # not even an empty schema.prisma: the builder itself must exit with
        # a message, not leak a StopIteration traceback
        with tempfile.TemporaryDirectory() as tmp:
            specs = erd.normalize_input_specs(
                [], [{'id': 'x', 'type': self.TYPE, 'path': tmp}])
            with self.assertRaises(SystemExit) as cm, \
                    redirect_stderr(io.StringIO()):
                erd.run_input_specs(specs, {})
            self.assertIn('schema.prisma', str(cm.exception))


class TestDjangoContract(_ProviderContract, unittest.TestCase):
    TYPE = 'django.models'
    PATH = CONTRACT / 'django'

    def _write_empty_variant(self, tmp):
        (tmp / 'manage.py').write_text('#!/usr/bin/env python\n')
        return tmp


class TestRailsSchemaContract(_ProviderContract, unittest.TestCase):
    TYPE = 'rails.schema'
    PATH = CONTRACT / 'rails_schema' / 'schema.rb'
    PROVENANCE = 'schema_fk'
    M2M_STYLE = 'join_table'

    def _write_empty_variant(self, tmp):
        f = tmp / 'schema.rb'
        f.write_text('# empty schema, no create_table\n')
        return f


class TestDbmlContract(_ProviderContract, unittest.TestCase):
    TYPE = 'dbml'
    PATH = CONTRACT / 'dbml' / 'schema.dbml'
    PROVENANCE = 'schema_fk'
    M2M_STYLE = 'join_table'

    def _write_empty_variant(self, tmp):
        f = tmp / 'schema.dbml'
        f.write_text('// empty, no Table blocks\n')
        return f


class TestMermaidErContract(_ProviderContract, unittest.TestCase):
    TYPE = 'mermaid.er'
    PATH = CONTRACT / 'mermaid_er' / 'schema.mmd'
    PROVENANCE = 'declared'
    M2M_STYLE = 'habtm'

    def _write_empty_variant(self, tmp):
        f = tmp / 'schema.mmd'
        f.write_text('%% empty, no entities or relationships\nerDiagram\n')
        return f


if __name__ == '__main__':
    unittest.main()
