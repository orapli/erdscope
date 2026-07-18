"""--emit-config (backlog #1) — config-authoring (YAML/JSON) projection of
the final merged IR, and the six Sol relaxations that make a config-only
reimport reach "level1" (materially the same schema — same primary-key
column sets, column types/nullability/defaults, indexes as (columns, unique)
sets, associations as (type, target, foreign_key, through, polymorphic)
tuples, comments, notes, groups), not a byte-identical round trip.

Covers:
  - emit.py's new surface (config_document, config_json_text,
    config_yaml_text) as direct unit tests against hand-built final-IR table
    dicts, mirroring tests/test_emit_json.py's style.
  - the six relaxations, each both directly (config.py / providers.py) and
    through a full level1 round trip driven via erd.main() (no DB — config
    only, mirroring tests/test_notes.py's _NoDBDriver technique):
      #1 unnamed (non-drop) indexes accepted by config.py
      #2 null-vs-absent narrowing in resolve_and_validate_notes
      #3 polymorphic association target exemption + relation-note survival
      #4 schema_missing FK-column-existence exemption
      #5 (E-2) composite-PK detection from column.primary, not the
         primary_key field
      #6 (E-1) --emit-config extension dispatch + deterministic YAML
  - CLI wiring: alongside --emit-json/--excel/HTML, '-' stdout, the output
    collision guard, and non-destructiveness (HTML/Excel untouched).

Run from the repository root:
    python3 -m unittest tests.test_emit_config -v
"""
import builtins
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)

try:
    import yaml as _yaml_probe  # noqa: F401
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


def _col(name, type_='integer', nullable=False, **extra):
    c = {'name': name, 'type': type_, 'nullable': nullable}
    c.update(extra)
    return c


class _NoPyYAML:
    """Context manager that makes every `import yaml` fail with ImportError,
    regardless of whether PyYAML is actually installed in THIS environment —
    simulates "PyYAML not installed" for the --emit-config FILE.yml error
    path (Sol relaxation #6 / "no JSON fallback")."""
    def __enter__(self):
        self._orig = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == 'yaml' or name.startswith('yaml.'):
                raise ImportError(f'simulated: no module named {name!r}')
            return self._orig(name, *a, **kw)

        builtins.__import__ = fake_import
        return self

    def __exit__(self, *exc):
        builtins.__import__ = self._orig


# ---------------------------------------------------------------------------
# Domain fixture — used by both the direct config_document unit tests and the
# level1 round-trip integration tests. Deliberately includes: a composite PK
# (memberships), a `through` association (organizations -> members), a
# polymorphic belongs_to with a synthetic non-table target (comments), an
# unnamed unique index (users.email), a column default (users.status), and a
# table comment (users).
# ---------------------------------------------------------------------------
def _domain_tables():
    return {
        'users': {
            'comment': 'App users',
            'columns': [
                _col('id', primary=True),
                _col('email', type_='string'),
                _col('status', type_='string', default='active'),
            ],
            'indexes': [{'columns': ['email'], 'unique': True}],  # unnamed
            'associations': [],
        },
        'organizations': {
            'columns': [_col('id', primary=True), _col('name', type_='string')],
            'indexes': [],
            'associations': [
                {'type': 'has_many', 'name': 'members', 'target': 'users',
                 'through': 'memberships', 'provenance': 'declared',
                 'sources': [{'kind': 'framework', 'provider': 'rails'}]},
            ],
        },
        'memberships': {
            'columns': [
                _col('org_id', primary=True),
                _col('user_id', primary=True),
                _col('role', nullable=True, type_='string'),
            ],
            'indexes': [],
            'associations': [
                {'type': 'belongs_to', 'name': 'org', 'target': 'organizations',
                 'foreign_key': 'org_id', 'provenance': 'declared',
                 'sources': [{'kind': 'framework', 'provider': 'rails'}]},
                {'type': 'belongs_to', 'name': 'member', 'target': 'users',
                 'foreign_key': 'user_id', 'provenance': 'declared',
                 'sources': [{'kind': 'framework', 'provider': 'rails'}]},
            ],
        },
        'comments': {
            'columns': [
                _col('id', primary=True),
                _col('commentable_id'),
                _col('commentable_type', type_='string'),
                _col('body', type_='text'),
            ],
            'indexes': [],
            'associations': [
                {'type': 'belongs_to', 'name': 'commentable', 'target': 'commentables',
                 'foreign_key': 'commentable_id', 'polymorphic': True,
                 'provenance': 'declared', 'sources': [{'kind': 'framework', 'provider': 'rails'}]},
            ],
        },
    }


