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
                            only=None, exclude=None, no_infer_fk=False)
    tmp = tempfile.mkdtemp()
    out = Path(tmp) / 'out.html'
    args.output = str(out)
    erd._finish(tables, args, 'e2e_fixture')
    return out


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


if __name__ == '__main__':
    unittest.main()
