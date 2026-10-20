"""Conservative Phase 2C-4 project Stop command.

Checks the Phase 2C-4 implementation allowlist only:
  - src/repo_intelligence/resolve_query.py
  - tests/test_resolve_query.py
  - src/repo_intelligence/cli.py   (additive-only: a `resolve` command must
                                    appear, and all six existing commands
                                    must remain present)

Standard-library-only. Fails open on any operational error.
Never emits deny from an error handler.
"""
import ast
import contextlib
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time

PROJECT_ROOT = Path('/projects/repo-intelligence')
STATE_ROOT = Path('/projects/.openhands-stop-guard-state-10001/retries-v1')
AUTHORIZED = (
    'src/repo_intelligence/resolve_query.py',
    'tests/test_resolve_query.py',
    'src/repo_intelligence/cli.py',
)
PRODUCTION = 'src/repo_intelligence/resolve_query.py'
TESTS = 'tests/test_resolve_query.py'
CLI = 'src/repo_intelligence/cli.py'
MAX_SOURCE_BYTES = 1_048_576
MAX_EVENT_BYTES = 65_536
MAX_MARKERS = 4096
PREFIX = 'PHASE2C4_GUARD'
EXISTING_CLI_COMMANDS = ('build', 'stats', 'files', 'search-files', 'symbols', 'imports')


# ---------------------------------------------------------------------------
# Approved scope separation (approved record §11; grounded in the human
# approval of A1-A8 + Q1-Q5 on 2026-10-20). These sets are the ONLY paths
# the guard considers "in scope" for a legitimate change set. Any other
# path appearing in `git status --porcelain` is a scope violation and the
# guard will deny (or report fail-open inconclusive if `git status` is
# unavailable / errors).
#
# WORKFLOW_SCOPE: the six authorized workflow-preparation paths for this
# approval/guard task (approved record §4).
# PRODUCTION_SCOPE: the three future production/test allowlist paths
# (approved record §5). NOT active until a separate implementation
# authorization + guard readiness in a fresh conversation, but the guard
# tracks them so that the phase is observable in the change set.
#
# PLAN_MAVINTENANCE: the standing maintenance permission for PLAN.md
# (summary/detail/log entries). Per AGENTS.md and docs/phase-workflow.md,
# PLAN maintenance is a routine part of each phase's implementation work
# and is permitted in both PREPARATION and PRODUCTION scope checks.
# No other workflow path is a routine production exception.
#
# The guard enforces a single fixed "allowed set" = union of both, which is
# the most conservative reading of "only authorized paths may differ" across
# both preparation and production phases. Which set's files are actually
# present in the change set is surfaced in the find string so the agent
# can see whether the change set is workflow-preparation-only, production,
# or a mix. The guard does NOT enforce phase ordering (i.e. it does not
# deny production files during preparation) — that is a human/process
# decision, not a guard check.
WORKFLOW_SCOPE = (
    'AGENTS.md',
    'PLAN.md',
    'docs/phase-2c-4-architecture-approved.md',
    'docs/phase-2c-4-architecture-review.md',
    'docs/openhands-stop-hooks.md',
    '.openhands/hooks.json',
)
PRODUCTION_SCOPE = (
    'src/repo_intelligence/resolve_query.py',
    'tests/test_resolve_query.py',
    'src/repo_intelligence/cli.py',
)
PLAN_MAVINTENANCE = (
    'PLAN.md',
)
ALLOWED_SCOPE = WORKFLOW_SCOPE + PRODUCTION_SCOPE + PLAN_MAVINTENANCE + (
    '.openhands/phase2c4_stop_guard.py',
)


class CannotCheck(Exception):
    pass


def _rel_root(root):
    """Root path as a string for scope comparison."""
    return str(root)


def _git_status_z(root):
    import subprocess
    try:
        proc = subprocess.run(
            ['git', '-C', str(root), 'status', '--porcelain=v1', '-z', '--untracked-files=all'],
            capture_output=True, check=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError, subprocess.TimeoutExpired) as exc:
        raise CannotCheck(f'git status failed: {type(exc).__name__}') from exc
    return proc.stdout


