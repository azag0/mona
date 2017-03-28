import subprocess as sp
import os
import json


from caflib.Logging import info, error


class Remote:
    def __init__(self, host, path, top):
        self.host = host
        self.path = path
        self.top = top

    def update(self, delete=False):
        info(f'Updating {self.host}...')
        sp.run(['ssh', self.host, f'mkdir -p {self.path}'], check=True)
        exclude = []
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

    def command(self, cmd, get_output=False, inp=None):
        if not get_output:
            info(f'Running `./caf {cmd}` on {self.host}...')
        if inp:
            inp = inp.encode()
        try:
            output = sp.run([
                'ssh',
                self.host,
                f'sh -c "cd {self.path} && exec python3 -u caf {cmd}"'
            ], check=True, input=inp, stdout=sp.PIPE if get_output else None)
        except sp.CalledProcessError:
            error(f'Command `{cmd}` on {self.host} ended with error')
        if get_output:
            return output.stdout.decode()

    def check(self, hashes):
        info(f'Checking {self.host}...')
        remote_hashes = dict(
            reversed(l.split()[:2]) for l in self.command(
                'list tasks --no-color', get_output=True
            ).strip().split('\n')
        )
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

    def fetch(self, hashes, files=True):
        info(f'Fetching from {self.host}...')
        tasks = {hashid: task for hashid, task in json.loads(self.command(
            'checkout --json', get_output=True, inp='\n'.join(hashes)
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

    def go(self):
        sp.call(['ssh', '-t', self.host, f'cd {self.path} && exec $SHELL'])


class Local:
    def __init__(self):
        self.host = 'local'

    def update(self, delete=False):
        pass

    def command(self, cmd, get_output=False):
        if not get_output:
            info(f'Running `./caf {cmd}` on {self.host}...')
        caller = sp.check_output if get_output else sp.check_call
        try:
            output = caller(f'sh -c "python3 -u caf {cmd}"', shell=True)
        except sp.CalledProcessError:
            error(f'Command `{cmd}` on {self.host} ended with error')
        return output.strip() if get_output else None

    def check(self, root):
        pass

    def fetch(self, targets, cache, root, dry=False, get_all=False, follow=False):
        pass

    def push(self, targets, cache, root, dry=False):
        pass

    def go(self):
        pass
