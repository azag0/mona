# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import os
import importlib
import shutil
import json
import sys
import signal
import argparse
import subprocess as sp
from configparser import ConfigParser
from pathlib import Path
from typing import Dict, List, Optional, Any, Set, Iterable
import asyncio

from .argparse_cli import CLI, CLIError, partial
from .app import Caf, CAFDIR, read_config
from .Utils import get_timestamp, config_group
from . import Logging
from .Logging import error, info, Table, colstr, no_cafdir, handle_broken_pipe
from .Remote import Remote, Local
from .Utils import config_group, groupby
from .argparse_cli import Arg, define_cli, CLIError, ThrowingArgumentParser
from .cellar import Cellar, Hash, TPath, State
from .scheduler import RemoteScheduler, Scheduler
from .Announcer import Announcer


class NoAppFoundError(Exception):
    pass


class RemoteNotExists(Exception):
    pass


class CommandContext:
    def __init__(self) -> None:
        self.config = read_config(CAFDIR)
        self.out = Path('build')
        self._remotes = {
            name: Remote(r['host'], r['path'])
            for name, r in config_group(self.config, 'remote')
        }
        self._remotes['local'] = Local()
        self.__app_module: Any = None

    @property
    def _app_module(self) -> Any:
        if not self.__app_module:
            app_module_path = os.environ.get('CAF_APP')
            if not app_module_path:
                if Path('app.py').is_file():
                    app_module_path = 'app'
                    sys.path.append('')
                else:
                    raise NoAppFoundError()
            self.__app_module = importlib.import_module(app_module_path)
        return self.__app_module

    @property
    def app(self) -> Caf:
        return self._app_module.app  # type: ignore

    @property
    def cellar(self) -> Cellar:
        return self._app_module.cellar  # type: ignore

    def parse_remotes(self, remotes: str) -> List[Remote]:
        if remotes == 'all':
            return [r for r in self._remotes.values() if not isinstance(r, Local)]
        try:
            return [self._remotes[r] for r in remotes.split(',')]
        except KeyError:
            pass
        raise RemoteNotExists(remotes)

    def mod_remote_args(self, args: List[str], kwargs: Dict[str, Any]) -> None:
        if '--last' in args:
            args.remove('--last')
            args = args + ['--queue', self.last_queue]
        if args[0] == 'make' and 'url' in kwargs:
            url = kwargs['url']
            args[args.index(url)] = self.get_queue_url(url)

    @property
    def last_queue(self) -> str:
        try:
            return (CAFDIR/'LAST_QUEUE').read_text()
        except FileNotFoundError:
            error('No queue was ever submitted, cannot use --last')

    @last_queue.setter
    def last_queue(self, queue: str) -> None:
        (CAFDIR/'LAST_QUEUE').write_text(queue)

    def get_queue_url(self, queue: str) -> str:
        qid: Optional[str]
        if ':' in queue:
            name, qid = queue.rsplit(':', 1)
        else:
            name, qid = queue, None
        section = f'queue "{name}"'
        if not self.config.has_section(section):
            return queue
        conf = self.config[section]
        url = f'{conf["host"]}/token/{conf["token"]}'
        if qid:
            url += f'/queue/{qid}'
        return url

    def log(self, args: List[str]) -> None:
        if CAFDIR.exists():
            with (CAFDIR/'log').open('a') as f:
                f.write(f'{get_timestamp()}: {" ".join(args)}\n')


