import json
import subprocess
import glob
from pathlib import Path
import signal
import sys

from caflib.Utils import cd


class Worker:
    def __init__(self, myid, path):
        self.myid = myid
        self.path = path

    def work(self, targets, dry=False, descend=True):
        queue = []

        def enqueue(path, descend):
            if (path/'.caf/seal').is_file():
                return
            children = [path/x for x in json.load((path/'.caf/children').open())]
            if descend:
                if path not in queue and (path/'.caf/lock').is_file():
                    queue.append(path)
                for child in children:
                    enqueue(child, True)
            else:
                if path not in queue and (path/'.caf/lock').is_file() and \
                        all((child/'.caf/seal').is_file() for child in children):
                    queue.append(path)

        def sigint_handler(sig, frame):
            print('Worker {} interrupted, aborting.'.format(self.myid))
            sys.exit()
        signal.signal(signal.SIGINT, sigint_handler)

        print('Worker {} alive and ready.'.format(self.myid))
        if targets:
            targets = [self.path/t for t in targets]
        else:
            targets = [Path(p) for p in glob.glob('{}/*'.format(self.path))]
        for target in targets:
            if target.is_symlink():
                enqueue(target, descend)
            else:
                for task in target.glob('*'):
                    enqueue(task, descend)
        while queue:
            path = queue.pop()
            lock = path/'.lock'
            try:
                lock.mkdir()
            except OSError:
                continue
            if (path/'.caf/seal').is_file():
                (path/'.lock').rmdir()
                continue
            print('Worker {} started working on {}...'.format(self.myid, path))
            if not dry:
                with cd(path):
                    with open('run.out', 'w') as stdout, \
                            open('run.err', 'w') as stderr:
                        try:
                            subprocess.check_call(open('command').read(),
                                                  shell=True,
                                                  stdout=stdout,
                                                  stderr=stderr)
                            with (path/'.caf/seal').open('w') as f:
                                print(self.myid, file=f)
                        except subprocess.CalledProcessError as e:
                            print(e)
                            print('error: There was an error when working on {}'
                                  .format(path))
            (path/'.lock').rmdir()
            print('Worker {} finished working on {}.'.format(self.myid, path))
        print('Worker {} has no more tasks to do, aborting.'.format(self.myid))