def _domain_notes():
    return [
        {'id': 'g1', 'scope': 'global', 'text': 'Overview note'},
        {'id': 't1', 'scope': 'table', 'table': 'users', 'text': 'Users note'},
        {'id': 'r1', 'scope': 'relation', 'source_table': 'memberships', 'target': 'organizations',
         'type': 'belongs_to', 'name': 'org', 'foreign_key': 'org_id', 'through': None,
         'polymorphic': False, 'text': 'org membership note'},
        {'id': 'r2', 'scope': 'relation', 'source_table': 'memberships', 'target': 'users',
         'type': 'belongs_to', 'name': 'member', 'foreign_key': 'user_id', 'through': None,
         'polymorphic': False, 'text': 'member note'},
        {'id': 'r3', 'scope': 'relation', 'source_table': 'comments', 'target': 'commentables',
         'type': 'belongs_to', 'name': 'commentable', 'foreign_key': 'commentable_id',
         'through': None, 'polymorphic': True, 'text': 'polymorphic note'},
    ]


def _domain_groups():
    return [{'id': 'gr1', 'title': 'Core', 'tables': ['users', 'organizations'], 'color': '#0d9488'}]


# ---------------------------------------------------------------------------
# config_document — shape / allowlist
# ---------------------------------------------------------------------------
class TestConfigDocumentShape(unittest.TestCase):
    def test_top_level_keys(self):
        doc = erd.config_document(_domain_tables(), _domain_notes(), _domain_groups(), title='Demo')
        self.assertEqual(doc['version'], 1)
        self.assertEqual(doc['title'], 'Demo')
        self.assertIn('tables', doc)
        self.assertIn('notes', doc)
        self.assertIn('groups', doc)
        self.assertLessEqual(set(doc), {'version', 'title', 'tables', 'notes', 'groups'})

    def test_title_omitted_when_absent(self):
        doc = erd.config_document(_domain_tables(), None, None)
        self.assertNotIn('title', doc)

    def test_title_omitted_when_falsy(self):
        doc = erd.config_document(_domain_tables(), None, None, title='')
        self.assertNotIn('title', doc)

    def test_notes_and_groups_omitted_when_empty(self):
        doc = erd.config_document(_domain_tables(), None, None)
        self.assertNotIn('notes', doc)
        self.assertNotIn('groups', doc)

    def test_table_keys_allowlisted(self):
        doc = erd.config_document(_domain_tables(), None, None)
        for t in doc['tables'].values():
            self.assertLessEqual(set(t), {'comment', 'primary_key', 'columns', 'indexes', 'associations'})
            self.assertIn('columns', t)
            self.assertIn('indexes', t)
            self.assertIn('associations', t)

    def test_comment_omitted_when_absent(self):
        doc = erd.config_document(_domain_tables(), None, None)
        self.assertNotIn('comment', doc['tables']['organizations'])
        self.assertEqual(doc['tables']['users']['comment'], 'App users')


# ---------------------------------------------------------------------------
# composite primary key detection (Sol relaxation #5 / E-2)
# ---------------------------------------------------------------------------
class TestCompositePrimaryKey(unittest.TestCase):
    def test_single_column_pk_stays_a_column_flag_only(self):
        doc = erd.config_document(_domain_tables(), None, None)
        users = doc['tables']['users']
        self.assertNotIn('primary_key', users)
        id_col = next(c for c in users['columns'] if c['name'] == 'id')
        self.assertIs(id_col['primary'], True)

    def test_composite_pk_promoted_to_table_level_list_in_column_order(self):
        doc = erd.config_document(_domain_tables(), None, None)
        memberships = doc['tables']['memberships']
        self.assertEqual(memberships['primary_key'], ['org_id', 'user_id'])
        # both columns still individually carry primary: true too
        self.assertTrue(all(c['primary'] for c in memberships['columns']
                            if c['name'] in ('org_id', 'user_id')))

    def test_no_primary_columns_yields_no_primary_key_key(self):
        tables = _domain_tables()
        for c in tables['comments']['columns']:
            c.pop('primary', None)
        doc = erd.config_document(tables, None, None)
        self.assertNotIn('primary_key', doc['tables']['comments'])

    def test_detection_ignores_the_irs_own_primary_key_field(self):
        # A DB-sourced composite PK's `primary_key` field names only its
        # FIRST column (merge.py:312) — config_document must NOT read that
        # field; it must derive the full set from column.primary flags.
        tables = _domain_tables()
        tables['memberships']['primary_key'] = 'org_id'  # truncated, as a real DB layer would leave it
        doc = erd.config_document(tables, None, None)
        self.assertEqual(doc['tables']['memberships']['primary_key'], ['org_id', 'user_id'])