def main() -> None:
    args = sys.argv[1:]
    ctx = CommandContext()
    cli = CLI([
        ('init', partial(init, ctx)),
        ('conf', partial(configure, ctx)),
        ('run', partial(run, ctx)),
        ('make', partial(make, ctx)),
        ('dispatch', partial(dispatch, ctx)),
        ('checkout', partial(checkout, ctx)),
        ('submit', partial(submit, ctx)),
        ('reset', partial(reset, ctx)),
        ('list', [
            ('profiles', partial(list_profiles, ctx)),
            ('remotes', partial(list_remotes, ctx)),
            ('builds', partial(list_builds, ctx)),
            ('tasks', partial(list_tasks, ctx)),
        ]),
        ('status', partial(status, ctx)),
        ('gc', partial(gc, ctx)),
        ('cmd', partial(cmd, ctx)),
        ('remote', [
            ('add', partial(remote_add, ctx)),
            ('path', partial(remote_path, ctx)),
            ('list', partial(list_remotes, ctx)),
        ]),
        ('update', partial(update, ctx)),
        ('check', partial(check, ctx)),
        ('fetch', partial(fetch, ctx)),
        ('archive', [
            ('save', partial(archive_store, ctx)),
        ]),
        ('go', partial(go, ctx)),
    ])
    if not args:
        cli.parser.print_help()
        error()
    try:
        cli.run(argv=args)
    except CLIError as e:
        clierror = e
    else:
        ctx.log(args)
        return

    remote_spec, *rargs = args
    try:
        remotes: Optional[List[Remote]] = ctx.parse_remotes(remote_spec)
    except RemoteNotExists:
        remotes = None
    if not rargs:
        if remotes is None:
            clierror.reraise()
        return
    try:
        kwargs = cli.parse(rargs)
    except CLIError as rclierror:
        if remotes is None:
            clierror.reraise()
        rclierror.reraise()
    if remotes is None:
        error(f'Remote {remote_spec!r} is not defined')

    ctx.mod_remote_args(rargs, kwargs)
    ctx.log(args)
    if rargs[0] in ['conf', 'make']:
        for remote in remotes:
            remote.update(CAFDIR.parent)
    if rargs[0] == 'make':
        check(ctx, remote_spec)
    for remote in remotes:
        remote.command(rargs)


def sig_handler(sig: Any, frame: Any) -> Any:
    print(f'Received signal {signal.Signals(sig).name}')
    raise KeyboardInterrupt


@define_cli()
def init(ctx: CommandContext) -> None:
    if not CAFDIR.is_dir():
        CAFDIR.mkdir()
        info(f'Initializing an empty repository in {CAFDIR.resolve()}.')
        if ctx.config.has_option('core', 'cache'):
            ts = get_timestamp()
            path = Path(ctx.config['core']['cache'])/f'{Path.cwd().name}_{ts}'
            path.mkdir()
            (CAFDIR/'objects').symlink_to(path)
        else:
            (CAFDIR/'objects').mkdir()


@define_cli([
    Arg('routes', metavar='ROUTE', nargs='*', help='Route to schedule'),
])
def configure(ctx: CommandContext, routes: List[str] = None) -> Any:
    Scheduler(ctx.cellar)
    if not routes:
        routes = list(ctx.app._routes.keys())
    with ctx.app.context(readonly=False):
        return ctx.app.get(*routes)


@define_cli([
    Arg('routes', metavar='ROUTE', nargs='*', help='Route to run'),
])
def run(ctx: CommandContext, routes: List[str] = None) -> Any:
    if not routes:
        routes = list(ctx.app._routes.keys())
    with ctx.app.context(execution=True, readonly=False):
        return ctx.app.get(*routes)


@define_cli([
    Arg('patterns', metavar='PATTERN', nargs='*', help='Tasks to be built'),
    Arg('-l', '--limit', type=int, help='Limit number of tasks to N'),
    Arg('-q', '--queue', dest='url', help='Take tasks from web queue'),
    Arg('--last', action='store_true', help='Use last submitted queue'),
    Arg('-v', '--verbose', action='store_true'),
    Arg('--maxerror', type=int, help='Number of errors in row to quit [default: 5]'),
    Arg('-r', '--randomize', action='store_true', help='Pick tasks in random order')
])
def make(ctx: CommandContext,
         patterns: List[str] = None,
         limit: int = None,
         url: str = None,
         dry: bool = False,
         last: bool = False,
         verbose: bool = False,
         maxerror: int = 5,
         randomize: bool = False) -> None:
    """Execute build tasks."""
    cellar = ctx.cellar
    if verbose:
        Logging.DEBUG = True
    if url:
        url = ctx.get_queue_url(url)
        scheduler: Scheduler = RemoteScheduler(
            url,
            ctx.config.get('core', 'curl', fallback='') or None,
            CAFDIR,
            tmpdir=ctx.config.get('core', 'tmpdir', fallback='') or None,
        )
    else:
        scheduler = Scheduler(
            cellar,
            tmpdir=ctx.config.get('core', 'tmpdir', fallback='') or None,
        )
    if patterns:
        hashes: Optional[Set[Hash]] = \
            set(hashid for hashid, _ in cellar.get_tree().glob(*patterns))
        if not hashes:
            return
    else:
        hashes = None
    signal.signal(signal.SIGTERM, sig_handler)
    signal.signal(signal.SIGXCPU, sig_handler)
    asyncio.get_event_loop().run_until_complete(
        scheduler.tasks_for_work(
            hashes=hashes, limit=limit, dry=dry, nmaxerror=maxerror,
            randomize=randomize
        )
    )


