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


if __name__ == '__main__':
    unittest.main()
