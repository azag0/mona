# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import subprocess as sp
import os
import json
from pathlib import Path

from caflib.Logging import info, error

from typing import List, Optional, cast, Dict, Iterable, Any, Callable  # noqa
from caflib.Cellar import Hash, TPath


class Remote:
    def __init__(self, host: str, path: str, top: Path) -> None:
        self.host = host
        self.path = path
        self.top = top

    def update(self, delete: bool = False) -> None:
        info(f'Updating {self.host}...')
        sp.run(['ssh', self.host, f'mkdir -p {self.path}'], check=True)
        exclude: List[str] = []
        for file in ['.cafignore', os.path.expanduser('~/.config/caf/ignore')]:
            if os.path.exists(file):
                with open(file) as f:
                    exclude.extend(l.strip() for l in f.readlines())
        cmd = [
            'rsync', '-cirl', '--copy-unsafe-links',
            '--exclude=.*', '--exclude=build*', '--exclude=*.pyc',
            '--exclude=__pycache__'
        ]
        if delete:
            cmd.append('--delete')
        cmd.extend(f'--exclude={patt}' for patt in exclude)
        cmd.extend(['caf', 'cscript.py', str(self.top)])
        if os.path.exists('caflib'):
            cmd.append('caflib')
        cmd.append(f'{self.host}:{self.path}')
        sp.run(cmd, check=True)

    def command(self, cmd: str, inp: str = None, _get_output: bool = False) -> Optional[str]:
        if not _get_output:
            info(f'Running `./caf {cmd}` on {self.host}...')
        inp_bytes = inp.encode() if inp else None
        try:
            output = sp.run([  # type: ignore
                'ssh',
                self.host,
                f'sh -c "cd {self.path} && exec python3 -u caf {cmd}"'
            ], check=True, input=inp_bytes, stdout=sp.PIPE if _get_output else None)
        except sp.CalledProcessError:
            error(f'Command `{cmd}` on {self.host} ended with error')
        if _get_output:
            return cast(str, output.stdout.decode())
        return None

    def command_output(self, cmd: str, inp: str = None) -> str:
        output = self.command(cmd, inp, _get_output=True)
        assert output
        return output

    def check(self, hashes: Dict[TPath, Hash]) -> None:
        info(f'Checking {self.host}...')
        remote_hashes: Dict[TPath, Hash] = {}
        output = self.command_output('list tasks --no-color')
        for hashid, path, *_ in (l.split() for l in output.strip().split('\n')):
            remote_hashes[TPath(path)] = Hash(hashid)
        is_ok = True
        for path, hashid in hashes.items():
            if path not in remote_hashes:
                print(f'{path} does not exist on remote')
                is_ok = False
            elif remote_hashes[path] != hashid:
                print(f'{path} has a different hash on remote')
                is_ok = False
        for path, hashid in remote_hashes.items():
            if path not in hashes:
                print(f'{path} does not exist on local')
                is_ok = False
        if is_ok:
            info('Local tasks are on remote')
        else:
            error('Local tasks are not on remote')

    def fetch(self, hashes: List[Hash], files: bool = True) \
            -> Dict[Hash, Dict[str, Any]]:
        info(f'Fetching from {self.host}...')
        tasks = {hashid: task for hashid, task in json.loads(self.command_output(
            'checkout --json', inp='\n'.join(hashes)
        )).items() if 'outputs' in task}
        if not files:
            info(f'Fetched {len(tasks)}/{len(hashes)} task metadata')
            return tasks
        info(f'Will fetch {len(tasks)}/{len(hashes)} tasks')
        if len(tasks) == 0:
            return {}
        elif input('Continue? ["y" to confirm]: ') != 'y':
            return {}
        paths = set(
            hashid
            for task in tasks.values()
            for hashid in task['outputs'].values()
        )
        cmd = [
            'rsync', '-cirlP', '--files-from=-',
            f'{self.host}:{self.path}/.caf/objects', '.caf/objects'
        ]
        sp.run(cmd, input='\n'.join(f'{p[0:2]}/{p[2:]}' for p in paths).encode())
        return tasks

    # def push(self, targets, cache, root, dry=False):
    #     info('Pushing to {}...'.format(self.host))
    #     roots = [p for p in root.glob('*')
    #              if not targets or p.name in targets]
    #     paths = set()
    #     for task in find_tasks(*roots, stored=True, follow=False):
    #         paths.add(get_stored(task))
    #     cmd = ['rsync',
    #            '-cirlP',
    #            '--delete',
    #            '--exclude=*.pyc',
    #            '--exclude=.caf/env',
    #            '--exclude=__pycache__',
    #            '--dry-run' if dry else None,
    #            '--files-from=-',
    #            str(cache),
    #            '{0.host}:{0.path}/{1}'.format(self, cache)]
    #     p = sp.Popen(filter_cmd(cmd), stdin=sp.PIPE)
    #     p.communicate('\n'.join(paths).encode())

    def go(self) -> None:
        sp.call(['ssh', '-t', self.host, f'cd {self.path} && exec $SHELL'])


class Local:
    def __init__(self) -> None:
        self.host = 'local'

    def update(self, delete: bool = False) -> None:
        pass

    def command(self, cmd: str, _get_output: bool = False) -> Optional[str]:
        if not _get_output:
            info(f'Running `./caf {cmd}` on {self.host}...')
        cmd = f'sh -c "python3 -u caf {cmd}"'
        try:
            if _get_output:
                output = sp.check_output(cmd, shell=True)
            else:
                sp.check_call(cmd, shell=True)
        except sp.CalledProcessError:
            error(f'Command `{cmd}` on {self.host} ended with error')
        return cast(str, output.strip()) if _get_output else None

    def check(self, hashes: Dict[TPath, Hash]) -> None:
        pass

    def fetch(self, hashes: List[Hash], files: bool = True) \
            -> Dict[Hash, Dict[str, Any]]:
        pass

    def go(self) -> None:
        pass