@define_cli([
    Arg('profile', metavar='PROFILE', help='Use worker at ~/.config/caf/worker_PROFILE'),
    Arg('-j', '--jobs', type=int, help='Number of launched workers [default: 1]'),
    Arg('argv', metavar='...', nargs=argparse.REMAINDER, help='Arguments for make')
])
def dispatch(ctx: CommandContext, profile: str, argv: List[str] = None, jobs: int = 1) -> None:
    """Dispatch make to external workers."""
    argv = argv or []
    parser = ThrowingArgumentParser()
    for arg in make.__cli__:  # type: ignore
        parser.add_argument(*arg.args, **arg.kwargs)
    try:
        parser.parse_args(argv)
    except CLIError:
        error(f'Invalid arguments for make: {argv}')
    worker = Path(f'~/.config/caf/worker_{profile}').expanduser()
    cmd = [str(worker)] + argv
    for _ in range(jobs):
        try:
            sp.run(cmd, check=True)
        except sp.CalledProcessError:
            error(f'Running {worker} failed.')


@define_cli([
    Arg('patterns', metavar='PATTERN', nargs='*',
        help='Tasks to be checked out'),
    Arg('-b', '--blddir', type=Path, help=f'Where to checkout [default: blddir]'),
    Arg('--json', dest='do_json', action='store_true',
        help='Do not checkout, print JSONs of hashes from STDIN.'),
    Arg('-f', '--force', action='store_true', help='Remove PATH if exists'),
    Arg('-n', dest='nth', type=int, help='Nth build to the past'),
    Arg('--finished', action='store_true', help='Check out only finished tasks'),
    Arg('-L', '--no-link', action='store_true',
        help='Do not create links to cellar, but copy'),
])
def checkout(ctx: CommandContext,
             blddir: Path = Path('build'),
             patterns: Iterable[str] = None,
             do_json: bool = False,
             force: bool = False,
             nth: int = 0,
             finished: bool = False,
             no_link: bool = False) -> None:
    """Create the dependecy tree physically on a file system."""
    cellar = Cellar()
    if not do_json:
        if blddir.exists():
            if force:
                shutil.rmtree(blddir)
            else:
                error(f'Cannot checkout to existing path: {blddir}')
        cellar.checkout(
            blddir, patterns=patterns or ['**'], nth=nth, finished=finished,
            nolink=no_link
        )
    else:
        hashes = [Hash(l.strip()) for l in sys.stdin.readlines()]
        json.dump({
            hashid: task.asdict_v2(with_outputs=True) for hashid, task in
            cellar.get_tasks(hashes).items()
        }, sys.stdout)


@define_cli([
    Arg('url', metavar='URL'),
    Arg('patterns', metavar='PATTERN', nargs='*', help='Tasks to be submitted'),
    Arg('-a', '--append', action='store_true', help='Append to an existing queue'),
])
def submit(ctx: CommandContext, url: str, patterns: List[str] = None, append: bool = False) -> None:
    """Submit the list of prepared tasks to a queue server."""
    url = ctx.get_queue_url(url)
    announcer = Announcer(url, ctx.config.get('core', 'curl', fallback='') or None)
    cellar = Cellar()
    scheduler = Scheduler(cellar)
    queue = scheduler.get_queue()
    if patterns:
        hashes = dict(cellar.get_tree().glob(*patterns))
    else:
        hashes = {hashid: TPath(label) for hashid, (state, label, *_) in queue.items()}
    hashes = {
        hashid: label for hashid, label in hashes.items()
        if queue[hashid][0] == State.CLEAN
    }
    if not hashes:
        error('No tasks to submit')
    queue_url = announcer.submit(hashes, append=append)
    if queue_url:
        print(f'./caf make --queue {queue_url}')
        ctx.last_queue = queue_url


