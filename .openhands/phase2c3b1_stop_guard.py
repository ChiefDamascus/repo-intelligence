"""Conservative project Stop command. Python standard library only."""
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
    'src/repo_intelligence/imported_name_analysis.py',
    'tests/test_imported_name_analysis.py',
)
MAX_SOURCE_BYTES = 1_048_576
MAX_EVENT_BYTES = 65_536
MAX_MARKERS = 4096
PREFIX = 'PHASE2C3B1_GUARD'


class CannotCheck(Exception):
    pass


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


def compatible_api(tree):
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != 'analyze_named_import':
            continue
        args = node.args
        positional = [a.arg for a in (*args.posonlyargs, *args.args)]
        # Accept the documented positional API, including optional extra args.
        # Do not infer behavior from annotations, decorators or function bodies.
        if positional[:3] != ['record', 'inventory', 'target_sources']:
            continue
        required_positional = len(positional) - len(args.defaults)
        required_keyword_only = any(value is None for value in args.kw_defaults)
        if required_positional <= 3 and not required_keyword_only:
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


def findings(root):
    problems = []
    trees = []
    for relative in AUTHORIZED:
        tree, problem = parse_file(root, relative)
        trees.append(tree)
        if problem:
            problems.append(problem)
    if trees[0] is not None and not compatible_api(trees[0]):
        problems.append('missing compatible analyze_named_import(record, inventory, target_sources)')
    if trees[1] is not None and not has_tests(trees[1]):
        problems.append('no discoverable test_* function in the authorized test file')
    return problems


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
    key = hashlib.sha256((str(root) + '\0' + session).encode()).hexdigest()
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
        # No automatic eviction: resumed sessions keep their consumed budget.
        # The lock serializes capacity checking and marker creation.
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
        # Persist the directory entry before issuing a denial on Linux.
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
        problems = findings(root)
        if not problems:
            return verdict('allow', 'basic file checks passed; acceptance tests and independent review remain required')
        if not consume_retry(root, state, session):
            return verdict('allow', 'retry budget consumed; unresolved: ' + '; '.join(problems))
        return verdict('deny', 'one continuation requested: ' + '; '.join(problems)
                       + '. Recheck only the two authorized files. Do not edit frozen files or expand scope. '
                       + 'If blocked, report the blocker. Report focused/regression results, F1-F6 dispositions, '
                       + 'changed files, final Git status and limitations; independent review remains required.')
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