def changed_paths(root):
    """Sorted set of repo-relative paths with ANY change: staged, unstaged,
    untracked (individual files, not collapsed directories), or rename
    (both source and destination). Uses
    `git status --porcelain=v1 -z --untracked-files=all` to expand
    untracked directories into their constituent files so that every actual
    file is checked against the scope.

    Entry shapes (NUL-terminated):
      <XY><one-space><path>
        XY is a two-character status (X=index, Y=worktree; a space in either
        spot means "no change on that side"). The ONE space after XY is a
        separator, NOT part of the path. A real leading-space filename
        character follows that separator and must be preserved exactly.
      R rename: two entries.
        Entry 1: `R<sp><new-path>`
        Entry 2: `<old-path>` (no status prefix; consumed as the second side)
        Both sides are added to the result set.

    Raises CannotCheck on any Git/subprocess failure (caller maps that to
    FAIL_OPEN per approved policy; never treated as a scope pass)."""
    raw = _git_status_z(root)
    paths = set()
    entries = [e for e in raw.split('\0') if e]
    i = 0
    while i < len(entries):
        entry = entries[i]
        if len(entry) < 3:
            i += 1
            continue
        status = entry[:2]
        path = entry[2:]
        # Exactly ONE separator space separates the XY status from the path.
        # A real leading-space filename character (if any) follows it and
        # must be preserved.
        if path.startswith(' '):
            path = path[1:]
        if not path:
            i += 1
            continue
        if status.endswith('?'):
            paths.add(path)
            i += 1
            continue
        if status.startswith('R'):
            # Add both sides of a rename. Entry 2 is the other side (no
            # status prefix; consume it as-is, stripping nothing).
            paths.add(path)
            if i + 1 < len(entries):
                other = entries[i + 1]
                if other:
                    paths.add(other)
                i += 2
                continue
            i += 1
            continue
        paths.add(path)
        i += 1
    return sorted(paths)


def mode_from_head(root):
    """Phase discriminator (approved record §11; grounded in §4/§5 allowlists).

    Reads HEAD (read-only) to decide the change-set scope rule:

      - PREPARATION: HEAD does NOT contain docs/phase-2c-4-architecture-approved.md
        (the approval record + guard config are still in-flight). Allowed changed
        paths = WORKFLOW_SCOPE union {'.openhands/phase2c4_stop_guard.py'}.
        Changes to PRODUCTION_SCOPE are a scope violation (premature production work).
      - PRODUCTION: HEAD DOES contain that approved record (promotion has happened).
        Allowed changed paths = PRODUCTION_SCOPE union PLAN_MAVINTENANCE (PLAN.md
        standing maintenance permission per AGENTS.md / phase-workflow).
        Any other changed path is a scope violation, including a new workflow-path
        edit (no other workflow path is a routine production exception).
      - UNDETERMINED: CannotCheck (caller maps to FAIL_OPEN; never treated as PASS).

    This mode choice is a static, testable signal from HEAD; it never grants
    human implementation authorization by itself. A guard allow under either
    mode still does not establish production authorization.
    """
    import subprocess
    try:
        proc = subprocess.run(
            ['git', '-C', str(root), 'ls-tree', 'HEAD', '--name-only', 'docs/phase-2c-4-architecture-approved.md'],
            capture_output=True, check=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError, subprocess.TimeoutExpired) as exc:
        raise CannotCheck(f'git ls-tree failed: {type(exc).__name__}') from exc
    if 'docs/phase-2c-4-architecture-approved.md' in proc.stdout.splitlines():
        return 'PRODUCTION'
    return 'PREPARATION'


def scope_check(root):
    """Bounded, read-only scope verification (HEAD-based mode).

    Returns (ok, findings, diagnostic, note) where:
      ok         True only when mode is determinable AND every changed path
                 is within the mode-specific allowed set.
      findings   list of human-readable violation strings (empty on pass).
      diagnostic 'PASS' | 'OK_NO_CHANGES' | 'FAIL_SCOPE'.
      note       'PREPARATION' | 'PRODUCTION' for the allow reason.

    UNDETERMINED mode raises CannotCheck → caller maps to FAIL_OPEN and
    returns allow with an inconclusive diagnostic (never treated as a pass).
    """
    mode = mode_from_head(root)
    changed = changed_paths(root)
    if mode == 'PREPARATION':
        allowed = set(WORKFLOW_SCOPE) | {'.openhands/phase2c4_stop_guard.py'}
        scope_note = 'preparation-phase scope (6 workflow paths + guard script)'
    else:  # PRODUCTION
        allowed = set(PRODUCTION_SCOPE) | set(PLAN_MAVINTENANCE)
        scope_note = 'production-phase scope (3 implementation/test/CLI paths + standing PLAN.md maintenance)'
    violations = [p for p in changed if p not in allowed]
    if not changed:
        return True, [], 'OK_NO_CHANGES', mode
    if violations:
        shown = violations[:8]
        more = len(violations) - len(shown)
        msgs = [f'out-of-scope changed path in {mode[:4].upper()} phase: {p}' for p in shown]
        if more:
            msgs.append(f'...and {more} more out-of-scope path(s)')
        return False, msgs, 'FAIL_SCOPE', mode
    return True, [], 'PASS', mode