@define_cli([
    Arg('patterns', metavar='PATTERN', nargs='*', help='Tasks to be reset'),
    Arg('--running', action='store_true', help='Also reset running tasks'),
    Arg('--only-running', action='store_true', help='Reset only running tasks'),
    Arg('--hard', action='store_true',
        help='Also reset finished tasks and remove outputs'),
])
def reset(ctx: CommandContext, patterns: List[str] = None, hard: bool = False,
          running: bool = False, only_running: bool = False) -> None:
    """Remove all temporary checkouts and set tasks to clean."""
    if hard and input('Are you sure? ["y" to confirm] ') != 'y':
        return
    if hard:
        running = True
    cellar = Cellar()
    scheduler = Scheduler(cellar)
    states = scheduler.get_states()
    queue = scheduler.get_queue()
    if patterns:
        hashes = set(
            hashid for hashid, _
            in cellar.get_tree(hashes=states.keys()).glob(*patterns)
        )
    else:
        hashes = set(queue)
    states_to_reset = set()
    if only_running or running:
        states_to_reset.add(State.RUNNING)
    if not only_running:
        states_to_reset.update((State.ERROR, State.INTERRUPTED))
    for hashid in hashes:
        if states[hashid] in states_to_reset:
            scheduler.reset_task(hashid)
        elif hard and states[hashid] in (State.DONE, State.DONEREMOTE, State.CLEAN):
            scheduler.reset_task(hashid)
            if states[hashid] in (State.DONE, State.CLEAN):
                cellar.reset_task(hashid)


@define_cli()
def list_profiles(ctx: CommandContext) -> None:
    """List profiles."""
    for p in Path.home().glob('.config/caf/worker_*'):
        print(p.name)


@define_cli()
def list_remotes(ctx: CommandContext) -> None:
    """List remotes."""
    for name, remote in config_group(ctx.config, 'remote'):
        print(name)
        print(f'\t{remote["host"]}:{remote["path"]}')


@define_cli()
def list_builds(ctx: CommandContext) -> None:
    """List builds."""
    cellar = Cellar()
    table = Table(align='<<')
    for i, created in reversed(list(enumerate(cellar.get_builds()))):
        table.add_row(str(i), created)
    print(table)


@define_cli([
    Arg('patterns', metavar='PATTERN', nargs='*', help='Tasks to be listed'),
    Arg('--finished', dest='do_finished', action='store_true',
        help='List finished tasks'),
    Arg('--unfinished', dest='do_unfinished', action='store_true',
        help='List unfinished tasks'),
    Arg('--running', dest='do_running', action='store_true',
        help='List running tasks'),
    Arg('--error', dest='do_error', action='store_true',
        help='List tasks in error'),
    Arg('--hash', dest='disp_hash', action='store_true',
        help='Display task hash'),
    Arg('--path', dest='disp_path', action='store_true',
        help='Display task virtual path'),
    Arg('--tmp', dest='disp_tmp', action='store_true',
        help='Display temporary path'),
    Arg('--no-color', dest='no_color', action='store_true',
        help='Do not color paths')
])
def list_tasks(ctx: CommandContext,
               patterns: List[str] = None,
               do_finished: bool = False,
               do_unfinished: bool = False,
               do_running: bool = False,
               do_error: bool = False,
               disp_hash: bool = False,
               disp_path: bool = False,
               disp_tmp: bool = False,
               no_color: bool = False) -> None:
    """List tasks."""
    cellar = Cellar()
    scheduler = Scheduler(cellar)
    states = scheduler.get_states()
    queue = scheduler.get_queue()
    if patterns:
        hashes_paths = cellar.get_tree(hashes=states.keys()).glob(*patterns)
    else:
        hashes_paths = (
            (hashid, label) for hashid, (_, label, *__) in sorted(
                queue.items(), key=lambda r: r[1]
            )
        )
    for hashid, path in hashes_paths:
        if do_finished and states[hashid] not in (State.DONE, State.DONEREMOTE):
            continue
        if do_error and states[hashid] != State.ERROR:
            continue
        if do_unfinished and states[hashid] in (State.DONE, State.DONEREMOTE):
            continue
        if do_running and states[hashid] != State.RUNNING:
            continue
        pathstr = str(path) if no_color else colstr(path, states[hashid].color)
        if disp_hash:
            line: str = hashid
        elif disp_tmp:
            if queue[hashid][2]:
                line = queue[hashid][2]
            else:
                continue
        elif disp_path:
            line = pathstr
        else:
            line = f'{hashid} {pathstr} {queue[hashid][2] or ""}'
        try:
            sys.stdout.write(line + '\n')
        except BrokenPipeError:
            handle_broken_pipe()
            break


