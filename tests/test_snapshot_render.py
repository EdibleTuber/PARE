from pare.commands._snapshot_render import render_table, render_catalog


def test_render_table_dynamic_columns_and_header():
    rows = [{"identifier": "com.bank", "name": "Bank", "pid": 0},
            {"identifier": "com.maps", "name": "Maps", "pid": 11}]
    out = render_table(rows)
    assert "identifier" in out and "name" in out and "pid" in out
    assert "com.bank" in out and "com.maps" in out


def test_render_table_clips_to_width():
    rows = [{"name": "x" * 300}]
    out = render_table(rows, max_width=80)
    assert all(len(line) <= 80 for line in out.splitlines())


def test_render_table_empty():
    assert "no rows" in render_table([]).lower()


def test_render_catalog_lists_sources_and_counts():
    out = render_catalog([{"source": "enumerate_applications:device=emu", "count": 21}])
    assert "enumerate_applications:device=emu" in out
    assert "21" in out
