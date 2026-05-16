#!/usr/bin/env bash
# init-agent.sh — scaffold a working agent from the agent_template.
#
# Usage:
#   ./scripts/init-agent.sh <agent-name> [--no-git]
#
# <agent-name> must be a lowercase slug: letters, digits, hyphens.
#              Must start with a letter. Examples: re-lab, coding, my-agent.
#
# The script derives:
#   AGENT_NAME        = <agent-name> as-is          (e.g. re-lab)
#   agent_pkg         = hyphens replaced by _        (e.g. re_lab)
#   AGENT_CLASS       = each token title-cased + "Agent" suffix
#                                                    (e.g. re-lab -> ReLabAgent)
#   AGENT_PREFIX      = uppercase, hyphens stripped  (e.g. re-lab -> RELAB)
#
# The script then prompts for AGENT_DESCRIPTION, renames the package
# directory + systemd service files, substitutes all placeholders, strips the
# README "Before Init" block, and removes itself.

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

die() { echo "ERROR: $*" >&2; exit 1; }

# Find the repo root (the directory containing this script's parent).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------

AGENT_NAME=""
RUN_GIT=true

for arg in "$@"; do
    case "$arg" in
        --no-git) RUN_GIT=false ;;
        -*) die "Unknown flag: $arg" ;;
        *)
            if [[ -z "$AGENT_NAME" ]]; then
                AGENT_NAME="$arg"
            else
                die "Unexpected argument: $arg"
            fi
            ;;
    esac
done

[[ -n "$AGENT_NAME" ]] || die "Usage: $0 <agent-name> [--no-git]"

# ---------------------------------------------------------------------------
# Validate the name
# ---------------------------------------------------------------------------

if ! [[ "$AGENT_NAME" =~ ^[a-z][a-z0-9-]*$ ]]; then
    die "Invalid agent name '${AGENT_NAME}'. Must match ^[a-z][a-z0-9-]*$ (lowercase, digits, hyphens; start with a letter)."
fi

# ---------------------------------------------------------------------------
# Idempotency guard: if placeholders are already replaced, bail out.
# ---------------------------------------------------------------------------

if ! grep -qF '{{AGENT_NAME}}' "${REPO_ROOT}/pyproject.toml" 2>/dev/null; then
    echo "Placeholders already replaced — init has already run. Nothing to do."
    exit 0
fi

# ---------------------------------------------------------------------------
# Derive placeholder values
# ---------------------------------------------------------------------------

# agent_pkg: hyphens to underscores
AGENT_PKG="${AGENT_NAME//-/_}"

# AGENT_CLASS: split on '-', title-case each token, join, append "Agent"
# Deduplication: if the joined form already ends in "Agent" (e.g. "test-agent"
# -> "TestAgent"), do not append a second "Agent" suffix.
AGENT_CLASS=""
IFS='-' read -ra TOKENS <<< "$AGENT_NAME"
for token in "${TOKENS[@]}"; do
    # title-case: uppercase first char, rest unchanged (lowercase slug, so all lowercase)
    AGENT_CLASS="${AGENT_CLASS}${token^}"
done
if [[ "${AGENT_CLASS}" != *Agent ]]; then
    AGENT_CLASS="${AGENT_CLASS}Agent"
fi

# AGENT_PREFIX: uppercase, hyphens stripped
AGENT_PREFIX="${AGENT_NAME//-/}"
AGENT_PREFIX="${AGENT_PREFIX^^}"

# ---------------------------------------------------------------------------
# Prompt for description
# ---------------------------------------------------------------------------

if [[ -t 0 ]]; then
    # Interactive: prompt the user.
    read -rp "Agent description (one line): " AGENT_DESCRIPTION
else
    # Non-interactive (piped / heredoc): read from stdin.
    read -r AGENT_DESCRIPTION
fi

[[ -n "$AGENT_DESCRIPTION" ]] || die "Agent description cannot be empty."

echo ""
echo "Scaffolding '${AGENT_NAME}'..."
echo "  Package dir : ${AGENT_PKG}/"
echo "  Class name  : ${AGENT_CLASS}"
echo "  Env prefix  : ${AGENT_PREFIX}_"
echo "  Description : ${AGENT_DESCRIPTION}"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Rename {{agent_pkg}}/ -> <agent_pkg>/
# ---------------------------------------------------------------------------

cd "${REPO_ROOT}"

if [[ -d "{{agent_pkg}}" ]]; then
    mv "{{agent_pkg}}" "${AGENT_PKG}"
    echo "Renamed {{agent_pkg}}/ -> ${AGENT_PKG}/"
elif [[ -d "${AGENT_PKG}" ]]; then
    echo "Package directory '${AGENT_PKG}/' already exists, skipping rename."
else
    die "Neither '{{agent_pkg}}/' nor '${AGENT_PKG}/' found in repo root."
fi

# ---------------------------------------------------------------------------
# Step 2: Rename systemd service files
# ---------------------------------------------------------------------------