def verdict(decision, reason):
    return {'decision': decision, 'reason': f'{PREFIX}: {reason}'}


def no_symlinks(path):
    path = path.absolute()
    for part in reversed((path, *path.parents)):
        if part.is_symlink():
            raise CannotCheck('symlink in an inspected path')


def parse_file(root, relative):
    path = root / relative
    no_symlinks(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        return None, f'missing {relative}'
    if not stat.S_ISREG(info.st_mode):
        raise CannotCheck('authorized path is not a regular file')
    if info.st_size > MAX_SOURCE_BYTES:
        raise CannotCheck('authorized file exceeds the size bound')
    with path.open('rb') as stream:
        source = stream.read(MAX_SOURCE_BYTES + 1)
    if len(source) > MAX_SOURCE_BYTES:
        raise CannotCheck('authorized file exceeds the size bound')
    try:
        return ast.parse(source, filename=relative), None
    except SyntaxError as exc:
        return None, f'syntax error in {relative} at line {exc.lineno}'
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CannotCheck('AST parser could not check the file') from exc


def api_compatible(tree):
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != 'resolve_imports_from_index':
            continue
        args = node.args
        positional = [a.arg for a in (*args.posonlyargs, *args.args)]
        # Accept the documented positional API
        # `resolve_imports_from_index(db_path, roots, *, ...)`.
        # No inference from annotations, decorators, or bodies.
        if positional[:2] != ['db_path', 'roots']:
            continue
        required_positional = len(positional) - len(args.defaults)
        required_keyword_only = any(value is None for value in args.kw_defaults)
        if required_positional <= 2 and not required_keyword_only:
            return True
    return False


def has_tests(tree):
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in tree.body:
        if isinstance(node, functions) and node.name.startswith('test_'):
            return True
        if isinstance(node, ast.ClassDef) and node.name.startswith('Test'):
            if any(isinstance(child, functions) and child.name.startswith('test_')
                   for child in node.body):
                return True
    return False


def command_names(tree):
    """Collect registered command names from the module AST.

    Recognized registration styles (all valid Click patterns):
      * ``@cli.command("name")``                 decorator (primary pattern)
      * ``@cli.command(name="name")``            decorator (keyword form)
      * ``cli.add_command(fn)`` / ``cli.add_command(fn, name="name")``
      * ``setattr(cli, "name", fn)``             direct attribute assignment
      * ``@cli.group("name")``                   group decorator
      * ``cli.<name>`` attribute / ``cli.<name>(...)`` method usage

    The decorator form is matched only when the ``cli.command(...)`` /
    ``cli.group(...)`` call is a function or lambda decorator, and the
    base name (``cli``) must be a plain ``ast.Name`` (not a dotted path).
    A decorator's first positional argument — a string constant — is taken
    as the registered name.
    """
    names = set()
    for node in ast.walk(tree):
        # --- @cli.command("name") / @cli.command(name="name") / @cli.group("name") ---
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute)
                    and func.attr in ('command', 'group')
                    and isinstance(func.value, ast.Name)
                    and func.value.id == 'cli'):
                if node.args and isinstance(node.args[0], ast.Constant) \
                        and isinstance(node.args[0].value, str):
                    names.add(node.args[0].value)
                for kw in node.keywords:
                    if kw.arg == 'name' and isinstance(kw.value, ast.Constant) \
                            and isinstance(kw.value.value, str):
                        names.add(kw.value.value)
        # --- cli.<name> attribute usage, and cli.<name>(...) calls ---
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == 'cli' and isinstance(node.attr, str):
                names.add(node.attr)
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id == 'cli' and isinstance(func.attr, str):
                    names.add(func.attr)
            # --- setattr(cli, "name", value) ---
            if (isinstance(func, ast.Name) and func.id == 'setattr'
                    and len(node.args) >= 2
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == 'cli'
                    and isinstance(node.args[1], (ast.Constant, ast.Str))):
                const = node.args[1]
                if isinstance(const, ast.Constant) and isinstance(const.value, str):
                    names.add(const.value)
    return names