# ---------------------------------------------------------------------------
# columns / indexes — reuse of the #0 canonical projection
# ---------------------------------------------------------------------------
class TestConfigColumnsAndIndexes(unittest.TestCase):
    def test_columns_match_canonical_projection(self):
        tables = _domain_tables()
        doc = erd.config_document(tables, None, None)
        schema = erd.canonical_schema(tables, None, None)
        self.assertEqual(doc['tables']['users']['columns'], schema['tables']['users']['columns'])

    def test_column_default_survives(self):
        doc = erd.config_document(_domain_tables(), None, None)
        status = next(c for c in doc['tables']['users']['columns'] if c['name'] == 'status')
        self.assertEqual(status['default'], 'active')

    def test_unnamed_index_name_omitted(self):
        doc = erd.config_document(_domain_tables(), None, None)
        ix = doc['tables']['users']['indexes'][0]
        self.assertNotIn('name', ix)
        self.assertEqual(ix['columns'], ['email'])
        self.assertTrue(ix['unique'])

    def test_indexes_match_canonical_projection(self):
        tables = _domain_tables()
        doc = erd.config_document(tables, None, None)
        schema = erd.canonical_schema(tables, None, None)
        self.assertEqual(doc['tables']['users']['indexes'], schema['tables']['users']['indexes'])


# ---------------------------------------------------------------------------
# associations — provenance/sources dropped, dangling pruning + polymorphic
# kept (reuse of the #0 canonical projection's pruning/sort)
# ---------------------------------------------------------------------------
class TestConfigAssociations(unittest.TestCase):
    def test_provenance_and_sources_dropped(self):
        doc = erd.config_document(_domain_tables(), None, None)
        for a in doc['tables']['memberships']['associations']:
            self.assertNotIn('provenance', a)
            self.assertNotIn('sources', a)

    def test_association_keys_allowlisted(self):
        doc = erd.config_document(_domain_tables(), None, None)
        allowed = {'type', 'target', 'name', 'foreign_key', 'through', 'polymorphic'}
        for t in doc['tables'].values():
            for a in t['associations']:
                self.assertLessEqual(set(a), allowed)

    def test_polymorphic_association_with_tableless_target_is_kept(self):
        doc = erd.config_document(_domain_tables(), None, None)
        assocs = doc['tables']['comments']['associations']
        self.assertEqual(len(assocs), 1)
        self.assertEqual(assocs[0]['target'], 'commentables')
        self.assertTrue(assocs[0]['polymorphic'])

    def test_dangling_association_pruned(self):
        tables = _domain_tables()
        tables['memberships']['associations'].append(
            {'type': 'belongs_to', 'name': 'ghost', 'target': 'nonexistent',
             'provenance': 'declared', 'sources': []})
        doc = erd.config_document(tables, None, None)
        targets = {a['target'] for a in doc['tables']['memberships']['associations']}
        self.assertNotIn('nonexistent', targets)

    def test_through_field_preserved(self):
        doc = erd.config_document(_domain_tables(), None, None)
        assocs = doc['tables']['organizations']['associations']
        self.assertEqual(assocs[0]['through'], 'memberships')


# ---------------------------------------------------------------------------
# notes reverse mapping
# ---------------------------------------------------------------------------
class TestConfigNotesReverseMapping(unittest.TestCase):
    def test_global_note(self):
        doc = erd.config_document(_domain_tables(), _domain_notes(), None)
        n = next(n for n in doc['notes'] if n['id'] == 'g1')
        self.assertEqual(n['target'], {'type': 'global'})
        self.assertEqual(n['text'], 'Overview note')

    def test_table_note(self):
        doc = erd.config_document(_domain_tables(), _domain_notes(), None)
        n = next(n for n in doc['notes'] if n['id'] == 't1')
        self.assertEqual(n['target'], {'type': 'table', 'table': 'users'})

    def test_relation_note_carries_all_five_narrowing_keys(self):
        doc = erd.config_document(_domain_tables(), _domain_notes(), None)
        n = next(n for n in doc['notes'] if n['id'] == 'r1')
        target = n['target']
        for key in ('name', 'foreign_key', 'through', 'assoc_type', 'polymorphic'):
            self.assertIn(key, target)
        self.assertEqual(target, {
            'type': 'relation', 'source_table': 'memberships', 'target_table': 'organizations',
            'name': 'org', 'foreign_key': 'org_id', 'through': None,
            'assoc_type': 'belongs_to', 'polymorphic': False,
        })

    def test_relation_note_through_is_explicit_null_not_omitted(self):
        doc = erd.config_document(_domain_tables(), _domain_notes(), None)
        n = next(n for n in doc['notes'] if n['id'] == 'r1')
        self.assertIn('through', n['target'])
        self.assertIsNone(n['target']['through'])

    def test_polymorphic_relation_note_carries_true(self):
        doc = erd.config_document(_domain_tables(), _domain_notes(), None)
        n = next(n for n in doc['notes'] if n['id'] == 'r3')
        self.assertTrue(n['target']['polymorphic'])
        self.assertEqual(n['target']['target_table'], 'commentables')

    def test_notes_sorted_by_id(self):
        doc = erd.config_document(_domain_tables(), _domain_notes(), None)
        self.assertEqual([n['id'] for n in doc['notes']], ['g1', 'r1', 'r2', 'r3', 't1'])