@define_cli([
    Arg('patterns', metavar='PATTERN', nargs='*', help='Tasks to be reset'),
    Arg('-i', '--incomplete', action='store_true',
        help='Print only incomplete patterns'),
])
def status(ctx: CommandContext, patterns: List[str] = None, incomplete: bool = False) -> None:
    """Print number of initialized, running and finished tasks."""
    cellar = Cellar(ctx.app)
    scheduler = Scheduler(cellar)
    patterns = patterns or ctx.app.paths
    colors = 'yellow green cyan red normal'.split()
    print('number of {} tasks:'.format('/'.join(
        colstr(s, color) for s, color in zip(
            'running finished remote error all'.split(),
            colors
        )
    )))
    states = scheduler.get_states()
    tree = cellar.get_tree(hashes=states.keys())
    groups = tree.dglob(*patterns)
    queue = scheduler.get_queue()
    groups['ALL'] = [(hashid, label) for hashid, (_, label, *__) in queue.items()]
    table = Table(
        align=['<', *len(colors)*['>']],
        sep=['   ', *(len(colors)-1)*['/']]
    )
    for pattern, hashes_paths in groups.items():
        if not hashes_paths:
            pattern = colstr(pattern, 'bryellow')
        grouped = {
            state: subgroup for state, subgroup
            in groupby(hashes_paths, key=lambda x: states[x[0]])
        }
        stats: List[Any] = [len(grouped.get(state, [])) for state in (
            State.RUNNING,
            State.DONE,
            State.DONEREMOTE,
            State.ERROR
        )]
        stats.append(len(hashes_paths))
        if incomplete and stats[1] + stats[2] == stats[4] and pattern != 'All':
            continue
        stats = [
            colstr(s, color) if s else colstr(s, 'normal')
            for s, color in zip(stats, colors)
        ]
        table.add_row(pattern, *stats)
    for state in (State.RUNNING, State.INTERRUPTED):
        color = state.color
        for hashid, path in grouped.get(state, []):
            table.add_row(
                f"{colstr('>>', color)} {path} "
                f"{colstr(queue[hashid][2], color)} {queue[hashid][3]}",
                free=True
            )
    print(table)


@define_cli([
    Arg('-a', '--all', action='store_true', help='Discard all nonactive tasks', dest='gc_all'),
])
def gc(ctx: CommandContext, gc_all: bool = False) -> None:
    """Discard running and error tasks."""
    cellar = Cellar()
    scheduler = Scheduler(cellar)
    scheduler.gc()
    if gc_all:
        scheduler.gc_all()
        cellar.gc()


@define_cli([
    Arg('cmd', metavar='CMD',
        help='This is a simple convenience alias for running commands remotely'),
])
def cmd(ctx: CommandContext, cmd: str) -> None:
    """Execute any shell command."""
    sp.run(cmd, shell=True)