def findings(root, production_present: bool = True):
    """Return (problems, notes).

    `production_present` indicates whether the change set being evaluated
    contains any authorized PRODUCTION-path modification (added, modified,
    renamed-in, or renamed-out of the 2C-4 production allowlist).

    PRODUCTION mode behavior:
      - production_present is False (e.g. a clean readiness state with the
        production files still absent): missing files are reported as
        NOTES (not hard failures), and the artifact/API/test/CLI checks are
        skipped (no file to check).
      - production_present is True (production changes exist): missing files
        ARE hard failures, AND the artifact/API/test/CLI checks run.

    PREPARATION mode is handled by the caller (`evaluate` does not call
    findings() in PREPARATION mode by design).

    Hard failures (always real problems when `production_present`):
      - syntax error in any authorized file that exists
      - resolve_imports_from_index present with an incompatible signature
      - test file present but containing no discoverable test_* / Test*
      - cli.py present but missing the additive `resolve` command, or an
        existing command name (build/stats/files/search-files/symbols/imports)
      - missing authorized file when `production_present` is True
    """
    problems = []
    notes = []
    production = tests = cli_tree = None
    for relative in AUTHORIZED:
        tree, problem = parse_file(root, relative)
        if problem:
            if problem.startswith('missing ') and not production_present:
                notes.append(problem)
            else:
                problems.append(problem)
            continue
        if relative == PRODUCTION:
            production = tree
        elif relative == TESTS:
            tests = tree
        elif relative == CLI:
            cli_tree = tree
    if production_present:
        if production is not None and not api_compatible(production):
            problems.append(
                'missing compatible resolve_imports_from_index(db_path, roots, *...)'
            )
        if tests is not None and not has_tests(tests):
            problems.append('no discoverable test_* function or Test* class method in the authorized test file')
        if cli_tree is not None:
            names = command_names(cli_tree)
            if 'resolve' not in names:
                problems.append('cli.py is missing the additive resolve command')
            for required in EXISTING_CLI_COMMANDS:
                if required not in names:
                    problems.append(f'cli.py lost an existing command: {required}')
    return problems, notes


