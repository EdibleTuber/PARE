# TUI Footer Hints, Pane Labels, and the /help Table — Implementation Plan

> **For the implementer:** TDD is mandatory — every step that says "watch
> RED" means run the command and see the named assertion fail before
> writing implementation code. Deployment and live verification are
> operator steps, never agent steps.

**Goal:** In `pare-tui`, an operator sees at a glance how to quit and how
to get help (footer), which pane is which (title strips), and `/help`
returns a readable aligned table instead of a wall of flat lines.

**Architecture:** Three small, independently testable pieces:
1. **Footer hints (PARE)** — declare `^q Quit` and `f1 Help` in
   `PareTUI.BINDINGS`. Textual 8.2.8's Footer renders the *app
   subclass's own* `BINDINGS` (verified live: the base App's `^q -> quit`
   is NOT shown; adding `LAYOUT_KEY` made `^l Layout` appear). `F1` is an
   async action that submits `/help` through the exact same code path as
   typing it, so the daemon stays the single source of truth for the
   command list.
2. **Pane labels (PARE)** — a one-line dim `Static` strip: "Chat" as the
   first child of `#chat-area`; "UART" as an optional `title` on the
   generic `PaneDock`. Labels are static titles; live state stays in the
   `StatusBar` row (no duplication).
3. **`/help` table (agent_core)** — `Help.run` pads the command column to
   the longest entry + 2, descriptions in a second column. One change
   fixes both the CLI REPL and the daemon (the TUI just renders the
   daemon's plain text). Release as `v1.11.1` per the established
   agent_core pattern: code + version bump + changelog + release commit on
   `main`, PARE consumer gate, then tag and push.

**Tech stack:** Python 3.12, Textual 8.2.8 (PareTUI), agent_core
(daemon-side commands), pytest + Textual `run_test`/`Pilot` +
`export_screenshot` (SVG) for operator-visible render assertions.

## File Structure

| File | Change |
|---|---|
| `PARE/pare/tui/app.py` | Task 1: `BINDINGS` (line 216), extract `_submit_line` from `on_input_submitted` (line 719), add `action_show_help`. Task 2: "Chat" label in `compose()` (line 343), `title="UART"` on the `PaneDock` (line 350), `.pane-label` CSS. |
| `PARE/pare/tui/panes/base.py` | Task 2: `PaneDock.__init__` gains keyword-only `title: str \| None = None`; `compose()` yields the label strip before the panes. |
| `PARE/tests/test_tui_operator_fixes.py` | Tasks 1–2: new test section 5 (footer + F1) and section 6 (labels), plus the `_svg_rows` screen-reconstruction helper. |
| `PARE/pyproject.toml` | Task 4: agent_core pin `@v1.11.0` → `@v1.11.1` (line 11). |
| `agent_core/agent_core/commands/_builtin_impls.py` | Task 3: `Help.run` reformat. |
| `agent_core/tests/test_builtin_commands.py` | Task 3: format-pinning tests (existing two `/help` tests keep passing unchanged — they assert loose substrings that survive the reformat). |
| `agent_core/pyproject.toml`, `agent_core/CHANGELOG.md` | Task 3: version `1.11.1` + changelog entry. |

**Branches:** PARE work on a new branch `feat/tui-footer-help-labels` cut
from `origin/main` (`13a96ab`). agent_core work goes straight to its
`main` — that is its established release pattern (see the v1.11.0 release
commit `c5876da`). **Note:** PR #78 (open, unmerged) also touched
`pare/tui/panes/*`; this plan touches `app.py` and `PaneDock` init/compose
in `base.py` only — different methods, low conflict risk. If PR #78 merges
first, rebase this branch onto `main` before merge.

**Commit footer convention** (matches the previous plan's commits):

```
Co-Authored-By: Qwen3.8 (OpenCode) <noreply@localhost>
OpenCode-Session: ses_f107afda7ffeQuntLuBQOYJRNf
```

**Ledger** (executing-plans): create `.superpowers/sdd/2026-10-01-tui-footer-help-labels/progress.md`
at start (gitignored) and update it alongside each commit.

**PARE test runner note:** run PARE tests with `.venv/bin/python -m pytest`
— **not** `uv run pytest`. `uv run` re-syncs the project environment and
would reinstall agent_core from the git pin, silently clobbering the
local-path install that Task 4's consumer gate depends on.

---

### Task 1 — PARE: footer hints `^q Quit` + `f1 Help`

**Files:** modify `pare/tui/app.py`, `tests/test_tui_operator_fixes.py`

**Context:** the live footer today reads `^l Layout … ^p palette` — no quit,
no help. The base `App.BINDINGS` contains `ctrl+q -> quit | Quit` but the
Footer does **not** render base bindings (verified in this session), so the
fix is declaring both in `PareTUI.BINDINGS`. `F1` must behave *exactly*
like typing `/help` + Enter: the same `you> /help` echo line, the same
`parse_input` call, the same `[send failed: ...]` line when no daemon is
connected. That means extracting the submit body of `on_input_submitted`
into a shared `_submit_line`.

**Step 1 — cut the branch:**

```bash
cd /mnt/secondary/projects/PARE
git switch -c feat/tui-footer-help-labels   # from origin/main (13a96ab)
```

Create the ledger file.

**Step 2 — write the failing tests.** Add a new section to
`tests/test_tui_operator_fixes.py` after the layout tests (after
`test_status_bar_has_its_own_row_above_the_footer`):

```python
# --- 5. footer hints: ^q quit, f1 help (2026-10-01) ------------------------


def _svg_rows(app) -> list[str]:
    """The operator-visible screen, rows reconstructed from
    `export_screenshot(simplify=True)`. The SVG holds one `<text>` node per
    glyph, so this reassembles them (col from x, row from y — the same
    transform as the 2026-10-01 manual dump script that sized the 120x30
    grid). Use size=(120, 30) in tests that call this."""
    import html as _html
    import re

    svg = app.export_screenshot(simplify=True)
    cells: dict[tuple[int, int], str] = {}
    for m in re.finditer(
        r'<text class="[^"]*" x="([\d.]+)" y="([\d.]+)"[^>]*>([^<]*)</text>',
        svg,
    ):
        col = round(float(m.group(1)) / 12.2)
        row = round((float(m.group(2)) - 20) / 24.4)
        for i, ch in enumerate(_html.unescape(m.group(3))):
            cells[(col + i, row)] = ch
    if not cells:
        return []
    max_c = max(c for c, _ in cells)
    return [
        "".join(cells.get((c, r), " ") for c in range(max_c + 1)).rstrip()
        for r in range(max(r for _, r in cells) + 1)
    ]


async def test_quit_and_help_are_in_the_footer(tmp_path):
    """The Footer renders only the app subclass's own BINDINGS (the base
    App's ^q -> quit is NOT shown -- verified live in Textual 8.2.8), so
    both hints must be declared here to appear at a glance."""
    from pare.tui.app import PareTUI

    declared = {(b.key, b.description) for b in PareTUI.BINDINGS}
    assert ("ctrl+q", "Quit") in declared
    assert ("f1", "Help") in declared
    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        footer = _svg_rows(app)[-1]
        assert "Quit" in footer
        assert "Help" in footer


async def test_f1_sends_help_exactly_like_typing_it(tmp_path):
    session = _Session()
    app = _make_app(tmp_path, session=session)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f1")
        await pilot.pause()
        lines = _lines(app)
        assert "you> /help" in lines
        commands = [m for m in session.sent if isinstance(m, CommandMessage)]
        assert commands == [CommandMessage(name="help", args="")]


async def test_f1_with_no_live_connection_fails_like_typed_help(tmp_path):
    """F1 must go through the same path as typing /help: echo the line,
    then a [send failed: ...] transcript line, never a crash out of the
    event handler."""
    session = _Session(fail_send=RuntimeError("connect() before send()"))
    app = _make_app(tmp_path, session=session)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("f1")
        await pilot.pause()
        lines = _lines(app)
        assert "you> /help" in lines
        assert any("[send failed:" in line for line in lines)
```

**Step 3 — watch RED:**

```bash
.venv/bin/python -m pytest tests/test_tui_operator_fixes.py -k "footer or f1" -x -q
```

Expected: `test_quit_and_help_are_in_the_footer` fails on
`assert ("ctrl+q", "Quit") in declared`; the F1 tests fail on
`"you> /help" in lines` (no binding, nothing happens).

**Step 4 — implement in `pare/tui/app.py`:**

`BINDINGS` (line 216) becomes:

```python
    BINDINGS = [
        Binding(LAYOUT_KEY, "cycle_layout", "Layout"),
        # Declared here, not inherited: the Textual 8.2.8 Footer renders
        # only the app subclass's own BINDINGS (verified: the base App's
        # ^q -> quit does not appear in the footer), so "at a glance"
        # requires the explicit entries.
        Binding("ctrl+q", "quit", "Quit"),
        Binding("f1", "show_help", "Help"),
    ]
```

`on_input_submitted` (lines 719–739) splits into:

```python
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "chat-input":
            return
        line = event.value
        event.input.value = ""
        await self._submit_line(line)

    async def _submit_line(self, line: str) -> None:
        """One line into the daemon, exactly as typing it would send it:
        the `you>` echo first (a reply cannot precede it, and it stays if
        the send fails), then the send with its failure line. `F1`
        (action_show_help) submits "/help" through this same path."""
        if is_sendable(line):
            # Before the send: a reply cannot precede it, and it stays if
            # the send fails.
            self._write_transcript(
                Text.assemble((ECHO_PREFIX, "bold cyan"), line.strip())
            )
        try:
            await parse_input(self.session, line)
        except Exception as exc:
            # Typing into the chat box while there is no live daemon
            # connection (verified: DaemonConnection.send asserts
            # "connect() before send()" -- agent_core/client.py:53) must
            # not crash the whole app out from under an event handler; the
            # UART pane's own I3 resilience is the model here.
            logger.exception("failed to send chat/command input")
            self._write_transcript(f"[send failed: {exc}]")

    async def action_show_help(self) -> None:
        await self._submit_line("/help")
```

(Keep `action_show_help` next to `action_cycle_layout`; keep the
existing comments intact as shown.)

**Step 5 — GREEN + full file:**

```bash
.venv/bin/python -m pytest tests/test_tui_operator_fixes.py -q
```

All tests in the file pass (including the pre-existing
`test_layout_key_is_in_the_footer_bindings`, which must stay green).

**Step 6 — commit:**

```bash
git add pare/tui/app.py tests/test_tui_operator_fixes.py
git commit -m "feat(tui): footer shows ^q Quit and f1 Help; F1 submits /help

The Textual 8.2.8 Footer renders only the app subclass's own BINDINGS,
so the base App's ^q never appeared; declare both hints explicitly. F1
reuses the input-submit path (echo + parse_input + failure line), so
behavior is identical to typing /help, including with no live daemon."
```

Update the ledger.

---

### Task 2 — PARE: pane title labels ("Chat" / "UART")

**Files:** modify `pare/tui/app.py`, `pare/tui/panes/base.py`,
`tests/test_tui_operator_fixes.py`

**Context:** in the live 120x30 view the two columns are unlabeled; the
operator had to guess which side was chat and which was UART. One-line dim
strips fix it. The "Chat" label goes directly in `compose()` (chat-area is
a plain `Vertical` in app.py); the "UART" label goes through a new
optional `title` on the *generic* `PaneDock` (base.py) so the label lives
at dock level — the generic base must not hardcode "UART".

**Step 1 — write the failing tests.** Append to the new section in
`tests/test_tui_operator_fixes.py`:

```python
# --- 6. pane title labels (2026-10-01) --------------------------------------


async def test_chat_and_uart_panes_carry_title_labels(tmp_path):
    from pare.tui.app import LAYOUT_KEY
    from pare.tui.panes.base import PaneDock

    app = _make_app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.pause()
        chat_label = app.query_one("#chat-area .pane-label")
        assert str(chat_label.render()) == "Chat"
        dock_label = app.query_one("#pane-dock .pane-label")
        assert str(dock_label.render()) == "UART"
        # The UART label sits above the pane it titles.
        first_pane = app.query_one("#pane-dock", PaneDock).panes[0]
        assert dock_label.region.y < first_pane.region.y
        # Operator-visible in the render, not just in the widget tree.
        screen = "\n".join(_svg_rows(app))
        assert "Chat" in screen
        assert "UART" in screen
        # Hiding the dock (4th layout preset) hides the label with it --
        # no orphan strip.
        for _ in range(3):
            await pilot.press(LAYOUT_KEY)
            await pilot.pause()
        assert not dock_label.visible
```

**Step 2 — watch RED:**

```bash
.venv/bin/python -m pytest tests/test_tui_operator_fixes.py -k title_labels -x -q
```

Expected: `NoMatch` — no `.pane-label` widget exists yet.

**Step 3 — implement.**

`pare/tui/panes/base.py` — `PaneDock` (line 159): add the import
(`from textual.widgets import Static` alongside the existing
`textual.containers` import), and:

```python
    def __init__(
        self,
        panes: Sequence[Pane] = (),
        *,
        id: str | None = None,
        title: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._panes: list[Pane] = list(panes)
        self._title = title

    def compose(self):
        if self._title is not None:
            yield Static(self._title, classes="pane-label")
        yield from self._panes
```

Update the class docstring's "Owns ... layout (a vertical stack by
default)" sentence to mention the optional title strip.

`pare/tui/app.py`:

- `compose()` (line 343): inside `with Vertical(id="chat-area"):`, yield
  `Static("Chat", classes="pane-label")` **before** the `TranscriptLog`.
  Add `Static` to the `textual.widgets` import.
- Line 350: `yield PaneDock([self._build_uart_pane()], id="pane-dock", title="UART")`
- CSS: append to the `CSS` string:

```css
    /* One-line dim title strip above a pane's content. Static by design:
       live pane state belongs in the StatusBar row above the footer. */
    .pane-label {
        height: 1;
        text-style: dim;
        padding: 0 1;
        background: $panel;
    }
```

**Step 4 — GREEN + full file:**

```bash
.venv/bin/python -m pytest tests/test_tui_operator_fixes.py tests/test_tui_pane_dock.py -q
```

All pass (the pane-dock suite constructs `PaneDock` without a title — the
new parameter is keyword-only and optional, so nothing there changes).

**Step 5 — commit:**

```bash
git add pare/tui/app.py pare/tui/panes/base.py tests/test_tui_operator_fixes.py
git commit -m "feat(tui): label the panes -- 'Chat' and 'UART' title strips

One-line dim strips (height: 1, .pane-label) above each pane's content.
PaneDock gains an optional keyword-only title so the generic dock stays
source-agnostic; the Chat label is a plain Static in the app's compose."
```

Update the ledger.

---

### Task 3 — agent_core: `/help` aligned two-column table + release 1.11.1

**Files:** modify `agent_core/commands/_builtin_impls.py` (`Help.run`),
`tests/test_builtin_commands.py`, `pyproject.toml`, `CHANGELOG.md`

**Context:** `Help.run` today emits one flat line per command:
`  /name args  -  description` — a wall of text in both the CLI REPL and
the daemon (and therefore the TUI transcript). The fix pads the command
column to the longest entry plus a two-character gap, so descriptions line
up in a second column. Plain text only (no Rich markup, no ANSI) — it must
stay safe for the CLI. The empty-registry case must still yield just the
header (guard the `max()` over entries).

**Exact format** (this is what the tests pin):

```
Available commands:
  /hello [<name>]           Say hi to the agent
  /quit                     Exit
  /scratch [clear | <text>]  Read or manage the scratchpad
```

Rule: `entry = "/{name}" + (f" {args}" if args else "")`;
`width = max(len(entry)) + 2`;
`line = "  " + entry.ljust(width) + description`.

**Step 1 — write the failing tests.** In
`agent_core/tests/test_builtin_commands.py`, inside the existing `/help`
section (after `test_help_formats_args`):

```python
async def test_help_is_an_aligned_two_column_table():
    """2026-10-01: the flat "  /name args  -  desc" lines were a wall of
    text in the CLI and the daemon; the command column is now padded to
    the longest entry + 2 so descriptions line up. Plain text only."""
    cr = MagicMock()
    cr.metadata.return_value = [
        ("hello", "[<name>]", "Say hi to the agent"),
        ("quit", "", "Exit"),
        ("scratch", "[clear | <text>]", "Read or manage the scratchpad"),
    ]
    agent = MagicMock(command_registry=cr)
    body = _body(await _collect(Help().run("", _ctx(agent))))
    lines = body.splitlines()
    assert lines[0] == "Available commands:"
    # Column width = longest entry ("/scratch [clear | <text>]", 25) + 2.
    assert lines[1] == "  " + "/hello [<name>]".ljust(27) + "Say hi to the agent"
    assert lines[2] == "  " + "/quit".ljust(27) + "Exit"
    assert lines[3] == (
        "  " + "/scratch [clear | <text>]".ljust(27) + "Read or manage the scratchpad"
    )


async def test_help_with_no_commands_shows_only_the_header():
    cr = MagicMock()
    cr.metadata.return_value = []
    agent = MagicMock(command_registry=cr)
    body = _body(await _collect(Help().run("", _ctx(agent))))
    assert body == "Available commands:"
```

**Step 2 — watch RED:**

```bash
cd /mnt/secondary/projects/agent_core && uv run pytest tests/test_builtin_commands.py -k help -x -q
```

Expected: `test_help_is_an_aligned_two_column_table` fails on `lines[1]`
(the old flat format); `test_help_with_no_commands...` passes already
(empty registry → header only, as today) — that's fine, it pins the guard.

**Step 3 — implement `Help.run`:**

```python
    async def run(self, raw_args: str, ctx) -> AsyncIterator:
        rows = list(ctx.agent.command_registry.metadata())
        if not rows:
            yield ResponseMessage(text="Available commands:")
            return
        entries = [
            f"/{name}" + (f" {args}" if args else "") for name, args, _ in rows
        ]
        width = max(len(entry) for entry in entries) + 2
        lines = ["Available commands:"]
        for (name, args, desc), entry in zip(rows, entries):
            lines.append("  " + entry.ljust(width) + desc)
        yield ResponseMessage(text="\n".join(lines))
```

**Step 4 — GREEN + full agent_core suite:**

```bash
uv run pytest tests/ -q
```

All pass; the two pre-existing `/help` tests (`test_help_lists_commands`,
`test_help_formats_args`) stay green unchanged.

**Step 5 — version bump, changelog, release commit** (the v1.11.0 pattern,
commit `c5876da`):

- `pyproject.toml`: `version = "1.11.1"`
- `CHANGELOG.md`: new entry above `[1.11.0]`:

```markdown
## [1.11.1] - 2026-10-01

`/help` renders as an aligned two-column table: the command column
(`/name` plus its arg spec) is padded to the longest entry plus a
two-character gap, so descriptions line up. Purely presentational — same
commands, same metadata, plain text, no markup. Fixes the wall-of-text
output in both the CLI REPL and the daemon (and therefore pare-tui).

### Changed
- **`Help.run`** — command column width computed from the longest entry
  (long arg specs like `/scratch [clear | <text>]` size the column; no
  fixed width). Empty registry still yields just the header.
```

- Commit (footer per the convention above):

```bash
git add agent_core/commands/_builtin_impls.py tests/test_builtin_commands.py pyproject.toml CHANGELOG.md
git commit -m "chore: release 1.11.1 — /help renders as an aligned two-column table

Full changes documented in CHANGELOG.md's [1.11.1] entry.

The consumer-suite gate (PARE full test suite against this branch)
must pass before this version is tagged and released. See PARE's
docs/superpowers/plans/2026-10-01-tui-footer-help-and-labels.md Task 4
for the gate.
"
```

**Do NOT tag or push yet** — the consumer gate (Task 4) runs first, per
the v1.11.0 release-note rule.

---

### Task 4 — PARE: consumer gate + pin to v1.11.1

**Files:** modify `pyproject.toml` (line 11), `.venv`

**Context:** agent_core's release rule (from the v1.11.0 commit message):
the PARE full suite must pass against the new agent_core *before* the tag
is cut. The gate installs the new agent_core from the **local path**
(no tag exists yet), runs the whole PARE suite, then the pin is updated
to the future tag name.

**Step 1 — install the local agent_core into PARE's venv:**

```bash
cd /mnt/secondary/projects/PARE
uv pip install --python .venv/bin/python --reinstall /mnt/secondary/projects/agent_core
.venv/bin/python -c "from importlib.metadata import version; print(version('agent_core'))"
# expect: 1.11.1
```

**Step 2 — run the consumer gate (full PARE suite):**

```bash
.venv/bin/python -m pytest -q
```

Expected: full pass. Baseline before this work: **602 passed, 3 skipped**;
with this branch's new tests it will be higher (footer/F1/label tests).
If any failure appears, do not attribute it without re-running the known
flaky-under-load tests individually first:
`tests/test_project_slug_arcticbase.py` (live) and the wall-clock test in
`tests/test_tui_approval.py`.

**Step 3 — update the pin:**

`pyproject.toml` line 11:

```
    "agent_core @ git+https://github.com/EdibleTuber/agent_core.git@v1.11.1",
```

**Step 4 — commit:**

```bash
git add pyproject.toml
git commit -m "chore: pin agent_core v1.11.1 (/help table)

Consumer gate: full PARE suite green against the local v1.11.1
checkout. The git tag is cut in the agent_core repo immediately after
this gate (v1.11.0 release-note rule)."
```

Update the ledger.

---

### Task 5 — agent_core: tag, push, verify the pin resolves

**Files:** git only (agent_core) + PARE `.venv`

**Step 1 — tag and push (gate passed in Task 4):**

```bash
cd /mnt/secondary/projects/agent_core
git tag v1.11.1
git push origin main
git push origin v1.11.1
```

**Step 2 — verify the exact pin PARE declares now resolves:**

```bash
cd /mnt/secondary/projects/PARE
uv pip install --python .venv/bin/python \
  'agent_core @ git+https://github.com/EdibleTuber/agent_core.git@v1.11.1'
.venv/bin/python -c "from importlib.metadata import version; print(version('agent_core'))"
# expect: 1.11.1
```

**Step 3 — sanity suite slice** (the tag reinstall is byte-identical to
the gated local install; a slice, not a full re-run):

```bash
.venv/bin/python -m pytest tests/test_tui_operator_fixes.py tests/test_commands_metadata.py tests/test_handle_command.py -q
```

The point is a fast smoke that the TUI and the daemon command path import
and pass against the tag install.

---

### Task 6 — whole-branch verification, review, PR

**Step 1 — full verification of both repos:**

```bash
cd /mnt/secondary/projects/PARE && .venv/bin/python -m pytest -q          # full suite
cd /mnt/secondary/projects/agent_core && uv run pytest tests/ -q         # full suite
```

Both green.

**Step 2 — whole-branch review.** Use the `requesting-code-review` skill
against the diff `origin/main..HEAD` in PARE (and the agent_core release
commit). Review focus — the input classes / failure modes this work must
get right, each pinned by a test above:

1. **F1 with no live daemon connection** — must behave exactly like
   typing `/help`: `you> /help` echo, then `[send failed: ...]`, no crash
   out of the event handler. (`test_f1_with_no_live_connection_fails_like_typed_help`)
2. **Footer render fidelity** — the operator-visible strip, not just the
   class attribute: the last screen row contains "Quit" and "Help"
   (footer degrades/truncates at narrow widths; the 120x30 assertion is
   the operator-truth check). (`test_quit_and_help_are_in_the_footer`)
3. **Layout preset 4 (dock hidden)** — the UART label must vanish with
   the dock, no orphan strip; chat label unaffected.
   (`test_chat_and_uart_panes_carry_title_labels`)
4. **Long command arg specs** (`/scratch [clear | <text>]`) — the column
   width is computed, never a fixed literal that would clip.
   (`test_help_is_an_aligned_two_column_table`)
5. **CLI plain-text compatibility** — no markup/ANSI in the table; the
   pinned test's expected strings are exactly what the CLI prints.
   (same test as 4, plus `test_help_with_no_commands_shows_only_the_header`
   for the empty-registry guard.)

**Step 3 — push and open the PR:**

```bash
cd /mnt/secondary/projects/PARE
git push -u origin feat/tui-footer-help-labels
gh pr create --title "feat(tui): footer quit/help hints, pane labels; /help table (agent_core v1.11.1)" \
  --body "$(cat <<'EOF'
What the operator sees: the footer now reads `^l Layout  ^q Quit  f1 Help … ^p palette`
(quit and help at a glance; F1 is one-keystroke help, sent through the same path as
typing /help); each column is titled ("Chat" / "UART"); /help returns an aligned
two-column table instead of a flat wall of lines (agent_core v1.11.1, pinned).

- PARE: PareTUI.BINDINGS declares ctrl+q/f1 (the Textual 8.2.8 Footer renders only the
  app subclass's own bindings); _submit_line extraction shared by input-Enter and F1;
  .pane-label strips (PaneDock gains an optional title).
- agent_core: Help.run pads the command column; v1.11.1 tagged after the PARE
  consumer gate (full suite green against the local checkout).
- TDD throughout; footer/label render asserted via export_screenshot reconstruction.

Deferred to its own design + TDD cycle: drag-to-resize divider.
Note: if PR #78 merges first, this branch rebases cleanly (disjoint files/methods).
EOF
)"
```

CI green → **stop here.** Merging the PR, deploying to pare-bench, and
live verification are the operator's steps.

---

## Deferred (explicitly out of scope)

- **Drag-to-resize divider** — its own design + TDD cycle (Pilot has no
  `mouse_move`; the drag path is `mouse_down` → repeated `hover` →
  `mouse_up`).
- **PR #78 merge** — operator's call; independent of this branch.
- **power-cycle-and-baud deferred minors** (relay.py bare-OK parse,
  unguarded `port.close()`, untyped `_clamp_off_ms`, `_CONTRACT_MODULES`
  caveat) and the worker-side `power_cycle` inject-on-restore idea —
  carried from the previous plan's ledger.