# ---------------------------------------------------------------------------
# groups — passthrough
# ---------------------------------------------------------------------------
class TestConfigGroupsPassthrough(unittest.TestCase):
    def test_groups_shape_matches_config_input_allowlist(self):
        doc = erd.config_document(_domain_tables(), None, _domain_groups())
        self.assertEqual(doc['groups'], [
            {'id': 'gr1', 'title': 'Core', 'tables': ['organizations', 'users'], 'color': '#0d9488'}])


# ---------------------------------------------------------------------------
# JSON / YAML text serialization
# ---------------------------------------------------------------------------
class TestConfigJsonText(unittest.TestCase):
    def test_indent_sort_keys_trailing_newline(self):
        doc = erd.config_document(_domain_tables(), None, None)
        text = erd.config_json_text(doc)
        self.assertTrue(text.endswith('\n'))
        self.assertEqual(json.loads(text), doc)
        # sort_keys=True -> "columns" before "indexes" before "associations"
        # at any one table level (alphabetical)
        self.assertIn('"associations"', text)

    def test_deterministic_regardless_of_dict_order(self):
        t1 = _domain_tables()
        t2 = {k: t1[k] for k in reversed(list(t1))}
        doc1 = erd.config_document(t1, None, None)
        doc2 = erd.config_document(t2, None, None)
        self.assertEqual(erd.config_json_text(doc1), erd.config_json_text(doc2))


@unittest.skipUnless(HAS_YAML, 'PyYAML not installed')
class TestConfigYamlText(unittest.TestCase):
    def test_full_document_round_trips_via_safe_load(self):
        import yaml
        doc = erd.config_document(_domain_tables(), _domain_notes(), _domain_groups(), title='Demo')
        text = erd.config_yaml_text(doc)
        self.assertEqual(yaml.safe_load(text), doc)

    def test_norway_problem_strings_round_trip(self):
        import yaml
        tables = _domain_tables()
        tables['users']['comment'] = 'no'
        doc = erd.config_document(tables, None, None)
        text = erd.config_yaml_text(doc)
        self.assertEqual(yaml.safe_load(text)['tables']['users']['comment'], 'no')

    def test_leading_zero_digit_string_round_trips(self):
        import yaml
        tables = _domain_tables()
        tables['users']['columns'][1]['default'] = '0123'
        doc = erd.config_document(tables, None, None)
        text = erd.config_yaml_text(doc)
        reloaded = yaml.safe_load(text)
        col = next(c for c in reloaded['tables']['users']['columns'] if c['name'] == 'email')
        self.assertEqual(col['default'], '0123')
        self.assertIsInstance(col['default'], str)

    def test_multiline_text_uses_block_style(self):
        notes = [{'id': 'n1', 'scope': 'global', 'text': 'line one\nline two\nline three'}]
        doc = erd.config_document(_domain_tables(), notes, None)
        text = erd.config_yaml_text(doc)
        self.assertIn('|', text)  # literal block style marker present

    def test_multiline_text_round_trips(self):
        import yaml
        notes = [{'id': 'n1', 'scope': 'global', 'text': 'line one\nline two\nline three'}]
        doc = erd.config_document(_domain_tables(), notes, None)
        text = erd.config_yaml_text(doc)
        reloaded = yaml.safe_load(text)
        self.assertEqual(reloaded['notes'][0]['text'], 'line one\nline two\nline three')

    def test_deterministic_regardless_of_dict_order(self):
        t1 = _domain_tables()
        t2 = {k: t1[k] for k in reversed(list(t1))}
        doc1 = erd.config_document(t1, None, None)
        doc2 = erd.config_document(t2, None, None)
        self.assertEqual(erd.config_yaml_text(doc1), erd.config_yaml_text(doc2))

    def test_unicode_survives_unescaped(self):
        tables = _domain_tables()
        tables['users']['comment'] = 'ユーザー'
        doc = erd.config_document(tables, None, None)
        text = erd.config_yaml_text(doc)
        self.assertIn('ユーザー', text)  # allow_unicode=True -> not \uXXXX escaped


# ---------------------------------------------------------------------------
# config.py — Sol relaxation #1: unnamed non-drop indexes accepted
# ---------------------------------------------------------------------------
def _load(cfg_dict):
    tmp = tempfile.TemporaryDirectory()
    try:
        path = Path(tmp.name) / 'c.json'
        path.write_text(json.dumps(cfg_dict))
        args = type('Args', (), {'config': str(path), 'no_config': False})()
        return erd.load_config(args)
    finally:
        tmp.cleanup()


class TestUnnamedIndexAcceptedAtLoad(unittest.TestCase):
    def test_unnamed_non_drop_index_accepted(self):
        cfg = _load({'tables': {'t': {'indexes': [{'columns': ['a'], 'unique': True}]}}})
        ix = cfg['tables']['t']['indexes'][0]
        self.assertNotIn('name', ix)

    def test_unnamed_index_drop_still_rejected(self):
        with self.assertRaises(SystemExit):
            _load({'tables': {'t': {'indexes': [{'columns': ['a'], 'drop': True}]}}})