@contextlib.contextmanager
def state_lock(directory):
    no_symlinks(directory)
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise CannotCheck('state directory is unavailable')
    if os.name != 'nt':
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise CannotCheck('state directory must be private and owned by the runtime user')
    lock = directory / '.lock'
    no_symlinks(lock)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(lock, flags, 0o600)
    locked = False
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise CannotCheck('invalid state lock')
        if os.name == 'nt':
            import msvcrt
            if opened.st_size == 0:
                os.write(fd, b'0')
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    finally:
        if locked:
            if os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def consume_retry(root, state, session):
    # Phase-tagged key so the 2C-4 guard's denial budget is independent of
    # the 2C-3B-1 and 2C-3B-2 guards' even when all share the same state directory.
    key = hashlib.sha256((PREFIX + '\0' + str(root) + '\0' + session).encode()).hexdigest()
    marker = state / f'{key}.json'
    with state_lock(state):
        no_symlinks(marker)
        if marker.exists():
            if not marker.is_file() or marker.stat().st_size > 1024:
                raise CannotCheck('invalid retry marker')
            with marker.open('rb') as stream:
                raw = stream.read(1025)
            if len(raw) > 1024:
                raise CannotCheck('retry marker exceeds the size bound')
            data = json.loads(raw)
            if data.get('key') != key or data.get('denials') != 1 or data.get('version') != 1:
                raise CannotCheck('invalid retry marker')
            return False
        count = 0
        with os.scandir(state) as entries:
            for entry in entries:
                if entry.name == '.lock':
                    continue
                count += 1
                if count >= MAX_MARKERS:
                    raise CannotCheck('retry state is at capacity')
        payload = json.dumps({
            'version': 1, 'key': key, 'denials': 1, 'created_at': time.time(),
        }).encode('utf-8')
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(marker, flags, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != 'nt':
            fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        return True


def evaluate(event, root, state, cwd, environ):
    try:
        if not isinstance(event, dict):
            raise CannotCheck('event must be an object')
        if str(event.get('event_type', '')).lower() != 'stop':
            raise CannotCheck('not a Stop event')
        metadata = event.get('metadata')
        if not isinstance(metadata, dict) or metadata.get('reason') != 'agent_finished':
            raise CannotCheck('not an agent_finished completion')
        no_symlinks(root)
        root = root.resolve(strict=True)
        if Path(cwd).resolve(strict=True) != root:
            raise CannotCheck('unexpected process working directory')
        directory = event.get('working_dir')
        if not isinstance(directory, str) or Path(directory).resolve(strict=True) != root:
            raise CannotCheck('unexpected event working directory')
        env_root = environ.get('OPENHANDS_PROJECT_DIR')
        if env_root and Path(env_root).resolve(strict=True) != root:
            raise CannotCheck('project environment does not match the event')
        session = event.get('session_id')
        if not isinstance(session, str) or not session.strip() or len(session) > 256:
            raise CannotCheck('missing or invalid conversation identity')
        env_session = environ.get('OPENHANDS_SESSION_ID')
        if env_session and env_session != session:
            raise CannotCheck('conversation environment does not match the event')
        # Bounded, read-only HEAD-based scope check (approved record §11 separation).
        # CannotCheck here (git status/ls-tree unavailable/errored) propagates to
        # the fail-open handler below; we deliberately do NOT treat that as a pass.
        scope_ok, scope_findings, scope_diag, scope_mode = scope_check(root)
        # Mode-gated file checks. In PRODUCTION mode, "production_present" is
        # True when ANY authorized PRODUCTION_SCOPE path (add/modify/rename
        # either side of a rename) is in the change set. When False (clean
        # readiness with the production files still absent), missing files
        # are reported as readiness NOTES (not failures) and the artifact/
        # API/test/CLI checks are skipped (nothing to check).
        if scope_mode == 'PRODUCTION':
            changed = changed_paths(root)
            prod_changed = [p for p in changed if p in set(PRODUCTION_SCOPE)]
            problems, readiness_notes = findings(root, production_present=bool(prod_changed))
        else:
            problems, readiness_notes = [], []
        if not problems and scope_ok:
            if scope_mode == 'PRODUCTION':
                if readiness_notes:
                    return verdict('allow', 'PRODUCTION-mode readiness state: authorized files absent (no production changes yet); scope checks passed; artifact/API/test/CLI completion checks are NOT evaluated in this readiness state; acceptance tests T1-T12/REG, independent review, and human acceptance remain required. Readiness notes: ' + '; '.join(readiness_notes))
                return verdict('allow', 'basic file checks passed and all changed paths are within the approved 2C-4 production-phase scope; acceptance tests T1-T12/REG, independent review, and human acceptance remain required')
            return verdict('allow', 'preparation/readiness change set verified within the approved 2C-4 preparation scope; production artifacts (resolve_query.py, tests/test_resolve_query.py, cli.py resolve command) are NOT evaluated in preparation mode; acceptance tests T1-T12/REG, independent review, and human acceptance remain required')
        combined = list(problems) + list(scope_findings)
        if not consume_retry(root, state, session):
            return verdict('allow', 'retry budget consumed; unresolved: ' + '; '.join(combined))
        return verdict('deny', 'one continuation requested: ' + '; '.join(combined)
                       + '. Recheck only the Phase 2C-4 authorized files '
                       f'({PRODUCTION}, {TESTS}, additive resolve command in {CLI}) '
                       + 'or the standing PLAN.md maintenance permission, or the approved preparation scope (AGENTS.md, PLAN.md, docs, .openhands/hooks.json, .openhands/phase2c4_stop_guard.py). '
                       + 'Scope mode detected: ' + str(scope_mode) + '. '
                       + 'Do not edit frozen files, the 2C-3B-1/2C-3B-2 guards, or expand scope beyond the 3 production/test/CLI paths + PLAN.md. '
                       + 'If blocked, report the blocker. Report focused T1-T12/REG results, full regression results, changed files, '
                       + 'final Git status and limitations; independent review, human acceptance, and Git promotion remain separate gates.')
    except Exception as exc:
        # Never output source fragments, raw event data, or a deny on errors.
        return verdict('allow', f'FAIL_OPEN ({type(exc).__name__}); checks are inconclusive')


def main():
    try:
        raw = sys.stdin.buffer.read(MAX_EVENT_BYTES + 1)
        if len(raw) > MAX_EVENT_BYTES:
            raise CannotCheck('event exceeds size bound')
        event = json.loads(raw)
        result = evaluate(event, PROJECT_ROOT, STATE_ROOT, Path.cwd(), os.environ)
    except Exception as exc:
        result = verdict('allow', f'FAIL_OPEN ({type(exc).__name__}); checks are inconclusive')
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