@define_cli([
    Arg('url', metavar='URL'),
    Arg('name', metavar='NAME', nargs='?')
])
def remote_add(ctx: CommandContext, url: str, name: str = None) -> None:
    """Add a remote."""
    config = ConfigParser(interpolation=None)
    config.read([CAFDIR/'config.ini'])
    host, path = url.split(':')
    name = name or host
    config[f'remote "{name}"'] = {'host': host, 'path': path}
    try:
        with (CAFDIR/'config.ini').open('w') as f:
            config.write(f)
    except FileNotFoundError:
        no_cafdir()


@define_cli([
    Arg('name', metavar='NAME')
])
def remote_path(ctx: CommandContext, _: Any, name: str) -> None:
    """Print a remote path in the form HOST:PATH."""
    print('{0[host]}:{0[path]}'.format(ctx.config[f'remote "{name}"']))


@define_cli([
    Arg('remotes', metavar='REMOTE'),
    Arg('--delete', action='store_true', help='Delete files when syncing'),
    Arg('--dry', action='store_true', help='Do a dry run'),
])
def update(ctx: CommandContext, remotes: str, delete: bool = False, dry: bool = False) -> None:
    """Update a remote."""
    for remote in ctx.parse_remotes(remotes):
        remote.update(CAFDIR.parent, delete=delete, dry=dry)


@define_cli([
    Arg('remotes', metavar='REMOTE'),
])
def check(ctx: CommandContext, remotes: str) -> None:
    """Verify that hashes of the local and remote tasks match."""
    cellar = Cellar()
    for remote in ctx.parse_remotes(remotes):
        remote.check(cellar.get_tree())


@define_cli([
    Arg('remotes', metavar='REMOTE'),
    Arg('patterns', metavar='PATTERN', nargs='*', help='Tasks to fetch'),
    Arg('--no-files', action='store_true', help='Fetch task metadata, but not files'),
])
def fetch(ctx: CommandContext,
          remotes: str,
          patterns: List[str] = None,
          no_files: bool = False) -> None:
    """Fetch targets from remote."""
    cellar = Cellar()
    scheduler = Scheduler(cellar)
    states = scheduler.get_states()
    if patterns:
        hashes = set(hashid for hashid, _ in cellar.get_tree().glob(*patterns))
    else:
        hashes = set(states)
    for remote in ctx.parse_remotes(remotes):
        tasks = remote.fetch([
            hashid for hashid in hashes if states[hashid] == State.CLEAN
        ] if no_files else [
            hashid for hashid in hashes
            if states[hashid] in (State.CLEAN, State.DONEREMOTE)
        ], files=not no_files)
        for hashid, task in tasks.items():
            if not no_files:
                cellar.seal_task(hashid, hashed_outputs=task['outputs'])
            scheduler.task_done(hashid, remote=remote.host if no_files else None)


@define_cli([
    Arg('filename', metavar='FILE'),
    Arg('patterns', metavar='PATTERN', nargs='*'),
])
def archive_store(ctx: CommandContext, filename: str, patterns: List[str] = None) -> None:
    """Archives files accessible from the given tasks as tar.gz."""
    cellar = Cellar()
    scheduler = Scheduler(cellar)
    states = scheduler.get_states()
    if patterns:
        hashes = set(hashid for hashid, _ in cellar.get_tree().glob(*patterns))
    else:
        hashes = set(states)
    cellar.archive(hashes, filename)  # type: ignore  # TODO


# @Caf.command()
# def push(caf, targets: 'TARGET', dry: '--dry', remotes: ('REMOTE', 'proc_remote')):
#     """
#     Push targets to remote and store them in remote Cellar.
#
#     Usage:
#         caf push REMOTE [TARGET...] [--dry]
#
#     Options:
#         -n, --dry                  Dry run (do not write to disk).
#     """
#     for remote in remotes:
#         remote.push(targets, caf.cache, caf.out, dry=dry)


@define_cli([
    Arg('remotes', metavar='REMOTE'),
])
def go(ctx: CommandContext, remotes: str) -> None:
    """SSH into the remote caf repository."""
    for remote in ctx.parse_remotes(remotes):
        remote.go()