# ---------------------------------------------------------------------------
# providers.py — Sol relaxation #2: null-vs-absent narrowing
# ---------------------------------------------------------------------------
class TestNullNarrowing(unittest.TestCase):
    def _two_relations_differing_only_by_through(self):
        return {
            'users': {'columns': [{'name': 'id'}], 'associations': []},
            'orgs': {'columns': [{'name': 'id'}], 'associations': []},
            'memberships': {
                'columns': [{'name': 'id'}],
                'associations': [
                    {'type': 'has_many', 'name': 'members', 'target': 'users'},
                    {'type': 'has_many', 'name': 'members', 'target': 'users', 'through': 'invites'},
                ],
            },
        }

    def test_absent_key_is_wildcard_and_stays_ambiguous(self):
        notes = [{'id': 'n1', 'target': {'type': 'relation', 'source_table': 'memberships',
                                         'target_table': 'users', 'name': 'members'}, 'text': 'hi'}]
        with self.assertRaises(SystemExit):
            erd.resolve_and_validate_notes(notes, self._two_relations_differing_only_by_through(), 'cfg')

    def test_explicit_null_through_narrows_to_the_one_without_through(self):
        notes = [{'id': 'n1', 'target': {'type': 'relation', 'source_table': 'memberships',
                                         'target_table': 'users', 'name': 'members',
                                         'through': None}, 'text': 'hi'}]
        out = erd.resolve_and_validate_notes(notes, self._two_relations_differing_only_by_through(), 'cfg')
        self.assertIsNone(out[0]['through'])

    def test_explicit_through_value_narrows_to_the_one_with_through(self):
        notes = [{'id': 'n1', 'target': {'type': 'relation', 'source_table': 'memberships',
                                         'target_table': 'users', 'name': 'members',
                                         'through': 'invites'}, 'text': 'hi'}]
        out = erd.resolve_and_validate_notes(notes, self._two_relations_differing_only_by_through(), 'cfg')
        self.assertEqual(out[0]['through'], 'invites')

    def test_full_narrowing_key_set_never_collides_on_the_full_domain(self):
        # every relation note in the domain fixture uses the FULL five-key
        # narrowing set (as config_document emits it) — none should collide
        # with each other, even though 'org'/'member' share source+target
        # shape and 'commentable' shares nothing but the pattern.
        tables = _domain_tables()
        doc_notes = erd.config_document(tables, _domain_notes(), None)['notes']
        cfg_notes = [n for n in doc_notes if n['target']['type'] == 'relation']
        out = erd.resolve_and_validate_notes(cfg_notes, tables, 'cfg')
        self.assertEqual(len(out), 3)


# ---------------------------------------------------------------------------
# providers.py — Sol relaxation #3: polymorphic target exemption
# ---------------------------------------------------------------------------
class TestPolymorphicTargetExemption(unittest.TestCase):
    def test_polymorphic_association_target_not_required_to_exist(self):
        tables = {'comments': {'columns': [{'name': 'id'}, {'name': 'commentable_id'}],
                               'associations': []}}
        config_tables = {'comments': {'associations': [
            {'type': 'belongs_to', 'name': 'commentable', 'target': 'ghosts',
             'foreign_key': 'commentable_id', 'polymorphic': True}]}}
        # must NOT raise
        erd.validate_config_references(config_tables, tables, 'cfg')

    def test_non_polymorphic_association_target_still_required(self):
        tables = {'comments': {'columns': [{'name': 'id'}], 'associations': []}}
        config_tables = {'comments': {'associations': [
            {'type': 'belongs_to', 'name': 'owner', 'target': 'ghosts', 'foreign_key': 'owner_id'}]}}
        with self.assertRaises(SystemExit):
            erd.validate_config_references(config_tables, tables, 'cfg')


# ---------------------------------------------------------------------------
# providers.py — Sol relaxation #4: schema_missing FK-column exemption
# ---------------------------------------------------------------------------
class TestSchemaMissingFkExemption(unittest.TestCase):
    def test_schema_missing_table_fk_column_not_required(self):
        tables = {'comments': {'columns': [], 'associations': [], 'schema_missing': True},
                  'users': {'columns': [{'name': 'id'}], 'associations': []}}
        config_tables = {'comments': {'associations': [
            {'type': 'belongs_to', 'name': 'author', 'target': 'users', 'foreign_key': 'author_id'}]}}
        # must NOT raise, even though 'author_id' names no real column
        erd.validate_config_references(config_tables, tables, 'cfg')

    def test_normal_table_fk_column_still_required(self):
        tables = {'comments': {'columns': [{'name': 'id'}], 'associations': []},
                  'users': {'columns': [{'name': 'id'}], 'associations': []}}
        config_tables = {'comments': {'associations': [
            {'type': 'belongs_to', 'name': 'author', 'target': 'users', 'foreign_key': 'author_id'}]}}
        with self.assertRaises(SystemExit):
            erd.validate_config_references(config_tables, tables, 'cfg')


