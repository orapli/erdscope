"""Browser-driven E2E tests for the client-side JS baked into the generated
HTML (grid layout, multi-select align/distribute, drag-to-snap, Auto-tidy).
These exercise the exact
bytes that ship — no extraction, no stubbing — by loading the real output
file in a real (headless) browser and driving it like a user would.

Requires the optional `playwright` package + a downloaded Chromium build;
skipped automatically otherwise (same soft-dependency pattern as the
openpyxl roundtrip test in test_erd.py):

    pip install playwright
    playwright install chromium
    python3 -m unittest tests.test_e2e -v

This is deliberately the thin tip of the testing pyramid/trophy: a handful
of high-value interaction tests, not exhaustive UI coverage. Parser and IR
correctness belong in test_erd.py's unit/integration layer.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location('erd', ROOT / 'erd.py')
erd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(erd)

try:
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except ImportError:
    HAVE_PLAYWRIGHT = False


# information_schema-shaped fixture: a hub ('users') with four spokes, so
# the overview exercises gridLayout's row-packing rather than a trivial
# single-pair layout. 'posts' carries a non-key column so header-only mode
# (colMode=2) visibly shrinks it, which the Auto-tidy test relies on.
TABLE_ROWS = [('users', ''), ('posts', ''), ('comments', ''),
              ('likes', ''), ('audit_logs', '')]
def _col(t, name, dtype='bigint', ctype='bigint', null='NO', key=''):
    return (t, name, dtype, ctype, null, key, '', '', '')
COL_ROWS = [
    _col('users', 'id', key='PRI'),
    _col('users', 'email', dtype='varchar', ctype='varchar(255)'),
    _col('posts', 'id', key='PRI'),
    _col('posts', 'user_id', key='MUL'),
    _col('posts', 'title', dtype='varchar', ctype='varchar(200)'),
    _col('comments', 'id', key='PRI'),
    _col('comments', 'user_id', key='MUL'),
    _col('likes', 'id', key='PRI'),
    _col('likes', 'user_id', key='MUL'),
    _col('audit_logs', 'id', key='PRI'),
    _col('audit_logs', 'user_id', key='MUL'),
]
FK_ROWS = [
    ('posts', 'user_id', 'users'),
    ('comments', 'user_id', 'users'),
    ('likes', 'user_id', 'users'),
    ('audit_logs', 'user_id', 'users'),
]
INDEX_ROWS = [('users', 'PRIMARY', 0, 1, 'id')]


def _build_html():
    tables = erd.mysql_ir(TABLE_ROWS, COL_ROWS, FK_ROWS, INDEX_ROWS)
    args = SimpleNamespace(output='', models=None, excel=None, max_rows=15,
                            only=None, exclude=None, infer_fk=False)
    tmp = tempfile.mkdtemp()
    out = Path(tmp) / 'out.html'
    args.output = str(out)
    erd._finish(tables, args, 'e2e_fixture')
    return out


def _build_html_with_isolated_table():
    # same base schema as _build_html(), plus 'settings': no FK to/from
    # anything, so it lands in the isolated ("singles") bucket gridLayout
    # and the incremental-add placer both special-case
    table_rows = TABLE_ROWS + [('settings', '')]
    col_rows = COL_ROWS + [_col('settings', 'id', key='PRI'), _col('settings', 'key', dtype='varchar', ctype='varchar(100)')]
    tables = erd.mysql_ir(table_rows, col_rows, FK_ROWS, INDEX_ROWS)
    args = SimpleNamespace(output='', models=None, excel=None, max_rows=15,
                            only=None, exclude=None, infer_fk=False)
    tmp = tempfile.mkdtemp()
    out = Path(tmp) / 'out.html'
    args.output = str(out)
    erd._finish(tables, args, 'e2e_fixture')
    return out


def _build_html_with_multiple_isolated_tables():
    # four isolated tables, so repeated single-table additions (checkbox
    # clicks or search+Enter, one at a time) can be checked for whether
    # they accumulate into one column or march further right each time
    names = ['settings_a', 'settings_b', 'settings_c', 'settings_d']
    table_rows = TABLE_ROWS + [(n, '') for n in names]
    col_rows = COL_ROWS + [_col(n, 'id', key='PRI') for n in names]
    tables = erd.mysql_ir(table_rows, col_rows, FK_ROWS, INDEX_ROWS)
    args = SimpleNamespace(output='', models=None, excel=None, max_rows=15,
                            only=None, exclude=None, infer_fk=False)
    tmp = tempfile.mkdtemp()
    out = Path(tmp) / 'out.html'
    args.output = str(out)
    erd._finish(tables, args, 'e2e_fixture')
    return out


def _build_html_with_comments():
    # users: short English comment (typical case). posts: Japanese comment
    # long enough to exercise the 16-display-width-unit truncation cap
    # (8 full-width chars == 16 units). Others: no comment, unaffected.
    table_rows = [
        ('users', 'Customer accounts'),
        ('posts', '投稿記事管理テーブル（本番用）'),
        ('comments', ''), ('likes', ''), ('audit_logs', ''),
    ]
    tables = erd.mysql_ir(table_rows, COL_ROWS, FK_ROWS, INDEX_ROWS)
    args = SimpleNamespace(output='', models=None, excel=None, max_rows=15,
                            only=None, exclude=None, infer_fk=False)
    tmp = tempfile.mkdtemp()
    out = Path(tmp) / 'out.html'
    args.output = str(out)
    erd._finish(tables, args, 'e2e_fixture')
    return out


def _build_html_with_notes():
    # notes Phase 1: exercise the real resolve_and_validate_notes ->
    # _finish(notes=...) path, not a hand-rolled DATA.notes shape — this is
    # what actually ships. 'users' carries a table note, the posts->users
    # belongs_to (FK user_id) carries a relation note, and one global note
    # with an http(s) link rounds out all three scopes.
    tables = erd.mysql_ir(TABLE_ROWS, COL_ROWS, FK_ROWS, INDEX_ROWS)
    notes_cfg = [
        {'id': 'n-table', 'target': {'type': 'table', 'table': 'users'},
         'title': 'User retention', 'text': 'Do not delete without archiving first.'},
        {'id': 'n-rel', 'target': {'type': 'relation', 'source_table': 'posts',
                                   'target_table': 'users', 'foreign_key': 'user_id'},
         'text': 'Posts survive user anonymization.'},
        {'id': 'n-global', 'target': {'type': 'global'},
         'title': 'Diagram conventions',
         'text': 'Teal badges mark manually-declared associations.',
         'links': [{'label': 'ADR-1', 'url': 'https://example.com/adr/1'}]},
    ]
    args = SimpleNamespace(output='', models=None, excel=None, max_rows=15,
                            only=None, exclude=None, infer_fk=False)
    tmp = tempfile.mkdtemp()
    out = Path(tmp) / 'out.html'
    args.output = str(out)
    # _finish resolves/validates the RAW config notes itself (after infer_fk,
    # before --only/--exclude) and stamps the relation `type` per the viewer
    # contract — so hand it the raw notes_cfg, exactly like the real pipeline.
    erd._finish(tables, args, 'e2e_fixture', notes=notes_cfg, notes_label='test')
    return out


def _build_html_with_xss_notes():
    # DOM-level XSS regression (Sol finding 6): notes are attacker-reachable
    # free text (anyone who can edit config, not just a trusted maintainer,
    # writes title/text/link-label). The existing regression coverage only
    # round-trips these strings through JSON; it never renders them through
    # innerHTML in a real browser, which is where the safety property
    # (esc()/escMark() on every field, never raw) actually has to hold.
    # One table note, one relation note, one global note, each carrying a
    # different mix of the payloads under review: a script-tag close+reopen,
    # an onerror-bearing tag, a quote+tag attribute escape, and a bare
    # attribute-injection attempt in a link label.
    tables = erd.mysql_ir(TABLE_ROWS, COL_ROWS, FK_ROWS, INDEX_ROWS)
    notes_cfg = [
        {'id': 'n-table-xss', 'target': {'type': 'table', 'table': 'users'},
         'title': '</script><script>window.__xss_title=1</script>',
         'text': '<img src=x onerror="window.__xss_text=1">',
         'links': [{'label': '"><b>esc</b>', 'url': 'https://example.com/safe1'},
                   {'label': '" onmouseover=alert(1) foo="', 'url': 'https://example.com/safe2'}]},
        {'id': 'n-rel-xss', 'target': {'type': 'relation', 'source_table': 'posts',
                                        'target_table': 'users', 'foreign_key': 'user_id'},
         'title': '"><b>rel-esc</b>',
         'text': '" onmouseover=alert(1)</script><script>window.__xss_rel=1</script>'},
        {'id': 'n-global-xss', 'target': {'type': 'global'},
         'title': '<b>global title</b>',
         'text': '<img src=x onerror="window.__xss_global=1">'},
    ]
    args = SimpleNamespace(output='', models=None, excel=None, max_rows=15,
                            only=None, exclude=None, infer_fk=False)
    tmp = tempfile.mkdtemp()
    out = Path(tmp) / 'out.html'
    args.output = str(out)
    erd._finish(tables, args, 'e2e_fixture', notes=notes_cfg, notes_label='test')
    return out


@unittest.skipUnless(HAVE_PLAYWRIGHT, 'playwright not installed')
class TestNotes(unittest.TestCase):
    """notes Phase 1 — right-pane table/relation notes, the global note in
    the legend, search integration, and the documented hidden-table
    interaction (Fable review point 4: a banned table's note disappears
    from the right pane, but the global note's legend entry point survives)."""
    @classmethod
    def setUpClass(cls):
        cls.html_path = _build_html_with_notes()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f'Chromium not available: {e}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.goto(self.html_path.as_uri())
        self.page.wait_for_function('typeof nodePos.users !== "undefined"')
        self.page.wait_for_timeout(50)

    def tearDown(self):
        self.page.close()

    def _ban(self, name):
        self.page.evaluate('''(name) => {
            const item = [...document.querySelectorAll('.table-item')]
                .find(el => el.querySelector('.tname')?.textContent === name);
            item.querySelector('.hide-btn').click();
        }''', name)

    def _uncheck(self, name):
        self.page.evaluate('''(name) => {
            const item = [...document.querySelectorAll('.table-item')]
                .find(el => el.querySelector('.tname')?.textContent === name);
            item.querySelector('input[type=checkbox]').click();
        }''', name)

    def test_table_note_shown_in_right_pane(self):
        self.page.click('.er-node[data-name="users"]')
        self.page.wait_for_timeout(50)
        text = self.page.inner_text('#table-details')
        self.assertIn('User retention', text)
        self.assertIn('Do not delete without archiving first.', text)

    def test_relation_note_shown_on_the_assoc_entry(self):
        self.page.click('.er-node[data-name="posts"]')
        self.page.wait_for_timeout(50)
        text = self.page.inner_text('#table-details')
        self.assertIn('Posts survive user anonymization.', text)

    def test_global_note_shown_in_legend_with_working_link(self):
        text = self.page.inner_text('#legend-notes')
        self.assertIn('Diagram conventions', text)
        self.assertIn('Teal badges mark manually-declared associations.', text)
        href = self.page.get_attribute('#legend-notes .note-links a', 'href')
        self.assertEqual(href, 'https://example.com/adr/1')

    def test_search_surfaces_a_note_hit_badge(self):
        self.page.fill('#search', 'archiving')
        self.page.wait_for_timeout(50)
        badge = self.page.evaluate(
            "document.querySelector('.table-item .note-hit')?.textContent")
        self.assertIsNotNone(badge)
        self.assertIn('note', badge)

    def test_global_note_search_shows_banner(self):
        self.page.fill('#search', 'Teal badges')
        self.page.wait_for_timeout(50)
        banner = self.page.evaluate(
            "document.querySelector('.note-banner')?.textContent")
        self.assertIsNotNone(banner)
        self.assertIn('global note', banner)

    def test_banned_table_note_is_unreachable_but_global_note_survives(self):
        # Ban 'users' (🚫) BEFORE it's ever selected: its node leaves the
        # diagram entirely, so there is no way to select it and no way for
        # its table note to reach the right pane — the documented behavior
        # (Fable review point 4). The global note's legend entry point is a
        # separate, always-available block and must be unaffected.
        self._ban('users')
        self.page.wait_for_timeout(50)
        self.assertIsNone(
            self.page.evaluate('document.querySelector(\'.er-node[data-name="users"]\')'),
            'a banned table should no longer be a selectable diagram node')
        self.page.click('.er-node[data-name="posts"]')
        self.page.wait_for_timeout(50)
        text = self.page.inner_text('#table-details')
        self.assertNotIn('User retention', text)  # unreachable now that users is banned
        self.assertIn('Posts survive user anonymization.', text)  # posts' own relation note is unaffected
        self.assertIn('Diagram conventions', self.page.inner_text('#legend-notes'))

    def test_banning_an_already_selected_tables_note_disappears_from_the_right_pane(self):
        # Sol review finding 2, the actual regression: select 'users' FIRST
        # (its note is now in the right pane), THEN ban it. selectedTables
        # isn't cleared by toggleBan() for a plain (non-focused) selection,
        # so the anchor table stays "selected" while no longer being part of
        # getDisplayTables() — showDetails() must notice that itself. The
        # pre-existing "banned before ever selected" test above can't catch
        # this: it never lets 'users' become selected in the first place.
        self.page.click('.er-node[data-name="users"]')
        self.page.wait_for_timeout(50)
        self.assertIn('User retention', self.page.inner_text('#table-details'))
        self._ban('users')
        self.page.wait_for_timeout(50)
        text = self.page.inner_text('#table-details')
        self.assertNotIn('User retention', text)
        self.assertNotIn('Do not delete without archiving first.', text)

    def test_unchecking_an_already_selected_tables_note_disappears_from_the_right_pane(self):
        # Same regression, via the lighter "exclude" path (unchecking the list
        # checkbox) instead of a full ban — the SPEC draws no distinction
        # between the two ways of leaving the display set. Sol re-review #1:
        # the checkbox `change` handler now calls showDetails() itself, so the
        # note must vanish immediately on uncheck — no unrelated redraw needed.
        self.page.click('.er-node[data-name="users"]')
        self.page.wait_for_timeout(50)
        self.assertIn('User retention', self.page.inner_text('#table-details'))
        self._uncheck('users')
        self.page.wait_for_timeout(50)
        text = self.page.inner_text('#table-details')
        self.assertNotIn('User retention', text)
        self.assertNotIn('Do not delete without archiving first.', text)

    def test_highlight_matches_a_table_note(self):
        # Sol review finding 4: wordHit() used to only look at table/column
        # names and comments, so a query that only appears in a note's text
        # produced zero hits, dimmed every node, and made Enter-to-cycle a
        # no-op. 'archiving' only appears in users' table note.
        self.page.fill('#word-search', 'archiving')
        self.page.wait_for_timeout(250)
        hit = self.page.evaluate(
            "[...document.querySelectorAll('.er-node.word-hit')].map(n=>n.dataset.name)")
        self.assertEqual(hit, ['users'])
        self.assertEqual(
            self.page.evaluate("document.getElementById('word-search-count').textContent"), '1')

    def test_highlight_matches_a_relation_note_on_its_source_table(self):
        # 'anonymization' only appears in the posts->users relation note,
        # which is attached to 'posts' (the source_table / FK-holding side).
        self.page.fill('#word-search', 'anonymization')
        self.page.wait_for_timeout(250)
        hit = self.page.evaluate(
            "[...document.querySelectorAll('.er-node.word-hit')].map(n=>n.dataset.name)")
        self.assertEqual(hit, ['posts'])

    def test_highlight_does_not_match_a_global_note(self):
        # A global note has no owning table/node, so it's correctly outside
        # Highlight's reach (which can only mark diagram nodes) — it's only
        # discoverable via the left-pane filter's banner row (see
        # test_global_note_search_shows_banner above). 'conventions' is
        # unique to the global note's title; no table/column/other-note text
        # contains it.
        self.page.fill('#word-search', 'conventions')
        self.page.wait_for_timeout(250)
        hit = self.page.evaluate(
            "[...document.querySelectorAll('.er-node.word-hit')].map(n=>n.dataset.name)")
        self.assertEqual(hit, [])
        self.assertEqual(
            self.page.evaluate("document.getElementById('word-search-count').textContent"), '0')


@unittest.skipUnless(HAVE_PLAYWRIGHT, 'playwright not installed')
class TestNotesXSS(unittest.TestCase):
    """Sol review finding 6: notes are rendered via innerHTML (esc()/escMark()
    on every field, same discipline as the rest of the right pane), but the
    only existing coverage was a JSON round-trip of dangerous strings — it
    never actually rendered them in a browser. These tests do: malicious
    text/title/link-label content must show up as literal, inert text and
    must never execute or produce a live element/attribute."""
    @classmethod
    def setUpClass(cls):
        cls.html_path = _build_html_with_xss_notes()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f'Chromium not available: {e}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.goto(self.html_path.as_uri())
        self.page.wait_for_function('typeof nodePos.users !== "undefined"')
        self.page.wait_for_timeout(50)

    def tearDown(self):
        self.page.close()

    def _no_stray_event_attrs(self, container_selector):
        return self.page.evaluate('''(sel) => {
            const root = document.querySelector(sel);
            if (!root) return true;
            return ![...root.querySelectorAll('*')].some(
                el => el.getAttributeNames().some(a => a.startsWith('on')));
        }''', container_selector)

    def test_table_note_xss_payloads_are_escaped_not_executed(self):
        self.page.click('.er-node[data-name="users"]')
        self.page.wait_for_timeout(50)
        # nothing executed
        self.assertIsNone(self.page.evaluate('window.__xss_title'))
        self.assertIsNone(self.page.evaluate('window.__xss_text'))
        # the tags never became live elements
        self.assertIsNone(self.page.evaluate(
            "document.querySelector('#table-details .note-list img')"))
        self.assertIsNone(self.page.evaluate(
            "document.querySelector('#table-details .note-list script')"))
        self.assertIsNone(self.page.evaluate(
            "document.querySelector('#table-details .note-list b')"))
        self.assertTrue(self._no_stray_event_attrs('#table-details .note-list'))
        # the raw payload is visible as literal text, proving it was escaped
        # rather than silently dropped
        text = self.page.inner_text('#table-details .note-list')
        self.assertIn('<script>window.__xss_title=1</script>', text)
        self.assertIn('<img src=x onerror="window.__xss_text=1">', text)
        self.assertIn('<b>esc</b>', text)  # from the first link's label

    def test_relation_note_xss_payloads_are_escaped_not_executed(self):
        self.page.click('.er-node[data-name="posts"]')
        self.page.wait_for_timeout(50)
        self.assertIsNone(self.page.evaluate('window.__xss_rel'))
        self.assertIsNone(self.page.evaluate(
            "document.querySelector('#table-details .assoc-notes b')"))
        self.assertIsNone(self.page.evaluate(
            "document.querySelector('#table-details .assoc-notes script')"))
        self.assertTrue(self._no_stray_event_attrs('#table-details .assoc-notes'))
        text = self.page.inner_text('#table-details .assoc-notes')
        self.assertIn('<b>rel-esc</b>', text)
        self.assertIn('onmouseover=alert(1)', text)
        self.assertIn('<script>window.__xss_rel=1</script>', text)

    def test_global_note_xss_payloads_are_escaped_not_executed(self):
        self.page.wait_for_timeout(50)
        self.assertIsNone(self.page.evaluate('window.__xss_global'))
        self.assertIsNone(self.page.evaluate("document.querySelector('#legend-notes img')"))
        self.assertIsNone(self.page.evaluate("document.querySelector('#legend-notes b')"))
        self.assertTrue(self._no_stray_event_attrs('#legend-notes'))
        text = self.page.inner_text('#legend-notes')
        self.assertIn('<b>global title</b>', text)
        self.assertIn('<img src=x onerror="window.__xss_global=1">', text)

    def test_link_label_attribute_escape_creates_no_extra_attributes(self):
        # The second table-note link's label attempts to break out of the
        # href="..." attribute (' onmouseover=alert(1) foo="'); confirm the
        # <a> itself only has the two attributes noteLinksHtml() sets
        # (href, target — plus rel), nothing injected.
        self.page.click('.er-node[data-name="users"]')
        self.page.wait_for_timeout(50)
        links = self.page.evaluate('''() => {
            return [...document.querySelectorAll('#table-details .note-links a')]
                .map(a => ({ href: a.getAttribute('href'),
                             attrs: a.getAttributeNames(),
                             text: a.textContent }));
        }''')
        self.assertEqual(len(links), 2)
        for link in links:
            self.assertEqual(set(link['attrs']), {'href', 'target', 'rel'})
        self.assertIn('"><b>esc</b>', links[0]['text'])
        self.assertIn('" onmouseover=alert(1) foo="', links[1]['text'])


@unittest.skipUnless(HAVE_PLAYWRIGHT, 'playwright not installed')
class TestClientJS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html_path = _build_html()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f'Chromium not available: {e}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.goto(self.html_path.as_uri())
        # renderDiagram() runs synchronously at the bottom of the inline
        # script, but fitView() is scheduled via requestAnimationFrame.
        self.page.wait_for_function('typeof nodePos.users !== "undefined"')
        self.page.wait_for_timeout(50)
        # the legend is a fixed-position overlay in the top-left of the
        # canvas; depending on layout/fit-zoom a node can end up underneath
        # it, intercepting clicks — collapse it so node clicks are reliable
        self.page.click('#legend-head')

    def tearDown(self):
        self.page.close()

    def _boxes(self):
        return self.page.evaluate('''() => {
            const out = {};
            for (const t of getDisplayTables()) {
                const p = nodePos[t], s = nodeSize[t];
                out[t] = {x0:p.x-s.w/2, y0:p.y-s.h/2, x1:p.x+s.w/2, y1:p.y+s.h/2};
            }
            return out;
        }''')

    def test_grid_layout_no_overlap(self):
        boxes = self._boxes()
        self.assertEqual(set(boxes), {'users', 'posts', 'comments', 'likes', 'audit_logs'})
        names = list(boxes)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = boxes[names[i]], boxes[names[j]]
                separated = (a['x1'] <= b['x0'] or b['x1'] <= a['x0'] or
                             a['y1'] <= b['y0'] or b['y1'] <= a['y0'])
                self.assertTrue(separated,
                    f'{names[i]} and {names[j]} overlap: {a} vs {b}')

    def test_autotidy_off_keeps_positions_on_display_change(self):
        self.assertEqual(self.page.evaluate('autoLayout'), False,
                          'Auto-tidy should default to OFF for this assertion to be meaningful')
        before = self.page.evaluate('({...nodePos})')
        self.page.click('#colmode-group [data-cm="2"]')  # header-only: shrinks every node
        after = self.page.evaluate('({...nodePos})')
        self.assertEqual(before, after,
                          'node positions must be preserved when Auto-tidy is off')
        sizes = self.page.evaluate('({...nodeSize})')
        self.assertLess(sizes['posts']['h'], 90,
                         'header-only mode should have actually shrunk the node '
                         '(otherwise this test is not exercising the resize path)')

    def test_dark_mode_header_is_distinguishable_from_body_and_selection(self):
        # regression: dark mode had no override for .n-hdr at all, so every
        # table's header silently inherited the *light*-mode fill — which
        # happens to be identical to the dark-mode body color, making every
        # header invisible against its own body (reported as "monotonous,
        # hard to tell apart"). Also covers a fix-of-the-fix: the new dark
        # default rule's higher specificity (extra "body" type selector)
        # initially beat the pre-existing .sel/.center rule too, so a
        # selected node's header looked identical to every other one's.
        self.page.click('#btn-dark')
        self.page.wait_for_timeout(50)
        colors = self.page.evaluate('''() => {
            const hdr = document.querySelector('[data-name="posts"] .n-hdr');
            const bg  = document.querySelector('[data-name="posts"] .n-bg');
            return {header: getComputedStyle(hdr).fill, body: getComputedStyle(bg).fill};
        }''')
        self.assertNotEqual(colors['header'], colors['body'],
                            'the header must be visually distinct from the node body in dark mode')

        self.page.click('[data-name="posts"]')
        selected_fill = self.page.evaluate(
            '''getComputedStyle(document.querySelector('[data-name="posts"] .n-hdr')).fill''')
        self.assertNotEqual(selected_fill, colors['header'],
                            'a selected node must still look different from an unselected one in dark mode')

    def test_display_change_does_not_reset_viewport_when_content_still_fits(self):
        # regression: refreshView() used to unconditionally re-fit the
        # viewport on every display-set change, so checking one more table
        # in the list snapped away any pan/zoom the user had set up —
        # jarring on its own, and specifically disruptive right after a
        # deliberate zoom-in to inspect a cluster
        self.assertEqual(self.page.evaluate('autoLayout'), False)
        # the initial page load already fit-viewed once — the diagram is in
        # view at this transform, so any change from here proves a refit fired
        before = self.page.evaluate('({vx, vy, vs})')
        # uncheck then recheck 'likes' — a real display-set change that
        # goes through refreshView(), with positions untouched by autoLayout
        self.page.locator('.table-item:has(.tname:text-is("likes")) input[type=checkbox]').uncheck()
        self.page.wait_for_timeout(50)
        self.page.locator('.table-item:has(.tname:text-is("likes")) input[type=checkbox]').check()
        self.page.wait_for_timeout(50)
        after = self.page.evaluate('({vx, vy, vs})')
        self.assertEqual(before, after,
                         'the viewport must not be reset when the display change '
                         "didn't actually move the diagram out of view")

    def test_manual_zoom_survives_a_removal(self):
        # regression: isDisplayInView() checked the *whole* display set's
        # bbox against the viewport — which is false almost by definition
        # once a user has zoomed in past "everything fits" (that's what
        # zooming in means). So any refreshView() call afterward, even one
        # that places nothing new (like unchecking a table), silently
        # zoomed back out to fit-all. The check must be scoped to only
        # what's newly appearing, not the full set.
        self.page.click('#btn-zoom-in')
        self.page.click('#btn-zoom-in')
        self.page.wait_for_timeout(50)
        zoomed = self.page.evaluate('({vx, vy, vs})')
        self.assertFalse(self.page.evaluate('isDisplayInView()'),
            'test setup assumption: zooming in should leave the full set out of view')
        self.page.locator('.table-item:has(.tname:text-is("likes")) input[type=checkbox]').uncheck()
        self.page.wait_for_timeout(100)
        after = self.page.evaluate('({vx, vy, vs})')
        self.assertEqual(zoomed, after,
            'unchecking a table (nothing new appears) must not undo a manual zoom-in')

    def _click_remove_button(self, name):
        box = self.page.evaluate(f'''() => {{
            const g = document.querySelector('.er-node[data-name="{name}"]');
            const btn = [...g.querySelectorAll('text')].find(t => t.textContent.startsWith('⊖'));
            if (!btn) return null;
            const r = btn.getBoundingClientRect();
            return {{x: r.x + r.width/2, y: r.y + r.height/2}};
        }}''')
        if box is None: return False
        self.page.mouse.click(box['x'], box['y'])
        self.page.wait_for_timeout(100)
        return True

    def test_node_remove_button_excludes_the_table(self):
        # the ⊖ button on a node's header is the diagram-side equivalent of
        # unchecking the table's list checkbox — a lighter action than the
        # list's separate 🚫 ban button (excluding is easy to undo and
        # doesn't survive auto-expand pulling the table back in, unlike a
        # full ban, which was judged worse for this one-click canvas action)
        self.assertTrue(self.page.evaluate('getDisplayTables().includes("users")'))
        self.assertTrue(self._click_remove_button('users'))
        self.assertFalse(self.page.evaluate('getDisplayTables().includes("users")'),
            'clicking ⊖ should remove the table from the diagram')
        self.assertTrue(self.page.evaluate('excludedTables.has("users")'),
            '⊖ should exclude (like unchecking), not ban, the table')
        self.assertFalse(self.page.evaluate('hiddenTables.has("users")'),
            '⊖ must not use the heavier ban mechanism')
        self.assertEqual(self.page.evaluate('[...selectedTables]'), [],
            'clicking ⊖ must not also select the node underneath it')

    def test_remove_button_hidden_while_focused(self):
        # excludeTable() has no effect while focused (the focus view
        # ignores excludedTables entirely) — the button must not be shown
        # where clicking it would silently do nothing
        self.page.dblclick('.er-node[data-name="users"] .n-title')
        self.page.wait_for_timeout(100)
        self.assertTrue(self.page.evaluate('!!focusedTable'), 'test setup: should now be focused')
        self.assertFalse(self._click_remove_button('users'),
            'the ⊖ button should not be rendered while focused')

    def test_remove_button_hidden_when_autoexpand_would_restore_it(self):
        # with auto-expand on, excluding a table that's still reachable as
        # another root's neighbor doesn't actually remove it from view —
        # the button must not be offered in that case either
        self.page.locator('#auto-expand').check()
        self.page.wait_for_timeout(100)
        self.assertTrue(self.page.evaluate('getDisplayTables().includes("posts")'))
        self.assertFalse(self.page.evaluate('canExclude("posts")'),
            "test setup: 'posts' should still be reachable from 'users' via auto-expand")
        self.assertFalse(self._click_remove_button('posts'),
            'the ⊖ button should not be rendered when auto-expand would just restore the table')

    def test_drag_snaps_to_neighbor_and_shows_guide(self):
        rect = self.page.evaluate('''() => {
            const r = document.querySelector('svg').getBoundingClientRect();
            return {left:r.left, top:r.top};
        }''')
        anchor = self.page.evaluate('({...nodePos.users})')
        view = self.page.evaluate('({vx, vy, vs})')
        to_client = lambda wx, wy: (
            rect['left'] + view['vx'] + wx * view['vs'],
            rect['top'] + view['vy'] + wy * view['vs'],
        )
        # drag 'posts' until its x-center lands 1 world-px off of users' x-center
        # (well inside the SNAP_PX=6-client-px threshold) so it should snap flush.
        target_world_x = anchor['x'] + 1
        target_world_y = anchor['y'] + 400  # far away vertically, away from other nodes
        start = self.page.evaluate('({...nodePos.posts})')
        sx, sy = to_client(start['x'], start['y'])
        tx, ty = to_client(target_world_x, target_world_y)

        self.page.mouse.move(sx, sy)
        self.page.mouse.down()
        self.page.mouse.move(tx, ty, steps=8)
        self.assertGreater(
            self.page.evaluate("document.querySelectorAll('#guide-layer .snap-guide').length"),
            0, 'expected a snap guide line while dragging near an aligned neighbor')
        self.page.mouse.up()

        final = self.page.evaluate('({...nodePos.posts})')
        self.assertEqual(final['x'], anchor['x'],
                          'dropped node should have snapped exactly onto the neighbor\'s x-center')
        self.assertEqual(
            self.page.evaluate("document.querySelectorAll('#guide-layer .snap-guide').length"),
            0, 'guides must clear on mouseup')

    def test_shift_click_multiselect_and_align_left(self):
        self.page.click('[data-name="posts"]')
        self.page.click('[data-name="comments"]', modifiers=['Shift'])
        self.assertEqual(sorted(self.page.evaluate('[...selectedTables]')),
                          ['comments', 'posts'])
        align_btn = self.page.locator('[data-align="left"]')
        self.assertTrue(align_btn.is_enabled(), 'align buttons should be enabled at 2+ selected')
        align_btn.click()

        boxes = self.page.evaluate('''() => {
            const box = n => ({left: nodePos[n].x - nodeSize[n].w/2});
            return {posts: box('posts'), comments: box('comments')};
        }''')
        self.assertAlmostEqual(boxes['posts']['left'], boxes['comments']['left'], places=6,
                                msg='align-left should give both nodes the same left edge')

    def test_shift_drag_on_empty_canvas_marquee_selects(self):
        # regression: the marquee's own mouseup is immediately followed by a
        # native `click` event, which svg's click handler used to interpret
        # as "clicked empty canvas" and wipe the selection it had just set
        rect = self.page.evaluate('''() => {
            const r = document.querySelector('svg').getBoundingClientRect();
            return {left:r.left, top:r.top};
        }''')
        view = self.page.evaluate('({vx, vy, vs})')
        to_client = lambda wx, wy: (rect['left'] + view['vx'] + wx * view['vs'],
                                     rect['top'] + view['vy'] + wy * view['vs'])
        boxes = self.page.evaluate('''() => {
            const box = n => ({...nodePos[n], ...nodeSize[n]});
            return {posts: box('posts'), comments: box('comments'), users: box('users')};
        }''')
        # a rectangle around posts+comments only (not users)
        x0 = min(boxes['posts']['x']-boxes['posts']['w']/2, boxes['comments']['x']-boxes['comments']['w']/2) - 20
        y0 = min(boxes['posts']['y']-boxes['posts']['h']/2, boxes['comments']['y']-boxes['comments']['h']/2) - 20
        x1 = max(boxes['posts']['x']+boxes['posts']['w']/2, boxes['comments']['x']+boxes['comments']['w']/2) + 20
        y1 = max(boxes['posts']['y']+boxes['posts']['h']/2, boxes['comments']['y']+boxes['comments']['h']/2) + 20
        self.assertFalse(boxes['users']['x']-boxes['users']['w']/2 >= x0 and
                         boxes['users']['x']+boxes['users']['w']/2 <= x1 and
                         boxes['users']['y']-boxes['users']['h']/2 >= y0 and
                         boxes['users']['y']+boxes['users']['h']/2 <= y1,
                         'test setup: users must NOT be inside the marquee box')
        sx, sy = to_client(x0, y0)
        tx, ty = to_client(x1, y1)
        self.page.keyboard.down('Shift')
        self.page.mouse.move(sx, sy)
        self.page.mouse.down()
        self.page.mouse.move(tx, ty, steps=8)
        self.page.mouse.up()
        self.page.keyboard.up('Shift')
        self.page.wait_for_timeout(50)
        self.assertEqual(sorted(self.page.evaluate('[...selectedTables]')), ['comments', 'posts'])
        # must survive the trailing click, not get wiped by it
        self.page.wait_for_timeout(100)
        self.assertEqual(sorted(self.page.evaluate('[...selectedTables]')), ['comments', 'posts'])

    def test_stray_shift_click_on_empty_canvas_does_not_clear_selection(self):
        # shift is "additive" everywhere else (shift-click a node adds it);
        # a shift-click on empty canvas with no real drag — under the
        # marquee's 3px threshold — must be a no-op too, not a destructive
        # clear of whatever was already selected
        rect = self.page.evaluate('''() => {
            const r = document.querySelector('svg').getBoundingClientRect();
            return {left:r.left, top:r.top, right:r.right, bottom:r.bottom};
        }''')
        # right-middle of the SVG's own visible viewport: clear of the
        # (fit-viewed, so left/center-weighted) fixture's nodes, the legend
        # overlay (top-left), and the toolbar (bottom edge) — verified by
        # the plain-click sanity check right below, not just assumed
        cx, cy = rect['right'] - 100, (rect['top'] + rect['bottom']) / 2

        # sanity check: this point must be real empty canvas — a *plain*
        # click there should clear the selection, or this test would pass
        # vacuously (asserting a no-op happened at a point that never did
        # anything to begin with)
        self.page.click('[data-name="posts"]')
        self.assertEqual(self.page.evaluate('[...selectedTables]'), ['posts'])
        self.page.mouse.click(cx, cy)
        self.assertEqual(self.page.evaluate('[...selectedTables]'), [],
                         'test setup: a plain click at this point must clear the selection')

        self.page.click('[data-name="posts"]')
        self.assertEqual(self.page.evaluate('[...selectedTables]'), ['posts'])
        self.page.keyboard.down('Shift')
        self.page.mouse.move(cx, cy)
        self.page.mouse.down()
        self.page.mouse.up()  # no movement at all -> well under the 3px threshold
        self.page.keyboard.up('Shift')
        self.page.wait_for_timeout(50)
        self.assertEqual(self.page.evaluate('[...selectedTables]'), ['posts'])

    def test_group_drag_moves_whole_selection_together(self):
        self.page.click('[data-name="posts"]')
        self.page.click('[data-name="comments"]', modifiers=['Shift'])
        before = self.page.evaluate('({posts:{...nodePos.posts}, comments:{...nodePos.comments}})')

        rect = self.page.evaluate('''() => {
            const r = document.querySelector('svg').getBoundingClientRect();
            return {left:r.left, top:r.top};
        }''')
        view = self.page.evaluate('({vx, vy, vs})')
        to_client = lambda wx, wy: (rect['left'] + view['vx'] + wx * view['vs'],
                                     rect['top'] + view['vy'] + wy * view['vs'])
        sx, sy = to_client(before['posts']['x'], before['posts']['y'])
        # move somewhere with no other node nearby so snapping doesn't distort the delta
        tx, ty = to_client(before['posts']['x'] + 500, before['posts']['y'] + 500)

        self.page.mouse.move(sx, sy)
        self.page.mouse.down()
        self.page.keyboard.down('Alt')  # disable snap so the delta is exact
        self.page.mouse.move(tx, ty, steps=8)
        self.page.keyboard.up('Alt')
        self.page.mouse.up()

        after = self.page.evaluate('({posts:{...nodePos.posts}, comments:{...nodePos.comments}})')
        dx_posts = after['posts']['x'] - before['posts']['x']
        dy_posts = after['posts']['y'] - before['posts']['y']
        dx_comments = after['comments']['x'] - before['comments']['x']
        dy_comments = after['comments']['y'] - before['comments']['y']
        self.assertGreater(abs(dx_posts) + abs(dy_posts), 50, 'the dragged node should have moved')
        self.assertAlmostEqual(dx_posts, dx_comments, places=3,
                                msg='the rest of the selection should move by the same delta')
        self.assertAlmostEqual(dy_posts, dy_comments, places=3)

    def test_drag_updates_only_edges_touching_the_dragged_node(self):
        # regression for a perf fix: dragging used to recompute and redraw
        # every edge on every mousemove. Now only edges touching the dragged
        # node are re-routed; the rest of the diagram's edges must be left
        # completely alone (same DOM element, same path) and no edges must
        # go missing or get duplicated.
        edge_count_before = self.page.evaluate('document.querySelectorAll(".er-edge").length')
        untouched_path_before = self.page.evaluate(
            '''document.querySelector('.er-edge[data-source="comments"][data-target="users"] path,'
            + '.er-edge[data-source="users"][data-target="comments"] path').getAttribute('d')''')
        moved_path_before = self.page.evaluate(
            '''document.querySelector('.er-edge[data-source="posts"][data-target="users"] path,'
            + '.er-edge[data-source="users"][data-target="posts"] path').getAttribute('d')''')

        before = self.page.evaluate('({...nodePos.posts})')
        rect = self.page.evaluate('''() => {
            const r = document.querySelector('svg').getBoundingClientRect();
            return {left:r.left, top:r.top};
        }''')
        view = self.page.evaluate('({vx, vy, vs})')
        to_client = lambda wx, wy: (rect['left'] + view['vx'] + wx * view['vs'],
                                     rect['top'] + view['vy'] + wy * view['vs'])
        sx, sy = to_client(before['x'], before['y'])
        tx, ty = to_client(before['x'] + 400, before['y'] + 400)
        self.page.mouse.move(sx, sy)
        self.page.mouse.down()
        self.page.keyboard.down('Alt')
        self.page.mouse.move(tx, ty, steps=8)
        self.page.keyboard.up('Alt')
        self.page.mouse.up()

        edge_count_after = self.page.evaluate('document.querySelectorAll(".er-edge").length')
        untouched_path_after = self.page.evaluate(
            '''document.querySelector('.er-edge[data-source="comments"][data-target="users"] path,'
            + '.er-edge[data-source="users"][data-target="comments"] path').getAttribute('d')''')
        moved_path_after = self.page.evaluate(
            '''document.querySelector('.er-edge[data-source="posts"][data-target="users"] path,'
            + '.er-edge[data-source="users"][data-target="posts"] path').getAttribute('d')''')

        self.assertEqual(edge_count_before, edge_count_after, 'no edge should go missing or duplicate')
        self.assertEqual(untouched_path_before, untouched_path_after,
                         "an edge not touching the dragged node shouldn't be redrawn at all")
        self.assertNotEqual(moved_path_before, moved_path_after,
                            "the dragged node's own edge must still track its new position")

    def test_distribute_horizontal_equalizes_gaps(self):
        for name in ('posts', 'comments', 'likes'):
            self.page.click(f'[data-name="{name}"]', modifiers=['Shift'])
        self.assertEqual(self.page.evaluate('selectedTables.size'), 3)
        dist_btn = self.page.locator('[data-dist="h"]')
        self.assertTrue(dist_btn.is_enabled(), 'distribute should be enabled at 3+ selected')
        dist_btn.click()

        gaps = self.page.evaluate('''() => {
            const items = ['posts','comments','likes'].map(t => {
                const p = nodePos[t], s = nodeSize[t];
                return {x0: p.x - s.w/2, x1: p.x + s.w/2};
            }).sort((a,b) => a.x0 - b.x0);
            return [items[1].x0 - items[0].x1, items[2].x0 - items[1].x1];
        }''')
        self.assertAlmostEqual(gaps[0], gaps[1], places=3,
                                msg='distribute should equalize the edge-to-edge gaps')

    def test_undo_redo_drag(self):
        self.assertTrue(self.page.evaluate("document.getElementById('btn-undo').disabled"),
                         'undo should start disabled — nothing to undo yet')
        before = self.page.evaluate('({...nodePos.users})')

        rect = self.page.evaluate('''() => {
            const r = document.querySelector('svg').getBoundingClientRect();
            return {left:r.left, top:r.top};
        }''')
        view = self.page.evaluate('({vx, vy, vs})')
        to_client = lambda wx, wy: (rect['left'] + view['vx'] + wx * view['vs'],
                                     rect['top'] + view['vy'] + wy * view['vs'])
        sx, sy = to_client(before['x'], before['y'])
        tx, ty = to_client(before['x'] + 300, before['y'] + 300)
        self.page.keyboard.down('Alt')  # disable snap so the delta is exact
        self.page.mouse.move(sx, sy)
        self.page.mouse.down()
        self.page.mouse.move(tx, ty, steps=8)
        self.page.mouse.up()
        self.page.keyboard.up('Alt')

        after_drag = self.page.evaluate('({...nodePos.users})')
        self.assertNotEqual(before, after_drag, 'the drag should have moved the node')
        self.assertFalse(self.page.evaluate("document.getElementById('btn-undo').disabled"),
                          'undo should be enabled after a real drag')

        self.page.click('#btn-undo')
        self.assertEqual(self.page.evaluate('({...nodePos.users})'), before,
                          'undo should restore the pre-drag position exactly')

        self.page.keyboard.press('Control+Shift+Z')
        self.assertEqual(self.page.evaluate('({...nodePos.users})'), after_drag,
                          'Ctrl+Shift+Z should redo back to the post-drag position')

    def test_undo_does_not_fire_on_a_plain_click(self):
        # a click with no movement must not push a spurious undo entry
        self.page.click('[data-name="users"]')
        self.assertTrue(self.page.evaluate("document.getElementById('btn-undo').disabled"),
                         'a plain click (no drag) should not create an undo entry')

    def test_undo_history_is_cleared_when_entering_focus_mode(self):
        # regression: a drag's undo snapshot is a full overview nodePos.
        # Entering focus mode wholesale-replaces nodePos with just the
        # focus ring's positions — undoing there used to restore the stale
        # overview snapshot into the wrong coordinate space/table set,
        # scrambling the layout. The stack must be cleared on the
        # transition instead, disabling undo.
        box = self.page.locator('[data-name="posts"]').bounding_box()
        self.page.mouse.move(box['x']+box['width']/2, box['y']+15)
        self.page.mouse.down()
        self.page.mouse.move(box['x']+box['width']/2+80, box['y']+15+80, steps=5)
        self.page.mouse.up()
        self.assertFalse(self.page.evaluate("document.getElementById('btn-undo').disabled"),
                         'a real drag should have created an undo entry')

        self.page.dblclick('[data-name="users"]')  # double-click = enter focus mode
        self.page.wait_for_timeout(50)
        self.assertTrue(self.page.evaluate('!!focusedTable'), 'should now be in focus mode')
        self.assertTrue(self.page.evaluate("document.getElementById('btn-undo').disabled"),
                         'the pre-focus undo history must not carry into focus mode')

        self.page.dblclick('[data-name="users"]')  # exit focus mode
        self.page.wait_for_timeout(50)
        self.assertFalse(self.page.evaluate('!!focusedTable'))
        self.assertTrue(self.page.evaluate("document.getElementById('btn-undo').disabled"),
                         'exiting focus mode must not resurrect the pre-focus undo history either')

    def test_entering_focus_always_fits_the_viewport(self):
        # regression: refreshView()'s "skip fitView if the content is
        # already in view" optimization (added for a different fix) could
        # accidentally also apply when entering/switching focus — if the
        # old (overview) viewport happened to already contain the new,
        # smaller focused layout's bounding box, the zoom never actually
        # moved in, defeating the entire point of "focusing"
        self.page.evaluate('vx=-99999; vy=-99999; vs=3; setTransform();')
        before = self.page.evaluate('({vx, vy, vs})')
        # focusTable() directly — the node itself is off-screen at this
        # transform (that's the point), so a real double-click can't hit it
        self.page.evaluate("focusTable('posts')")
        self.page.wait_for_timeout(100)
        after = self.page.evaluate('({vx, vy, vs})')
        self.assertNotEqual(before, after,
                            'entering focus must always re-fit the viewport, not leave a '
                            'clearly-unrelated prior transform in place')
        # and the focused table must actually be on screen afterward
        pos = self.page.evaluate('({...nodePos.posts})')
        view = self.page.evaluate('({vx, vy, vs})')
        rect = self.page.evaluate('''() => {
            const r = document.querySelector('svg').getBoundingClientRect();
            return {width:r.width, height:r.height};
        }''')
        sx = view['vx'] + pos['x'] * view['vs']
        sy = view['vy'] + pos['y'] * view['vs']
        self.assertTrue(0 <= sx <= rect['width'] and 0 <= sy <= rect['height'],
                        f'focused table should be visible on screen, got screen pos ({sx},{sy}) '
                        f'in a {rect["width"]}x{rect["height"]} viewport')


# Regression fixture for the "same-row skip" family of layout bugs: 'hub' is
# the highest-degree table (becomes the row-0 hub), its three direct children
# land in row 1, and 'center' is *also* directly connected to both of its
# row-1 siblings — a same-row "star" that a naive left-to-right ordering
# can't satisfy on both sides at once (discovery order puts the star's center
# at one end of the row, not between its two neighbors), forcing the edge
# router into a long detour arc around whichever sibling ends up in between.
CLIQUE_TABLE_ROWS = [('hub', ''), ('center', ''), ('left_leaf', ''), ('right_leaf', '')]
CLIQUE_COL_ROWS = [
    _col('hub', 'id', key='PRI'),
    _col('center', 'id', key='PRI'),
    _col('center', 'hub_id', key='MUL'),
    _col('left_leaf', 'id', key='PRI'),
    _col('left_leaf', 'hub_id', key='MUL'),
    _col('left_leaf', 'center_id', key='MUL'),
    _col('right_leaf', 'id', key='PRI'),
    _col('right_leaf', 'hub_id', key='MUL'),
    _col('right_leaf', 'center_id', key='MUL'),
]
CLIQUE_FK_ROWS = [
    ('center', 'hub_id', 'hub'),
    ('left_leaf', 'hub_id', 'hub'),
    ('left_leaf', 'center_id', 'center'),
    ('right_leaf', 'hub_id', 'hub'),
    ('right_leaf', 'center_id', 'center'),
]
CLIQUE_INDEX_ROWS = [('hub', 'PRIMARY', 0, 1, 'id')]


def _build_clique_html():
    tables = erd.mysql_ir(CLIQUE_TABLE_ROWS, CLIQUE_COL_ROWS, CLIQUE_FK_ROWS, CLIQUE_INDEX_ROWS)
    args = SimpleNamespace(output='', models=None, excel=None, max_rows=15,
                            only=None, exclude=None, infer_fk=False)
    tmp = tempfile.mkdtemp()
    out = Path(tmp) / 'clique.html'
    args.output = str(out)
    erd._finish(tables, args, 'e2e_clique_fixture')
    return out


@unittest.skipUnless(HAVE_PLAYWRIGHT, 'playwright not installed')
class TestSameRowSkipRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html_path = _build_clique_html()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f'Chromium not available: {e}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_star_center_edges_stay_short(self):
        page = self.browser.new_page()
        try:
            page.goto(self.html_path.as_uri())
            page.wait_for_function('typeof nodePos.hub !== "undefined"')
            page.wait_for_timeout(50)
            bends = page.evaluate(r'''() => {
                const out = {};
                document.querySelectorAll('.er-edge').forEach(g => {
                    const src = g.getAttribute('data-source'), tgt = g.getAttribute('data-target');
                    if (!((src==='center'&&tgt==='left_leaf')||(src==='left_leaf'&&tgt==='center')
                       || (src==='center'&&tgt==='right_leaf')||(src==='right_leaf'&&tgt==='center'))) return;
                    const path = g.querySelector('path');
                    const bb = path.getBBox();
                    out[src+'-'+tgt] = Math.max(bb.width, bb.height);
                });
                return out;
            }''')
            self.assertEqual(len(bends), 2, f'expected both center-leaf edges, got {bends}')
            for pair, size in bends.items():
                self.assertLess(size, 100,
                    f'{pair} edge bbox is {size}px — the row-1 "star" center '
                    f'ended up not adjacent to a row-1 sibling it connects to, '
                    f'forcing a long detour arc')
        finally:
            page.close()


@unittest.skipUnless(HAVE_PLAYWRIGHT, 'playwright not installed')
class TestIncrementalAdditionPlacement(unittest.TestCase):
    """Regression for the incremental-additions cascade: adding tables one
    checkbox at a time (each click is its own 1-table layout pass, all
    anchored at the same hub) used to stack every new table straight below
    the previous one — after a few rounds the diagram was a 1-node-wide
    vertical snake full of long detour edges. The placer must scan sideways
    within a row band before dropping to the next one."""

    @classmethod
    def setUpClass(cls):
        cls.html_path = _build_html()  # users hub + 4 spokes fixture
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f'Chromium not available: {e}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    CHECKBOX = '.table-item:has(.tname:text-is("{0}")) input[type=checkbox]'

    def test_sequential_checkbox_adds_fill_rows_not_one_column(self):
        page = self.browser.new_page()
        try:
            page.goto(self.html_path.as_uri())
            page.wait_for_function('typeof nodePos.users !== "undefined"')
            page.wait_for_timeout(50)
            # isolate state from other tests sharing this browser context,
            # then start from a fresh layout that contains only the hub
            page.evaluate('localStorage.clear()')
            page.reload()
            page.wait_for_function('typeof nodePos.users !== "undefined"')
            page.click('#btn-none')
            page.click(self.CHECKBOX.format('users'))
            page.reload()  # persisted state -> fresh gridLayout of just 'users'
            page.wait_for_function('typeof nodePos.users !== "undefined"')
            # four separate incremental passes, all anchored at the hub
            for name in ('posts', 'comments', 'likes', 'audit_logs'):
                page.click(self.CHECKBOX.format(name))
            boxes = page.evaluate('''() => {
                const out = {};
                for (const t of getDisplayTables()) {
                    const p = nodePos[t], s = nodeSize[t];
                    out[t] = {x0:p.x-s.w/2, y0:p.y-s.h/2, x1:p.x+s.w/2, y1:p.y+s.h/2,
                              x:p.x, y:p.y};
                }
                return out;
            }''')
            self.assertEqual(len(boxes), 5)
            names = list(boxes)
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    a, b = boxes[names[i]], boxes[names[j]]
                    separated = (a['x1'] <= b['x0'] or b['x1'] <= a['x0'] or
                                 a['y1'] <= b['y0'] or b['y1'] <= a['y0'])
                    self.assertTrue(separated,
                        f'{names[i]} and {names[j]} overlap: {a} vs {b}')
            # at least two of the hub's children must share a row band —
            # the old placer put every addition on its own row
            ys = sorted(boxes[n]['y'] for n in ('posts', 'comments', 'likes', 'audit_logs'))
            same_band = any(abs(ys[i+1] - ys[i]) < 40 for i in range(len(ys)-1))
            self.assertTrue(same_band,
                f'every incremental addition landed on its own row (ys={ys}) — '
                f'the 1-wide vertical cascade is back')
            # and the overall diagram must not be a vertical snake
            x0 = min(b['x0'] for b in boxes.values()); x1 = max(b['x1'] for b in boxes.values())
            y0 = min(b['y0'] for b in boxes.values()); y1 = max(b['y1'] for b in boxes.values())
            self.assertLess((y1-y0) / (x1-x0), 3.0,
                f'diagram bbox {x1-x0:.0f}x{y1-y0:.0f} is a vertical snake')
        finally:
            page.close()


@unittest.skipUnless(HAVE_PLAYWRIGHT, 'playwright not installed')
class TestIsolatedTablePlacement(unittest.TestCase):
    """A table with no FK relation to anything currently displayed used to
    be appended as a new row below the whole diagram — every unrelated
    table checked in made the diagram taller, compounding the layout's
    already-strong tendency to grow vertically via BFS-depth rows. It
    should stack in a column along the right edge instead."""
    @classmethod
    def setUpClass(cls):
        cls.html_path = _build_html_with_isolated_table()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f'Chromium not available: {e}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    CHECKBOX = '.table-item:has(.tname:text-is("{0}")) input[type=checkbox]'

    def _boxes(self, page):
        return page.evaluate('''() => {
            const out = {};
            for (const t of getDisplayTables()) {
                const p = nodePos[t], s = nodeSize[t];
                out[t] = {x0:p.x-s.w/2, y0:p.y-s.h/2, x1:p.x+s.w/2, y1:p.y+s.h/2};
            }
            return out;
        }''')

    def test_full_layout_puts_isolated_table_to_the_right_not_below(self):
        page = self.browser.new_page()
        try:
            page.goto(self.html_path.as_uri())
            page.wait_for_function('typeof nodePos.users !== "undefined"')
            page.wait_for_timeout(50)
            boxes = self._boxes(page)
            connected_x1 = max(boxes[t]['x1'] for t in boxes if t != 'settings')
            connected_y0 = min(boxes[t]['y0'] for t in boxes if t != 'settings')
            connected_y1 = max(boxes[t]['y1'] for t in boxes if t != 'settings')
            s = boxes['settings']
            self.assertGreaterEqual(s['x0'], connected_x1,
                'isolated table should sit to the right of the connected component, not overlap/below it')
            # top-anchored: its top edge should be near the connected group's
            # top, not appended past its bottom
            self.assertLess(s['y0'], connected_y1,
                'isolated table should start near the top of the diagram, not below everything')
        finally:
            page.close()

    def test_incremental_add_of_isolated_table_goes_right_not_below(self):
        page = self.browser.new_page()
        try:
            page.goto(self.html_path.as_uri())
            page.wait_for_function('typeof nodePos.users !== "undefined"')
            page.wait_for_timeout(50)
            page.evaluate('localStorage.clear()')
            page.reload()
            page.wait_for_function('typeof nodePos.users !== "undefined"')
            page.click('#btn-none')
            for name in ('users', 'posts', 'comments', 'likes', 'audit_logs'):
                page.click(self.CHECKBOX.format(name))
            page.reload()  # persisted state -> fresh gridLayout of the connected group
            page.wait_for_function('typeof nodePos.users !== "undefined"')
            before = self._boxes(page)
            bx1 = max(b['x1'] for b in before.values())
            by0 = min(b['y0'] for b in before.values())
            by1 = max(b['y1'] for b in before.values())
            page.click(self.CHECKBOX.format('settings'))  # incremental add, no shared neighbor
            after = self._boxes(page)
            s = after['settings']
            self.assertGreaterEqual(s['x0'], bx1,
                'incrementally-added isolated table should land to the right of the existing diagram')
            self.assertLess(s['y0'], by1,
                'incrementally-added isolated table should not be appended below everything')
        finally:
            page.close()


@unittest.skipUnless(HAVE_PLAYWRIGHT, 'playwright not installed')
class TestRepeatedIsolatedAdditionsShareOneColumn(unittest.TestCase):
    """Regression: the first fix for isolated-table placement (right-side
    column instead of rows below) recomputed the column's x from the whole
    diagram's right edge on every single addition — so once the first
    isolated table joined "the whole diagram", the second one's anchor
    included it and landed even further right, and so on. Each isolated
    table must reuse the *same* x as any already-placed isolated ones and
    just continue the column downward. Exercises both the checkbox path
    and the search+Enter-to-locate path, since both funnel through the
    same incremental layoutAll()."""
    @classmethod
    def setUpClass(cls):
        cls.html_path = _build_html_with_multiple_isolated_tables()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f'Chromium not available: {e}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    NAMES = ['settings_a', 'settings_b', 'settings_c', 'settings_d']
    CHECKBOX = '.table-item:has(.tname:text-is("{0}")) input[type=checkbox]'

    def _setup_base(self, page):
        page.goto(self.html_path.as_uri())
        page.wait_for_function('typeof nodePos.users !== "undefined"')
        page.wait_for_timeout(50)
        page.evaluate('localStorage.clear()')
        page.reload()
        page.wait_for_function('typeof nodePos.users !== "undefined"')
        page.click('#btn-none')
        for name in ('users', 'posts', 'comments'):
            page.click(self.CHECKBOX.format(name))
        page.reload()  # persisted state -> fresh gridLayout of the connected group
        page.wait_for_function('typeof nodePos.users !== "undefined"')

    def _xs(self, page):
        names_js = '[' + ','.join(f'"{n}"' for n in self.NAMES) + ']'
        return page.evaluate(f'''() => {names_js}.map(t => nodePos[t].x)''')

    def test_four_separate_checkbox_additions_form_one_column(self):
        page = self.browser.new_page()
        try:
            self._setup_base(page)
            for name in self.NAMES:
                page.click(self.CHECKBOX.format(name))  # each its own incremental pass
                page.wait_for_timeout(30)
            xs = self._xs(page)
            self.assertEqual(len(set(xs)), 1,
                f'isolated tables added one at a time should share one x, got {xs}')
        finally:
            page.close()

    def test_search_and_enter_additions_form_one_column(self):
        page = self.browser.new_page()
        try:
            self._setup_base(page)
            for name in self.NAMES:
                page.fill('#search', name)
                page.press('#search', 'Enter')
                page.wait_for_timeout(30)
            xs = self._xs(page)
            self.assertEqual(len(set(xs)), 1,
                f'isolated tables located via search+Enter should share one x, got {xs}')
        finally:
            page.close()


# Regression fixture for search case-sensitivity: a CamelCase table name (as
# Prisma models commonly are without an @@map override) that a lowercase
# search query must still be able to find.
SEARCH_TABLE_ROWS = [('CamelCaseWidget', ''), ('plain_table', '')]
SEARCH_COL_ROWS = [
    _col('CamelCaseWidget', 'id', key='PRI'),
    _col('plain_table', 'id', key='PRI'),
]


def _build_search_html():
    tables = erd.mysql_ir(SEARCH_TABLE_ROWS, SEARCH_COL_ROWS, [], [])
    args = SimpleNamespace(output='', models=None, excel=None, max_rows=15,
                            only=None, exclude=None, infer_fk=False)
    tmp = tempfile.mkdtemp()
    out = Path(tmp) / 'search.html'
    args.output = str(out)
    erd._finish(tables, args, 'e2e_search_fixture')
    return out


@unittest.skipUnless(HAVE_PLAYWRIGHT, 'playwright not installed')
class TestSearchCaseInsensitivity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html_path = _build_search_html()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f'Chromium not available: {e}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_lowercase_query_finds_camelcase_table_in_list(self):
        page = self.browser.new_page()
        try:
            page.goto(self.html_path.as_uri())
            page.wait_for_function('typeof nodePos.CamelCaseWidget !== "undefined"')
            page.fill('#search', 'camelcase')
            visible = page.evaluate('''() =>
                [...document.querySelectorAll('.table-item .tname')].map(e => e.textContent)''')
            self.assertEqual(visible, ['CamelCaseWidget'])
        finally:
            page.close()

    def test_lowercase_query_and_enter_locates_camelcase_table(self):
        # locateTable() (only reached when the Enter handler's match logic
        # actually resolves the query to a table) selects it — a stronger
        # signal than just "still displayed", since both fixture tables are
        # displayed by default regardless of whether the match worked
        page = self.browser.new_page()
        try:
            page.goto(self.html_path.as_uri())
            page.wait_for_function('typeof nodePos.CamelCaseWidget !== "undefined"')
            self.assertEqual(page.evaluate('selectedTables.size'), 0)
            page.click('#search')
            page.keyboard.type('camelcasewidget')
            page.keyboard.press('Enter')
            page.wait_for_timeout(100)
            self.assertEqual(page.evaluate('[...selectedTables]'), ['CamelCaseWidget'])
        finally:
            page.close()


@unittest.skipUnless(HAVE_PLAYWRIGHT, 'playwright not installed')
class TestLocalStorageWriteFailureIsNonFatal(unittest.TestCase):
    """localStorage.setItem throws in Safari private browsing / over quota.
    Regression: that used to abort whatever handler called saveState()
    partway through, so the state change it was about to reflect in the
    diagram (refreshView/renderTableList, called right after saveState())
    never happened, even though the underlying data (excludedTables) had
    already been mutated — a confusing half-applied UI state."""
    @classmethod
    def setUpClass(cls):
        cls.html_path = _build_html()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f'Chromium not available: {e}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_unchecking_a_table_still_updates_the_diagram_when_storage_throws(self):
        page = self.browser.new_page()
        try:
            page.add_init_script(
                'localStorage.setItem = () => { throw new DOMException("quota exceeded"); };')
            page.goto(self.html_path.as_uri())
            page.wait_for_function('typeof nodePos.users !== "undefined"')
            page.wait_for_timeout(50)
            self.assertIn('posts', page.evaluate('getDisplayTables()'))
            page.locator('.table-item:has(.tname:text-is("posts")) input[type=checkbox]').uncheck()
            page.wait_for_timeout(50)
            self.assertNotIn('posts', page.evaluate('getDisplayTables()'),
                             'unchecking must still remove the table from the diagram '
                             'even though persisting that choice to localStorage failed')
        finally:
            page.close()


@unittest.skipUnless(HAVE_PLAYWRIGHT, 'playwright not installed')
class TestPngExportOversizedCanvas(unittest.TestCase):
    """A canvas exceeding the browser's dimension/area limit makes
    toBlob() yield null with no exception of its own — that used to reach
    a bare `URL.createObjectURL(pngBlob)` / `new ClipboardItem(...)` with a
    null blob and throw, leaving exportToPNG's promise unresolved forever
    (no toast, no error, just a permanently "stuck" export)."""
    @classmethod
    def setUpClass(cls):
        cls.html_path = _build_html()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f'Chromium not available: {e}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def test_null_blob_shows_a_toast_instead_of_hanging(self):
        page = self.browser.new_page()
        try:
            page.add_init_script(
                'HTMLCanvasElement.prototype.toBlob = function(cb) { cb(null); };')
            page.goto(self.html_path.as_uri())
            page.wait_for_function('typeof nodePos.users !== "undefined"')
            page.evaluate('exportToPNG()')
            page.wait_for_timeout(200)
            self.assertTrue(page.evaluate("document.getElementById('toast').classList.contains('show')"),
                            'a toast should appear instead of the export silently hanging')
            self.assertIn('too large', page.evaluate("document.getElementById('toast').textContent"))
        finally:
            page.close()

    def test_download_button_downloads_even_when_clipboard_write_succeeds(self):
        # regression: PNG used to be one button that always tried the
        # clipboard first and only fell back to a download if clipboard
        # write failed — on a browser that supports clipboard images
        # (most do), there was no way to explicitly get a *file* instead.
        # "PNG — download file" must always download, regardless of
        # clipboard support/success, and "PNG — copy to clipboard" must
        # still prefer the clipboard when it's available.
        page = self.browser.new_page()
        try:
            page.add_init_script('''
                navigator.clipboard.write = () => Promise.resolve();
            ''')
            page.goto(self.html_path.as_uri())
            page.wait_for_function('typeof nodePos.users !== "undefined"')
            with page.expect_download() as dl_info:
                page.evaluate('downloadPNGFile()')
            self.assertEqual(dl_info.value.suggested_filename, 'erd.png')
            # and the clipboard-targeted action, in the same clipboard-capable
            # environment, must NOT also trigger a download
            downloaded = False
            def on_download(_): nonlocal downloaded; downloaded = True
            page.on('download', on_download)
            page.evaluate('exportToPNG()')
            page.wait_for_timeout(300)
            self.assertFalse(downloaded,
                'exportToPNG() should use the clipboard, not fall back to a download, when clipboard.write succeeds')
        finally:
            page.close()


@unittest.skipUnless(HAVE_PLAYWRIGHT, 'playwright not installed')
class TestExportMenu(unittest.TestCase):
    """PNG/SVG/Mermaid used to be three permanent toolbar buttons; they're
    now collapsed behind one 'Export' toggle (part of a toolbar
    decluttering pass) since each is used ~once per session, unlike the
    always-visible zoom/layout controls."""
    @classmethod
    def setUpClass(cls):
        cls.html_path = _build_html()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f'Chromium not available: {e}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.goto(self.html_path.as_uri())
        self.page.wait_for_function('typeof nodePos.users !== "undefined"')
        self.page.wait_for_timeout(50)

    def tearDown(self):
        self.page.close()

    def _is_open(self):
        return self.page.evaluate("document.getElementById('export-menu').classList.contains('open')")

    def test_toggle_opens_and_a_second_click_closes(self):
        self.assertFalse(self._is_open())
        self.page.click('#btn-export-toggle')
        self.assertTrue(self._is_open())
        self.page.click('#btn-export-toggle')
        self.assertFalse(self._is_open())

    def test_outside_click_closes_the_menu(self):
        self.page.click('#btn-export-toggle')
        self.assertTrue(self._is_open())
        self.page.click('#er-svg', position={'x': 10, 'y': 10})
        self.assertFalse(self._is_open())

    def test_escape_closes_the_menu_before_anything_else(self):
        self.page.click('#btn-export-toggle')
        self.assertTrue(self._is_open())
        self.page.keyboard.press('Escape')
        self.assertFalse(self._is_open())

    def test_clicking_an_export_option_closes_the_menu(self):
        self.page.click('#btn-export-toggle')
        self.page.click('#btn-export-svg')  # triggers a download; doesn't need to complete for this check
        self.page.wait_for_timeout(50)
        self.assertFalse(self._is_open())


@unittest.skipUnless(HAVE_PLAYWRIGHT, 'playwright not installed')
class TestExportOptionsAndPlantUML(unittest.TestCase):
    """The export menu's 'Image options' checkboxes (join-table labels,
    ✓ root badges) are independent of the live view's own Labels toggle —
    and the new PlantUML export, following exportToMermaid's shape."""
    @classmethod
    def setUpClass(cls):
        cls.html_path = _build_html()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f'Chromium not available: {e}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.goto(self.html_path.as_uri())
        self.page.wait_for_function('typeof nodePos.users !== "undefined"')
        self.page.wait_for_timeout(50)

    def tearDown(self):
        self.page.close()

    def test_defaults_labels_on_roots_off(self):
        self.assertTrue(self.page.evaluate('exportOptLabels'))
        self.assertFalse(self.page.evaluate('exportOptRoots'))
        self.page.click('#btn-export-toggle')
        self.assertTrue(self.page.is_checked('#export-opt-labels'))
        self.assertFalse(self.page.is_checked('#export-opt-roots'))

    def test_checkbox_click_does_not_close_the_menu(self):
        self.page.click('#btn-export-toggle')
        self.page.click('#export-opt-roots')
        self.page.wait_for_timeout(50)
        self.assertTrue(self.page.evaluate(
            "document.getElementById('export-menu').classList.contains('open')"))

    def test_export_options_are_independent_of_the_live_labels_toggle(self):
        # turning the live Labels view off must not turn export labels off,
        # and vice versa — the two toggles used to be the same variable
        self.page.click('#btn-labels')  # live view: labels off
        self.page.wait_for_timeout(50)
        self.assertFalse(self.page.evaluate('showEdgeLabels'))
        self.assertTrue(self.page.evaluate('exportOptLabels'),
            'the export checkbox must keep its own state, unaffected by the live toggle')
        built_has_labels_visible = self.page.evaluate('''() => {
            const built = buildExportSvg();
            const css = built.svg.querySelector('style').textContent;
            return !css.includes('.e-lbg,.e-ltxt{display:none}');
        }''')
        self.assertTrue(built_has_labels_visible,
            'export should still include labels even though the live view has them off')

    def test_export_options_persist_across_reload(self):
        self.page.click('#btn-export-toggle')
        self.page.click('#export-opt-roots')  # -> true
        self.page.click('#export-opt-labels')  # -> false
        self.page.wait_for_timeout(50)
        self.page.reload()
        self.page.wait_for_function('typeof nodePos.users !== "undefined"')
        self.assertTrue(self.page.evaluate('exportOptRoots'))
        self.assertFalse(self.page.evaluate('exportOptLabels'))

    def test_plantuml_export_produces_valid_looking_markup(self):
        self.page.evaluate('''() => {
            window.__clip = null;
            navigator.clipboard.writeText = t => { window.__clip = t; return Promise.resolve(); };
        }''')
        self.page.evaluate('exportToPlantUML()')
        self.page.wait_for_timeout(100)
        text = self.page.evaluate('window.__clip')
        self.assertTrue(text.startswith('@startuml'))
        self.assertTrue(text.rstrip().endswith('@enduml'))
        self.assertIn('entity users {', text)
        self.assertIn('* id : bigint <<PK>>', text)
        self.assertIn('* user_id : bigint <<FK>>', text)
        self.assertIn('||--o{', text)  # users -> posts/comments/likes/audit_logs, all 1:n

    def test_plantuml_uses_logical_name_alias_when_comment_present(self):
        html = _build_html_with_comments()
        page = self.browser.new_page()
        try:
            page.goto(html.as_uri())
            page.wait_for_function('typeof nodePos.users !== "undefined"')
            page.evaluate('''() => {
                window.__clip = null;
                navigator.clipboard.writeText = t => { window.__clip = t; return Promise.resolve(); };
            }''')
            page.evaluate('exportToPlantUML()')
            page.wait_for_timeout(100)
            text = page.evaluate('window.__clip')
            self.assertIn('entity "users（Customer account…）" as users {', text)
            self.assertIn('entity comments {', text)  # no comment -> plain form
        finally:
            page.close()

    def test_plantuml_sanitizes_a_non_word_table_name_and_reuses_it_consistently(self):
        # regression: a table name failing /^\w+$/ (backtick-quoted in
        # real SQL, e.g. a schema-qualified "shared.users") used to alias
        # itself to *itself* — still invalid PlantUML — and relationship
        # lines referenced the raw name, not even the (broken) alias, so
        # they pointed at an entity that was never declared. Also checks
        # that a comment containing a literal " doesn't terminate the
        # quoted display name early.
        table_rows = [('shared.users', 'A "core" table'), ('posts', '')]  # short: stays under the 16-unit truncation cap
        col_rows = [_col('shared.users', 'id', key='PRI'), _col('posts', 'id', key='PRI'),
                    _col('posts', 'user_id', key='MUL')]
        fk_rows = [('posts', 'user_id', 'shared.users')]
        tables = erd.mysql_ir(table_rows, col_rows, fk_rows, [])
        args = SimpleNamespace(output='', models=None, excel=None, max_rows=15,
                                only=None, exclude=None, infer_fk=False)
        tmp = tempfile.mkdtemp()
        out = Path(tmp) / 'out.html'
        args.output = str(out)
        erd._finish(tables, args, 'e2e_fixture')

        page = self.browser.new_page()
        try:
            page.goto(out.as_uri())
            page.wait_for_function('typeof nodePos.posts !== "undefined"')
            page.evaluate('''() => {
                window.__clip = null;
                navigator.clipboard.writeText = t => { window.__clip = t; return Promise.resolve(); };
            }''')
            page.evaluate('exportToPlantUML()')
            page.wait_for_timeout(100)
            text = page.evaluate('window.__clip')
            self.assertIn('entity "shared.users（A \'core\' table）" as shared_users {', text,
                'the alias must be a valid identifier, and " in the comment must not break the quoted string')
            self.assertIn('shared_users ||--o{ posts', text,
                'the relationship line must reference the same sanitized alias declared above')
            self.assertNotIn('shared.users ||--o{', text, 'must not reference the raw, undeclared name')
        finally:
            page.close()

    def test_svg_copy_writes_markup_to_the_clipboard(self):
        # SVG used to be download-only; it now has the same copy/download
        # pair as every other format
        self.page.evaluate('''() => {
            window.__clip = null;
            navigator.clipboard.writeText = t => { window.__clip = t; return Promise.resolve(); };
        }''')
        self.page.evaluate('copySVGToClipboard()')
        self.page.wait_for_timeout(100)
        text = self.page.evaluate('window.__clip')
        self.assertTrue(text.startswith('<svg'))

    def test_svg_download_still_works_alongside_the_new_copy_button(self):
        with self.page.expect_download() as dl:
            self.page.evaluate('exportToSVG()')
        self.assertEqual(dl.value.suggested_filename, 'erd.svg')

    def test_mermaid_download_writes_a_file_without_touching_the_clipboard(self):
        downloaded = []
        self.page.on('download', lambda d: downloaded.append(d))
        with self.page.expect_download() as dl:
            self.page.evaluate('downloadMermaidFile()')
        self.assertEqual(dl.value.suggested_filename, 'erd.mmd')

    def test_plantuml_download_writes_a_file_without_touching_the_clipboard(self):
        with self.page.expect_download() as dl:
            self.page.evaluate('downloadPlantUMLFile()')
        self.assertEqual(dl.value.suggested_filename, 'erd.puml')

    def test_export_menu_has_a_copy_and_download_button_for_every_format(self):
        self.page.click('#btn-export-toggle')
        for fmt_id in ('btn-export', 'btn-export-svg-copy', 'btn-export-mmd', 'btn-export-puml'):
            self.assertEqual(self.page.inner_text(f'#{fmt_id}'), 'Copy', fmt_id)
        for fmt_id in ('btn-export-download', 'btn-export-svg', 'btn-export-mmd-download', 'btn-export-puml-download'):
            self.assertEqual(self.page.inner_text(f'#{fmt_id}'), 'Download', fmt_id)


@unittest.skipUnless(HAVE_PLAYWRIGHT, 'playwright not installed')
class TestWordSearchHighlight(unittest.TestCase):
    """Toolbar 'Highlight' search — separate from the left-pane search box,
    which filters. This one must never hide a row; it only marks matches
    across the diagram, table list, and right pane. Fixture: users.email is
    the only 'email' column anywhere, so it isolates a single-table hit
    (users) with everything else dimmed — a clean case for the dim/hit
    distinction that a broader query like 'user' (which also matches every
    *_id column) wouldn't exercise."""
    @classmethod
    def setUpClass(cls):
        cls.html_path = _build_html()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f'Chromium not available: {e}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.goto(self.html_path.as_uri())
        self.page.wait_for_function('typeof nodePos.users !== "undefined"')
        self.page.wait_for_timeout(50)

    def tearDown(self):
        self.page.close()

    def _highlight(self, query):
        self.page.fill('#word-search', query)
        self.page.wait_for_timeout(250)  # clears the 150ms debounce

    def test_highlights_matching_node_and_dims_the_rest(self):
        self._highlight('email')
        hit = self.page.evaluate(
            '''[...document.querySelectorAll('.er-node.word-hit')].map(n=>n.dataset.name)''')
        dim = self.page.evaluate(
            '''[...document.querySelectorAll('.er-node.word-dim')].map(n=>n.dataset.name)''')
        self.assertEqual(hit, ['users'])
        self.assertEqual(sorted(dim), ['audit_logs', 'comments', 'likes', 'posts'])

    def test_does_not_filter_the_table_list(self):
        before = self.page.evaluate('''[...document.querySelectorAll('.table-item')].length''')
        self._highlight('email')
        after = self.page.evaluate('''[...document.querySelectorAll('.table-item')].length''')
        self.assertEqual(before, after, 'the Highlight box must never hide table-list rows')
        marked = self.page.evaluate(
            '''[...document.querySelectorAll('.table-item.word-hit .tname')].map(e=>e.textContent)''')
        self.assertEqual(marked, ['users'])

    def test_shows_a_match_count(self):
        self._highlight('user_id')  # matches posts/comments/likes/audit_logs, not users
        self.assertEqual(self.page.evaluate("document.getElementById('word-search-count').textContent"), '4')
        self._highlight('')
        self.assertEqual(self.page.evaluate("document.getElementById('word-search-count').textContent"), '')

    def test_right_pane_marks_matches(self):
        self._highlight('email')
        self.page.click('[data-name="users"]')
        self.page.wait_for_timeout(50)
        marks = self.page.evaluate(
            '''[...document.querySelectorAll('#right-pane mark')].map(e=>e.textContent)''')
        self.assertTrue(marks and all(m.lower() == 'email' for m in marks))

    def test_clear_button_resets_everything(self):
        self._highlight('email')
        self.page.click('#word-search-clear')
        self.page.wait_for_timeout(50)
        self.assertEqual(self.page.evaluate("document.getElementById('word-search').value"), '')
        self.assertEqual(self.page.evaluate(
            '''document.querySelectorAll('.er-node.word-hit,.er-node.word-dim').length'''), 0)

    def test_escape_clears_when_box_is_focused(self):
        self.page.fill('#word-search', 'email')
        self.page.wait_for_timeout(250)
        self.page.press('#word-search', 'Escape')
        self.page.wait_for_timeout(50)
        self.assertEqual(self.page.evaluate("document.getElementById('word-search').value"), '')
        self.assertEqual(self.page.evaluate(
            '''document.querySelectorAll('.er-node.word-hit').length'''), 0)

    def test_enter_cycles_through_matches(self):
        self.page.fill('#word-search', 'user_id')  # 4 matches: posts, comments, likes, audit_logs
        self.page.wait_for_timeout(250)
        seen=[]
        for _ in range(4):
            self.page.press('#word-search', 'Enter')
            self.page.wait_for_timeout(50)
            seen.append(self.page.evaluate('selectionAnchor'))
        self.assertEqual(len(set(seen)), 4, f'each Enter should land on a different match, got {seen}')
        self.assertEqual(set(seen), {'posts', 'comments', 'likes', 'audit_logs'})
        # a 5th Enter wraps back to the first match
        self.page.press('#word-search', 'Enter')
        self.page.wait_for_timeout(50)
        self.assertEqual(self.page.evaluate('selectionAnchor'), seen[0])

    def test_shift_enter_cycles_backward(self):
        self.page.fill('#word-search', 'user_id')  # same 4 matches as above
        self.page.wait_for_timeout(250)
        forward=[]
        for _ in range(3):
            self.page.press('#word-search', 'Enter')
            self.page.wait_for_timeout(50)
            forward.append(self.page.evaluate('selectionAnchor'))
        # walking back should retrace the same path in reverse
        self.page.press('#word-search', 'Shift+Enter')
        self.page.wait_for_timeout(50)
        self.assertEqual(self.page.evaluate('selectionAnchor'), forward[-2])
        self.page.press('#word-search', 'Shift+Enter')
        self.page.wait_for_timeout(50)
        self.assertEqual(self.page.evaluate('selectionAnchor'), forward[-3])

    def test_comment_only_match_highlights_the_logical_name_text_itself(self):
        # regression: the whole node already got an amber border on any
        # match (name/column/comment), but a comment-only match had no
        # visible mark on the text itself — unlike a matching column,
        # which gets its own highlighted row. The logical-name tspan must
        # turn amber when the match is specifically in the comment.
        html = _build_html_with_comments()  # users: 'Customer accounts'
        page = self.browser.new_page()
        try:
            page.goto(html.as_uri())
            page.wait_for_function('typeof nodePos.users !== "undefined"')
            page.fill('#word-search', 'customer')
            page.wait_for_timeout(250)
            info = page.evaluate('''() => {
                const g = document.querySelector('.er-node[data-name="users"]');
                const span = g.querySelector('.n-title .n-logical');
                return {cls: span.getAttribute('class'), fill: getComputedStyle(span).fill};
            }''')
            self.assertIn('n-namehit', info['cls'])
            self.assertEqual(info['fill'], 'rgb(245, 158, 11)')  # amber, same as elsewhere in word-search
        finally:
            page.close()

    def test_coexists_with_the_left_pane_filter_search(self):
        # the two searches must not interfere with each other
        self.page.fill('#search', 'posts')  # filters the list down to 'posts'
        self._highlight('email')            # highlights 'users' (not in the filtered list)
        visible = self.page.evaluate(
            '''[...document.querySelectorAll('.table-item .tname')].map(e=>e.textContent)''')
        self.assertEqual(visible, ['posts'], 'the left-pane filter should still narrow the list')
        hit = self.page.evaluate(
            '''[...document.querySelectorAll('.er-node.word-hit')].map(n=>n.dataset.name)''')
        self.assertEqual(hit, ['users'], 'the toolbar highlight is independent of the filter')


@unittest.skipUnless(HAVE_PLAYWRIGHT, 'playwright not installed')
class TestLogicalNames(unittest.TestCase):
    """Table comments displayed as a 'logical name' (physical name as-is,
    e.g. users（Customer accounts）) — searchable through both the
    left-pane filter and the toolbar highlight, in addition to the
    existing table/column name matching."""
    @classmethod
    def setUpClass(cls):
        cls.html_path = _build_html_with_comments()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f'Chromium not available: {e}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.goto(self.html_path.as_uri())
        self.page.wait_for_function('typeof nodePos.users !== "undefined"')
        self.page.wait_for_timeout(50)

    def tearDown(self):
        self.page.close()

    # .n-title's textContent recurses into its nested <title> tooltip too
    # (the full, untruncated comment) — read only the direct text/tspan
    # children to get what's actually rendered on screen.
    HEADER_TEXT_JS = '''(name) => [...document.querySelector(
        `.er-node[data-name="${name}"] .n-title`).childNodes]
        .filter(n => n.nodeName !== 'title')
        .map(n => n.textContent).join('')'''

    def _header_text(self, name):
        return self.page.evaluate(self.HEADER_TEXT_JS, name)

    def test_node_header_shows_physical_and_logical_name(self):
        # 'Customer accounts' is 17 chars, one over the 16-unit cap
        self.assertEqual(self._header_text('users'), 'users（Customer account…）')

    def test_table_without_a_comment_shows_only_the_physical_name(self):
        self.assertEqual(self._header_text('comments'), 'comments')

    def test_cjk_comment_truncates_by_display_width_not_character_count(self):
        # '投稿記事管理テーブル（本番用）' is all full-width chars (2 units
        # each); the 16-unit cap fits exactly 8 of them before truncating
        self.assertEqual(self._header_text('posts'), 'posts（投稿記事管理テー…）')

    def test_header_icons_stay_clear_of_a_long_logical_name(self):
        # regression: calcSize() originally sized the node to fit the
        # header *text* only, not the icon cluster (⊖/⊕/▤) that always
        # occupies the header's right edge — a long-enough logical name
        # made the title text visually run into/under the icons
        geo = self.page.evaluate('''() => {
            const g = document.querySelector('.er-node[data-name="posts"]');
            const titleRight = g.querySelector('.n-title').getBBox().x
                + g.querySelector('.n-title').getBBox().width;
            const iconLeft = [...g.querySelectorAll('text.n-mode')]
                .map(t => t.getBBox().x)
                .reduce((a, b) => Math.min(a, b));
            return {titleRight, iconLeft};
        }''')
        self.assertLess(geo['titleRight'], geo['iconLeft'],
            f'title text (right edge {geo["titleRight"]:.0f}) overlaps the icon cluster '
            f'(left edge {geo["iconLeft"]:.0f})')

    def test_left_pane_filter_matches_on_comment(self):
        self.page.fill('#search', 'Customer')
        self.page.wait_for_timeout(50)
        visible = self.page.evaluate(
            '''[...document.querySelectorAll('.table-item .tname')].map(e => e.textContent)''')
        self.assertEqual(visible, ['users'])

    def test_left_pane_lists_the_logical_name_alongside_the_row(self):
        text = self.page.evaluate('''() => {
            const item = [...document.querySelectorAll('.table-item')]
                .find(el => el.querySelector('.tname').textContent === 'users');
            return item.querySelector('.tlogical').textContent;
        }''')
        self.assertEqual(text, 'Customer account…')  # same 16-unit cap as the header

    def test_highlight_search_matches_on_comment(self):
        self.page.fill('#word-search', 'Customer')
        self.page.wait_for_timeout(250)
        hit = self.page.evaluate(
            '''[...document.querySelectorAll('.er-node.word-hit')].map(n => n.dataset.name)''')
        self.assertEqual(hit, ['users'])


@unittest.skipUnless(HAVE_PLAYWRIGHT, 'playwright not installed')
class TestSearchModifiers(unittest.TestCase):
    """Aa (case-sensitive) / .* (regex) toggles — independent per search
    box (left-pane filter vs. toolbar Highlight), each defaulting off
    (case-insensitive substring, matching every prior release's
    behavior)."""
    @classmethod
    def setUpClass(cls):
        cls.html_path = _build_html()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f'Chromium not available: {e}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.goto(self.html_path.as_uri())
        self.page.wait_for_function('typeof nodePos.users !== "undefined"')
        self.page.wait_for_timeout(50)

    def tearDown(self):
        self.page.close()

    def test_column_comment_is_searchable(self):
        # a dedicated fixture with a real "Stock keeping unit"-style
        # column comment — this is the maintainer's original bug report
        table_rows = [('products', '')]
        col_rows = [_col('products', 'id', key='PRI'),
                    ('products', 'sku', 'varchar', 'varchar(40)', 'NO', '', '', '', 'Stock keeping unit')]
        tables = erd.mysql_ir(table_rows, col_rows, [], [])
        args = SimpleNamespace(output='', models=None, excel=None, max_rows=15,
                                only=None, exclude=None, infer_fk=False)
        tmp = tempfile.mkdtemp()
        out = Path(tmp) / 'out.html'
        args.output = str(out)
        erd._finish(tables, args, 'e2e_fixture')
        page = self.browser.new_page()
        try:
            page.goto(out.as_uri())
            page.wait_for_function('typeof nodePos.products !== "undefined"')
            page.fill('#word-search', 'stock')
            page.wait_for_timeout(250)
            hit = page.evaluate(
                '''[...document.querySelectorAll('.er-node.word-hit')].map(n => n.dataset.name)''')
            self.assertEqual(hit, ['products'])
            page.fill('#word-search', '')
            page.wait_for_timeout(100)
            page.fill('#search', 'stock')
            page.wait_for_timeout(150)
            visible = page.evaluate(
                '''[...document.querySelectorAll('.table-item .tname')].map(e => e.textContent)''')
            self.assertEqual(visible, ['products'])
        finally:
            page.close()

    def test_toggles_are_off_by_default(self):
        for btn_id in ('fs-case', 'fs-regex', 'ws-case', 'ws-regex'):
            self.assertFalse(self.page.evaluate(
                f'''document.getElementById('{btn_id}').classList.contains('active')'''), btn_id)

    def test_highlight_case_sensitivity_toggle(self):
        self.page.click('#ws-case')
        self.page.fill('#word-search', 'Users')  # capital U; table name is lowercase 'users'
        self.page.wait_for_timeout(250)
        hit = self.page.evaluate(
            '''[...document.querySelectorAll('.er-node.word-hit')].map(n => n.dataset.name)''')
        self.assertEqual(hit, [], 'case-sensitive mode must not match differently-cased text')
        self.page.fill('#word-search', 'users')
        self.page.wait_for_timeout(250)
        hit2 = self.page.evaluate(
            '''[...document.querySelectorAll('.er-node.word-hit')].map(n => n.dataset.name)''')
        self.assertIn('users', hit2)

    def test_highlight_regex_toggle(self):
        self.page.click('#ws-regex')
        self.page.fill('#word-search', '^users$')
        self.page.wait_for_timeout(250)
        hit = self.page.evaluate(
            '''[...document.querySelectorAll('.er-node.word-hit')].map(n => n.dataset.name)''')
        self.assertEqual(hit, ['users'])

    def test_invalid_regex_matches_nothing_and_shows_error_state(self):
        self.page.click('#ws-regex')
        self.page.fill('#word-search', '(unclosed')
        self.page.wait_for_timeout(250)
        self.assertTrue(self.page.evaluate(
            "document.getElementById('word-search-box').classList.contains('bad-re')"))
        self.assertEqual(self.page.evaluate("document.getElementById('word-search-count').textContent"), '!')
        hit = self.page.evaluate(
            '''[...document.querySelectorAll('.er-node.word-hit')].map(n => n.dataset.name)''')
        self.assertEqual(hit, [], 'an invalid pattern must match nothing, not silently fall back to substring mode')

    def test_left_pane_filter_case_and_regex_toggles(self):
        self.page.click('#fs-regex')
        self.page.fill('#search', '^users$')
        self.page.wait_for_timeout(150)
        visible = self.page.evaluate(
            '''[...document.querySelectorAll('.table-item .tname')].map(e => e.textContent)''')
        self.assertEqual(visible, ['users'])

    def test_left_pane_invalid_regex_shows_error_and_empties_the_list(self):
        self.page.click('#fs-regex')
        self.page.fill('#search', '[unclosed')
        self.page.wait_for_timeout(150)
        self.assertTrue(self.page.evaluate(
            "document.getElementById('search-box').classList.contains('bad-re')"))
        visible = self.page.evaluate(
            '''[...document.querySelectorAll('.table-item .tname')].map(e => e.textContent)''')
        self.assertEqual(visible, [])

    def test_toggles_are_independent_per_search_box(self):
        self.page.click('#ws-case')
        self.assertTrue(self.page.evaluate("document.getElementById('ws-case').classList.contains('active')"))
        self.assertFalse(self.page.evaluate("document.getElementById('fs-case').classList.contains('active')"))

    def test_toggles_persist_across_reload(self):
        self.page.click('#fs-regex')
        self.page.click('#ws-case')
        self.page.wait_for_timeout(50)
        self.page.reload()
        self.page.wait_for_function('typeof nodePos.users !== "undefined"')
        self.assertTrue(self.page.evaluate("document.getElementById('fs-regex').classList.contains('active')"))
        self.assertTrue(self.page.evaluate("document.getElementById('ws-case').classList.contains('active')"))
        self.assertFalse(self.page.evaluate("document.getElementById('fs-case').classList.contains('active')"))
        self.assertFalse(self.page.evaluate("document.getElementById('ws-regex').classList.contains('active')"))


@unittest.skipUnless(HAVE_PLAYWRIGHT, 'playwright not installed')
class TestNameDisplayMode(unittest.TestCase):
    """Both/Physical/Logical toolbar toggle (live view) and its
    independent export-time counterpart in the Export menu's Image
    options. Default for both is 'Both' — today's existing behavior."""
    @classmethod
    def setUpClass(cls):
        cls.html_path = _build_html_with_comments()  # users: 'Customer accounts'; comments/posts/likes/audit_logs vary
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f'Chromium not available: {e}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.goto(self.html_path.as_uri())
        self.page.wait_for_function('typeof nodePos.users !== "undefined"')
        self.page.wait_for_timeout(50)

    def tearDown(self):
        self.page.close()

    def _header_text(self, name='users'):
        # the mode toggle hides tspans via CSS (display:none), not by
        # removing them from the DOM — filter to what's actually visible,
        # not just what's present
        return self.page.evaluate(f'''() => [...document.querySelector(
            `.er-node[data-name="{name}"] .n-title`).childNodes]
            .filter(n => n.nodeName !== 'title')
            .filter(n => n.nodeType === Node.TEXT_NODE || getComputedStyle(n).display !== 'none')
            .map(n => n.textContent).join('')''')

    def test_default_mode_is_both(self):
        self.assertTrue(self.page.evaluate("document.querySelector('[data-nm=\"0\"]').classList.contains('active')"))
        self.assertEqual(self._header_text(), 'users（Customer account…）')

    def test_physical_only_mode_hides_the_logical_name(self):
        self.page.click('[data-nm="1"]')
        self.page.wait_for_timeout(100)
        self.assertEqual(self._header_text(), 'users')

    def test_logical_only_mode_hides_the_physical_name(self):
        self.page.click('[data-nm="2"]')
        self.page.wait_for_timeout(100)
        self.assertEqual(self._header_text(), 'Customer account…')

    def test_logical_only_mode_falls_back_to_physical_when_no_comment(self):
        self.page.click('[data-nm="2"]')
        self.page.wait_for_timeout(100)
        self.assertEqual(self._header_text('comments'), 'comments')

    def test_left_pane_lists_reacts_are_unaffected_by_display_mode(self):
        # the .tlogical span in the list is a separate, always-both
        # feature (finding *why* a row matched); the toolbar mode only
        # controls the diagram node headers
        self.page.click('[data-nm="1"]')
        self.page.wait_for_timeout(100)
        text = self.page.evaluate('''() => {
            const item = [...document.querySelectorAll('.table-item')]
                .find(el => el.querySelector('.tname').textContent === 'users');
            return item.querySelector('.tlogical')?.textContent;
        }''')
        self.assertEqual(text, 'Customer account…')

    def test_export_mode_is_independent_of_the_live_mode(self):
        self.page.click('[data-nm="1"]')  # live: physical only
        self.page.wait_for_timeout(100)
        svg = self.page.evaluate('''() => {
            const built = buildExportSvg();
            return new XMLSerializer().serializeToString(built.svg);
        }''')
        self.assertNotIn('.n-logical,.n-paren{display:none}', svg,
            'export should still default to Both even though the live view is Physical-only')

    def test_export_mode_can_be_set_independently_via_the_popup(self):
        self.page.click('#btn-export-toggle')
        self.page.wait_for_timeout(50)
        self.page.click('[data-xnm="2"]')  # export: logical only
        self.page.wait_for_timeout(50)
        svg = self.page.evaluate('''() => {
            const built = buildExportSvg();
            return new XMLSerializer().serializeToString(built.svg);
        }''')
        self.assertIn('.er-node.has-logical .n-physical,.er-node.has-logical .n-paren{display:none}', svg)
        # live view must stay on Both — the popup click must not leak back
        self.assertEqual(self._header_text(), 'users（Customer account…）')

    def test_export_namemode_click_does_not_close_the_popup(self):
        self.page.click('#btn-export-toggle')
        self.page.wait_for_timeout(50)
        self.page.click('[data-xnm="1"]')
        self.page.wait_for_timeout(50)
        self.assertTrue(self.page.evaluate(
            "document.getElementById('export-menu').classList.contains('open')"))

    def test_modes_persist_across_reload(self):
        self.page.click('[data-nm="1"]')
        self.page.click('#btn-export-toggle')
        self.page.wait_for_timeout(50)
        self.page.click('[data-xnm="2"]')
        self.page.wait_for_timeout(50)
        self.page.reload()
        self.page.wait_for_function('typeof nodePos.users !== "undefined"')
        self.assertEqual(self.page.evaluate('nameMode'), 1)
        self.assertEqual(self.page.evaluate('exportNameMode'), 2)


@unittest.skipUnless(HAVE_PLAYWRIGHT, 'playwright not installed')
class TestDirectionControlVisibleWithoutAutoExpand(unittest.TestCase):
    """The Direction buttons (Both/Deps/Dependents) drive each table's ⊕
    manual-expand button too, not just Auto-expand's BFS — so they must be
    changeable even with Auto-expand off. Depth stays gated (it's BFS-only,
    irrelevant to a single ⊕ step)."""
    @classmethod
    def setUpClass(cls):
        cls.html_path = _build_html()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f'Chromium not available: {e}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.goto(self.html_path.as_uri())
        self.page.wait_for_function('typeof nodePos.users !== "undefined"')
        self.page.wait_for_timeout(50)

    def tearDown(self):
        self.page.close()

    def test_direction_control_is_visible_by_default(self):
        self.assertFalse(self.page.evaluate("document.getElementById('auto-expand').checked"))
        self.assertEqual(self.page.evaluate(
            "getComputedStyle(document.getElementById('dir-ctrl')).display"), 'flex')

    def test_depth_control_stays_hidden_without_auto_expand_or_focus(self):
        self.assertEqual(self.page.evaluate(
            "getComputedStyle(document.getElementById('depth-ctrl')).display"), 'none')

    def test_direction_button_changes_expand_dir_with_auto_expand_off(self):
        self.page.click('[data-dir="out"]')
        self.page.wait_for_timeout(50)
        self.assertEqual(self.page.evaluate('expandDir'), 'out')
        self.assertTrue(self.page.evaluate(
            "document.querySelector('[data-dir=\"out\"]').classList.contains('active')"))

    def test_plus_button_on_a_node_honors_the_direction_without_auto_expand(self):
        # 'out' = only what this table depends on (belongs_to). 'users' is
        # the FK target of posts/comments/likes/audit_logs (they depend on
        # it), so in 'out' mode users' ⊕ must add nothing.
        self.page.click('[data-dir="out"]')
        self.page.wait_for_timeout(50)
        self.page.evaluate('''() => {
            const plus = [...document.querySelectorAll('.er-node[data-name="users"] .n-mode')]
                .find(el => el.firstChild.textContent === '⊕');
            plus.dispatchEvent(new MouseEvent('click', {bubbles: true}));
        }''')
        self.page.wait_for_timeout(50)
        toast = self.page.evaluate("document.getElementById('toast').textContent")
        self.assertIn('No related tables to add', toast)


@unittest.skipUnless(HAVE_PLAYWRIGHT, 'playwright not installed')
class TestHelpMenu(unittest.TestCase):
    """The toolbar ? button: opens the shortcuts/help popup, closes on
    Escape (ahead of everything else in the Esc chain) and on outside
    click, and links to the hosted manual."""
    @classmethod
    def setUpClass(cls):
        cls.html_path = _build_html()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as e:
            cls.pw.stop()
            raise unittest.SkipTest(f'Chromium not available: {e}')

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page()
        self.page.goto(self.html_path.as_uri())
        self.page.wait_for_function('typeof nodePos.users !== "undefined"')
        self.page.wait_for_timeout(50)

    def tearDown(self):
        self.page.close()

    def _is_open(self):
        return self.page.evaluate(
            "document.getElementById('help-menu').classList.contains('open')")

    def test_button_toggles_the_popup(self):
        self.assertFalse(self._is_open())
        self.page.click('#btn-help')
        self.assertTrue(self._is_open())
        self.page.click('#btn-help')
        self.assertFalse(self._is_open())

    def test_escape_closes_the_popup_without_touching_focus(self):
        self.page.dblclick('.er-node[data-name="users"]')
        self.page.wait_for_timeout(100)
        self.page.click('#btn-help')
        self.assertTrue(self._is_open())
        self.page.keyboard.press('Escape')
        self.assertFalse(self._is_open())
        # focus survived: only the popup consumed this Esc
        self.assertEqual(self.page.evaluate('focusedTable'), 'users')

    def test_outside_click_closes_the_popup(self):
        self.page.click('#btn-help')
        self.assertTrue(self._is_open())
        self.page.click('#er-svg', position={'x': 5, 'y': 5})
        self.assertFalse(self._is_open())

    def test_manual_link_targets_the_hosted_manual(self):
        href = self.page.get_attribute('#help-menu .help-link', 'href')
        self.assertEqual(href, 'https://orapli.github.io/erdscope/manual.html')
        self.assertEqual(
            self.page.get_attribute('#help-menu .help-link', 'target'), '_blank')


if __name__ == '__main__':
    unittest.main()