if [[ -f "systemd/{{agent_name}}-daemon.service" ]]; then
    mv "systemd/{{agent_name}}-daemon.service" "systemd/${AGENT_NAME}-daemon.service"
    echo "Renamed systemd/{{agent_name}}-daemon.service -> systemd/${AGENT_NAME}-daemon.service"
fi

# ---------------------------------------------------------------------------
# Step 3: Substitute placeholders in all relevant files
# ---------------------------------------------------------------------------

# We do AGENT_DESCRIPTION first because it may contain characters that clash
# with sed delimiters. Use | as the delimiter and escape any | in the value.
# We also need to escape & and \ which are special in sed replacement strings.
_sed_escape_replacement() {
    # Escape characters that are special in sed replacement: \ & and the delimiter |
    printf '%s' "$1" | sed 's/[\\&|]/\\&/g'
}

ESCAPED_DESC="$(_sed_escape_replacement "${AGENT_DESCRIPTION}")"
ESCAPED_CLASS="$(_sed_escape_replacement "${AGENT_CLASS}")"
ESCAPED_PREFIX="$(_sed_escape_replacement "${AGENT_PREFIX}")"
ESCAPED_NAME="$(_sed_escape_replacement "${AGENT_NAME}")"
ESCAPED_PKG="$(_sed_escape_replacement "${AGENT_PKG}")"

echo "Substituting placeholders..."

# Find all relevant files (excluding .git)
mapfile -t FILES < <(
    find . -type f \(    \
        -name "*.py"     \
        -o -name "*.md"  \
        -o -name "*.toml"\
        -o -name "*.service" \
        -o -name "*.example" \
        -o -name "*.sh"  \
    \) ! -path "./.git/*" 2>/dev/null
)

for f in "${FILES[@]}"; do
    sed -i \
        -e "s|{{AGENT_DESCRIPTION}}|${ESCAPED_DESC}|g" \
        -e "s|{{AGENT_CLASS}}|${ESCAPED_CLASS}|g" \
        -e "s|{{AGENT_PREFIX}}|${ESCAPED_PREFIX}|g" \
        -e "s|{{AGENT_NAME}}|${ESCAPED_NAME}|g" \
        -e "s|{{agent_pkg}}|${ESCAPED_PKG}|g" \
        -e "s|{{agent_name}}|${ESCAPED_NAME}|g" \
        "$f"
done

echo "Placeholders replaced in ${#FILES[@]} files."

# ---------------------------------------------------------------------------
# Step 4: Strip the "Before Init" section from README.md
# ---------------------------------------------------------------------------

if [[ -f "README.md" ]]; then
    # Remove lines from <!-- BEFORE INIT --> through <!-- END BEFORE INIT -->
    # inclusive, plus the blank line that immediately follows.
    sed -i '/<!-- BEFORE INIT -->/,/<!-- END BEFORE INIT -->/d' README.md
    # Clean up any leading blank lines left at the top of the file.
    sed -i '/./,$!d' README.md
    echo "Stripped 'Before Init' block from README.md."
fi

# ---------------------------------------------------------------------------
# Step 5: Optionally reinitialise git
# ---------------------------------------------------------------------------

if [[ "$RUN_GIT" == "true" ]]; then
    if [[ ! -d ".git" ]]; then
        git init -b main
        git add --all
        git commit -m "feat: scaffold ${AGENT_NAME} from agent_template"
        echo "Initialised new git repo and made initial commit."
    else
        echo "Existing .git directory found — skipping git init (pass --no-git to suppress this message)."
    fi
fi

# ---------------------------------------------------------------------------
# Step 6: Print next steps
# ---------------------------------------------------------------------------

echo ""
echo "Done. Next steps:"
echo ""
echo "  1. Create a virtual environment:"
echo "       python -m venv .venv"
echo "       .venv/bin/pip install --upgrade pip"
echo "       .venv/bin/pip install -e '.[dev]'"
echo ""
echo "  2. Configure:"
echo "       cp .env.example .env"
echo "       \$EDITOR .env   # set ${AGENT_PREFIX}_INFERENCE_URL, ${AGENT_PREFIX}_VAULT_PATH, etc."
echo ""
echo "  3. Run the smoke test:"
echo "       .venv/bin/pytest tests/ -v"
echo ""
echo "  4. Start the daemon:"
echo "       .venv/bin/python -m ${AGENT_PKG}"
echo ""
echo "  5. (Optional) Install systemd service:"
echo "       cp systemd/${AGENT_NAME}-daemon.service ~/.config/systemd/user/"
echo "       systemctl --user daemon-reload"
echo "       systemctl --user enable --now ${AGENT_NAME}-daemon"
echo ""

# ---------------------------------------------------------------------------
# Step 7: Remove this script (last action so any failure above leaves it intact)
# ---------------------------------------------------------------------------

rm -- "$0"