# ---------------------------------------------------------------------------
# Level1 round-trip integration tests — drive the REAL pipeline via main(),
# no DB (config-only), mirroring tests/test_notes.py's _NoDBDriver.
# ---------------------------------------------------------------------------
class _NoDBDriver(unittest.TestCase):
    def setUp(self):
        def _no_db(url):
            raise AssertionError('DB should not be contacted in a config-only test')
        self._orig = (erd.parse_mysql, erd.parse_postgres)
        erd.parse_mysql = _no_db
        erd.parse_postgres = _no_db
        self._argv = sys.argv
        self._cwd = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: setattr(sys, 'argv', self._argv))
        self.addCleanup(os.chdir, self._cwd)
        self.addCleanup(lambda: (setattr(erd, 'parse_mysql', self._orig[0]),
                                 setattr(erd, 'parse_postgres', self._orig[1])))
        os.chdir(self.tmp.name)

    def _p(self, name):
        return str(Path(self.tmp.name) / name)

    def _run(self, *argv):
        sys.argv = ['erd.py', *argv]
        err_buf = io.StringIO()
        with redirect_stderr(err_buf):
            erd.main()
        return err_buf.getvalue()

    def _reimport_schema(self, document, filename='reimport.json'):
        """Write a config_document as JSON, reimport it via --config (no DB,
        no --models — config-only), and return the resulting --emit-json
        `schema` for level1 comparison against the ORIGINAL, hand-built IR
        it came from."""
        cfg_path = self._p(filename)
        Path(cfg_path).write_text(json.dumps(document), encoding='utf-8')
        out = self._p('out.html')
        snap = self._p('snap.json')
        self._run('--config', cfg_path, '-o', out, '--emit-json', snap)
        return json.loads(Path(snap).read_text())['schema']


def _level1(schema):
    """Reduce an --emit-json `schema` to the material-equivalence view used
    for round-trip comparison (per the backlog #1 spec): primary-key column
    SETS, column (type, nullable, default), indexes as (columns, unique)
    SETS, associations as (type, target, foreign_key, through, polymorphic)
    SETS (name deliberately excluded — it is not part of the level1
    contract), and table comment. Provenance/sources/fk_columns/
    schema_missing are already gone from an --emit-json schema."""
    out = {}
    for tname, t in schema['tables'].items():
        primary = frozenset(c['name'] for c in t['columns'] if c.get('primary'))
        columns = {c['name']: (c.get('type', ''), bool(c.get('nullable', False)), c.get('default'))
                   for c in t['columns']}
        indexes = frozenset((frozenset(ix['columns']), bool(ix.get('unique', False)))
                            for ix in t['indexes'])
        assocs = frozenset((a['type'], a.get('target'), a.get('foreign_key'), a.get('through'),
                            bool(a.get('polymorphic'))) for a in t['associations'])
        out[tname] = {'primary': primary, 'columns': columns, 'indexes': indexes,
                      'associations': assocs, 'comment': t.get('comment')}
    return out


class TestLevel1RoundTrip(_NoDBDriver):
    def test_full_domain_level1_round_trip(self):
        tables = _domain_tables()
        notes = _domain_notes()
        groups = _domain_groups()
        baseline = erd.canonical_schema(tables, notes, groups)
        document = erd.config_document(tables, notes, groups, title='Demo')
        reimported = self._reimport_schema(document)

        self.assertEqual(set(baseline['tables']), set(reimported['tables']))
        self.assertEqual(_level1(baseline), _level1(reimported))
        # notes/groups: empirically exact (not just level1) — every
        # narrowing key round-trips through the null-vs-absent resolver fix
        self.assertEqual(baseline['notes'], reimported['notes'])
        self.assertEqual(baseline['groups'], reimported['groups'])

    def test_rails_standalone_schema_missing_round_trip(self):
        # A Rails-only logical table (no DB): 0 columns, a belongs_to with
        # ONLY the convention FK column name (no real column to back it) —
        # must reimport without an FK-column-existence error (relaxation #4).
        tables = {
            'users': {'columns': [_col('id', primary=True)], 'indexes': [], 'associations': []},
            'comments': {'columns': [], 'indexes': [], 'associations': [
                {'type': 'belongs_to', 'name': 'author', 'target': 'users',
                 'foreign_key': 'author_id', 'provenance': 'declared',
                 'sources': [{'kind': 'framework', 'provider': 'rails'}]},
            ], 'schema_missing': True},
        }
        baseline = erd.canonical_schema(tables, None, None)
        document = erd.config_document(tables, None, None)
        self.assertNotIn('comment', document['tables']['comments'])
        self.assertEqual(document['tables']['comments']['columns'], [])
        reimported = self._reimport_schema(document)
        self.assertEqual(_level1(baseline), _level1(reimported))
        # the association survives with its convention FK, despite no real
        # 'author_id' column ever existing on the reimported table either
        assocs = reimported['tables']['comments']['associations']
        self.assertEqual(assocs, [{'type': 'belongs_to', 'target': 'users', 'name': 'author',
                                   'foreign_key': 'author_id', 'provenance': 'manual',
                                   'sources': [{'kind': 'config', 'provider': 'config'}]}])

    @unittest.skipUnless(HAS_YAML, 'PyYAML not installed')
    def test_yaml_round_trip_matches_json_round_trip(self):
        tables = _domain_tables()
        notes = _domain_notes()
        groups = _domain_groups()
        document = erd.config_document(tables, notes, groups, title='Demo')

        json_reimported = self._reimport_schema(document, filename='r.json')

        yaml_path = self._p('r.yml')
        Path(yaml_path).write_text(erd.config_yaml_text(document), encoding='utf-8')
        out = self._p('out2.html')
        snap = self._p('snap2.json')
        self._run('--config', yaml_path, '-o', out, '--emit-json', snap)
        yaml_reimported = json.loads(Path(snap).read_text())['schema']

        self.assertEqual(_level1(json_reimported), _level1(yaml_reimported))
        self.assertEqual(json_reimported['notes'], yaml_reimported['notes'])


# ---------------------------------------------------------------------------
# CLI wiring — driven through main(), mirroring tests/test_emit_json.py's
# _EmitJsonDriver technique (stubbed parse_mysql, a temp cwd, sys.argv).
# ---------------------------------------------------------------------------
def _dbcol(t, name, dtype, ctype, null='YES', key='', default='', extra='', comment=''):
    return (t, name, dtype, ctype, null, key, default, extra, comment)


TABLE_ROWS = [('users', ''), ('posts', '')]
COL_ROWS = [
    _dbcol('users', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
    _dbcol('users', 'email', 'varchar', 'varchar(255)', 'NO', 'UNI'),
    _dbcol('posts', 'id', 'bigint', 'bigint', 'NO', 'PRI', '', 'auto_increment'),
    _dbcol('posts', 'user_id', 'bigint', 'bigint', 'NO', 'MUL'),
    _dbcol('posts', 'title', 'varchar', 'varchar(255)', 'NO'),
]
FK_ROWS = [('posts', 'user_id', 'users')]
INDEX_ROWS = [('users', 'PRIMARY', 0, 1, 'id')]


class _EmitConfigDriver(unittest.TestCase):
    def setUp(self):
        self._orig_parse = erd.parse_mysql
        self._orig_argv = sys.argv
        self._orig_cwd = os.getcwd()
        self.addCleanup(lambda: setattr(erd, 'parse_mysql', self._orig_parse))
        self.addCleanup(lambda: setattr(sys, 'argv', self._orig_argv))
        self.addCleanup(os.chdir, self._orig_cwd)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.chdir(self.tmp.name)
        erd.parse_mysql = lambda url: erd.mysql_ir(TABLE_ROWS, COL_ROWS, FK_ROWS, INDEX_ROWS)

    def _run(self, *cli_args, capture_stdout=False, capture_stderr=False):
        sys.argv = ['erd.py', 'mysql://x@localhost/testdb', *cli_args]
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            erd.main()
        return out_buf.getvalue(), err_buf.getvalue()


class TestCLIExtensionDispatch(_EmitConfigDriver):
    def test_json_extension_writes_json(self):
        self._run('--emit-config', 'schema.json')
        doc = json.loads((Path(self.tmp.name) / 'schema.json').read_text())
        self.assertEqual(doc['version'], 1)

    def test_dash_stdout_is_always_json(self):
        out, _ = self._run('--emit-config', '-')
        doc = json.loads(out)
        self.assertEqual(doc['version'], 1)

    def test_unknown_extension_errors(self):
        with self.assertRaises(SystemExit):
            self._run('--emit-config', 'schema.txt')

    def test_unknown_extension_errors_before_any_file_is_written(self):
        with self.assertRaises(SystemExit):
            self._run('-o', 'out.html', '--emit-config', 'schema.txt')
        self.assertFalse((Path(self.tmp.name) / 'out.html').exists())

    @unittest.skipUnless(HAS_YAML, 'PyYAML not installed')
    def test_yml_extension_writes_yaml(self):
        self._run('--emit-config', 'schema.yml')
        import yaml
        doc = yaml.safe_load((Path(self.tmp.name) / 'schema.yml').read_text())
        self.assertEqual(doc['version'], 1)

    @unittest.skipUnless(HAS_YAML, 'PyYAML not installed')
    def test_yaml_extension_writes_yaml(self):
        self._run('--emit-config', 'schema.yaml')
        import yaml
        doc = yaml.safe_load((Path(self.tmp.name) / 'schema.yaml').read_text())
        self.assertEqual(doc['version'], 1)

    def test_yml_without_pyyaml_errors_with_no_json_fallback(self):
        with _NoPyYAML():
            with self.assertRaises(SystemExit) as cm:
                self._run('--emit-config', 'schema.yml')
        self.assertIn('PyYAML', str(cm.exception))
        # no fallback file of any format was written
        self.assertFalse((Path(self.tmp.name) / 'schema.yml').exists())

    def test_generated_path_reported_on_stderr(self):
        _, err = self._run('--emit-config', 'schema.json')
        self.assertIn('Generated: schema.json', err)


# ---------------------------------------------------------------------------
# Fail-fast ordering: --emit-config's extension/PyYAML validation must run
# BEFORE the DB layer is built (_run_pipeline calls _emit_config_format up
# front, before db_provider), not only later in _finish after a possibly-slow
# DB introspection has already happened. Uses a real sqlite:// URL pointing at
# a file that does NOT exist — no monkeypatching needed: if the fail-fast
# check regressed back to running only in _finish, the DB layer would run
# FIRST and raise sqlite's own "database file not found" SystemExit instead,
# which carries a different message than the --emit-config one, catching the
# ordering bug via message content.
# ---------------------------------------------------------------------------
class TestEmitConfigFailsFastBeforeDB(unittest.TestCase):
    def setUp(self):
        self._orig_argv = sys.argv
        self._orig_cwd = os.getcwd()
        self.addCleanup(lambda: setattr(sys, 'argv', self._orig_argv))
        self.addCleanup(os.chdir, self._orig_cwd)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.chdir(self.tmp.name)

    def _run(self, *cli_args):
        # a sqlite URL to a file that is never created — if the DB layer were
        # reached at all, this would fail with "sqlite database file not
        # found", not the --emit-config error under test
        sys.argv = ['erd.py', 'sqlite:///does-not-exist.db', *cli_args]
        err_buf = io.StringIO()
        with redirect_stderr(err_buf):
            erd.main()

    def test_unknown_extension_errors_before_db_connection(self):
        with self.assertRaises(SystemExit) as cm:
            self._run('--emit-config', 'schema.txt')
        msg = str(cm.exception)
        self.assertIn('--emit-config', msg)
        self.assertIn('must end in', msg)
        self.assertNotIn('sqlite database file not found', msg)

    def test_unknown_extension_errors_before_output_file_is_written(self):
        with self.assertRaises(SystemExit):
            self._run('-o', 'out.html', '--emit-config', 'schema.txt')
        self.assertFalse((Path(self.tmp.name) / 'out.html').exists())

    def test_pyyaml_missing_errors_before_db_connection(self):
        with _NoPyYAML():
            with self.assertRaises(SystemExit) as cm:
                self._run('--emit-config', 'schema.yml')
        msg = str(cm.exception)
        self.assertIn('PyYAML', msg)
        self.assertNotIn('sqlite database file not found', msg)


class TestCLICollisionGuard(_EmitConfigDriver):
    def test_emit_config_colliding_with_output_errors(self):
        with self.assertRaises(SystemExit):
            self._run('-o', 'same.json', '--emit-config', 'same.json')

    def test_emit_config_colliding_with_emit_json_errors(self):
        with self.assertRaises(SystemExit):
            self._run('--emit-json', 'same.json', '--emit-config', 'same.json')

    def test_emit_config_colliding_with_excel_errors(self):
        with self.assertRaises(SystemExit):
            self._run('--emit-config', 'same.xlsx', '--excel', 'same.xlsx')

    def test_distinct_paths_and_stdout_do_not_collide(self):
        out, _ = self._run('-o', 'erd.html', '--emit-json', 'snap.json', '--emit-config', '-')
        self.assertEqual(json.loads(out)['version'], 1)


class TestCLICoexistenceAndByteEquality(_EmitConfigDriver):
    def test_emit_config_coexists_with_emit_json_and_excel(self):
        self._run('--emit-config', 'schema.json', '--emit-json', 'snap.json', '--excel', 'defs.xlsx')
        self.assertTrue((Path(self.tmp.name) / 'erd.html').exists())
        self.assertTrue((Path(self.tmp.name) / 'schema.json').exists())
        self.assertTrue((Path(self.tmp.name) / 'snap.json').exists())
        self.assertTrue((Path(self.tmp.name) / 'defs.xlsx').exists())

    def test_html_byte_identical_with_and_without_emit_config(self):
        self._run('-o', 'a.html')
        self._run('-o', 'b.html', '--emit-config', 'schema.json')
        self.assertEqual((Path(self.tmp.name) / 'a.html').read_bytes(),
                         (Path(self.tmp.name) / 'b.html').read_bytes())

    def test_excel_byte_identical_with_and_without_emit_config(self):
        self._run('-o', 'a.html', '--excel', 'a.xlsx')
        self._run('-o', 'b.html', '--excel', 'b.xlsx', '--emit-config', 'schema.json')
        self.assertEqual((Path(self.tmp.name) / 'a.xlsx').read_bytes(),
                         (Path(self.tmp.name) / 'b.xlsx').read_bytes())


if __name__ == '__main__':
    unittest.main()
