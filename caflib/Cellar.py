# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
from pathlib import Path
import json
import sqlite3
import hashlib
from datetime import datetime
from collections import defaultdict
import sys
import os
import shutil
from enum import Enum

from caflib.Logging import info, no_cafdir
from caflib.Utils import make_nonwritable
from caflib.Timing import timing
from caflib.Glob import match_glob

from typing import (  # noqa
    NewType, NamedTuple, Dict, Tuple, Any, List, DefaultDict, Iterable,
    Generator, Set, cast, Optional, Union, Callable
)

Hash = NewType('Hash', str)
TPath = NewType('TPath', str)
TimeStamp = NewType('TimeStamp', str)


class State(Enum):
    CLEAN = 0
    DONE = 1
    DONEREMOTE = 5
    ERROR = -1
    RUNNING = 2
    INTERRUPTED = 3
    color = {
        CLEAN: 'normal',
        DONE: 'green',
        DONEREMOTE: 'cyan',
        ERROR: 'red',
        RUNNING: 'yellow',
        INTERRUPTED: 'blue',
    }


def get_hash(text: str) -> Hash:
    return get_hash_bytes(text.encode())


def get_hash_bytes(text: bytes) -> Hash:
    return Hash(hashlib.sha1(text).hexdigest())


class _TaskObject(NamedTuple):
    command: str
    inputs: Dict[str, Hash]
    symlinks: Dict[str, str]
    children: Dict[str, Hash]
    childlinks: Dict[str, Tuple[str, str]]
    outputs: Optional[Dict[str, Hash]]


class TaskObject(_TaskObject):
    @classmethod
    def from_obj(cls, obj: Dict[str, Any]) -> 'TaskObject':
        kwargs: Dict[str, Any] = {'outputs': None, **obj}
        return cls(**kwargs)

    def to_obj(self) -> Dict[str, Any]:
        obj = self._asdict()
        if self.outputs is None:
            del obj['outputs']
        return obj


class Tree(Dict[TPath, Hash]):
    def __init__(
            self,
            hashes: Iterable[Tuple[TPath, Hash]],
            objects: Dict[Hash, TaskObject] = None
    ) -> None:
        super().__init__(hashes)
        self.objects = objects or {}

    def dglob(self, *patterns: str) -> Dict[str, List[Tuple[Hash, str]]]:
        groups: DefaultDict[str, List[Tuple[Hash, str]]] = defaultdict(list)
        for patt in patterns:
            matched_any = False
            for path, hashid in self.items():
                matched = match_glob(path, patt)
                if matched:
                    groups[matched].append((hashid, path))
                    matched_any = True
            if not matched_any:
                groups[patt] = []
        return groups

    def glob(self, *patterns: str) -> Generator[Tuple[Hash, TPath], None, None]:
        for patt in patterns:
            for path, hashid in self.items():
                if match_glob(path, patt):
                    yield hashid, path


def symlink_to(src: Union[str, Path], dst: Path) -> None:
    dst.symlink_to(src)


def copy_to(src: Path, dst: Path) -> None:
    shutil.copyfile(src, dst)


class Cellar:
    def __init__(self, path: Path) -> None:
        path = path.resolve()
        self.objects = path/'objects'
        self.objectdb: Set[Hash] = set()
        try:
            self.db = sqlite3.connect(str(path/'index.db'))
        except sqlite3.OperationalError:
            no_cafdir()
        self.execute(
            'create table if not exists tasks ('
            'hash text primary key, task text, created text, state integer'
            ')'
        )
        self.execute(
            'create table if not exists builds ('
            'id integer primary key, created text'
            ')'
        )
        self.execute(
            'create table if not exists targets ('
            'taskhash text, buildid integer, path text, '
            'foreign key(taskhash) references tasks(hash), '
            'foreign key(buildid) references builds(id)'
            ')'
        )

    def execute(self, sql: str, *parameters: Iterable) -> sqlite3.Cursor:
        return self.db.execute(sql, *parameters)

    def executemany(self, sql: str, *seq_of_parameters: Iterable[Iterable]) -> sqlite3.Cursor:
        return self.db.executemany(sql, *seq_of_parameters)

    def commit(self) -> None:
        self.db.commit()

    def get_state(self, hashid: Hash) -> State:
        res = self.execute(
            'select state from tasks where hash = ?', (hashid,)
        ).fetchone()
        if not res:
            return State.ERROR
        return State(res[0])

    def store(self, hashid: Hash, text: str = None, file: Path = None) -> bool:
        if hashid in self.objectdb:
            return False
        self.objectdb.add(hashid)
        path = self.objects/hashid[:2]/hashid[2:]
        if path.is_file():
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        if text is not None:
            path.write_text(text)
        elif file is not None:
            file.rename(path)
        make_nonwritable(path)
        return True

    def gc(self) -> None:
        tree = self.get_tree(objects=True)
        self.execute('create temporary table retain(hash text)')
        self.executemany('insert into retain values (?)', (
            (hashid,) for hashid in tree.values()
        ))
        for task in tree.objects.values():
            for filehash in task.inputs.values():
                self.execute('insert into retain values (?)', (filehash,))
            if task.outputs is not None:
                for filehash in task.outputs.values():
                    self.execute('insert into retain values (?)', (filehash,))
        retain = set(r[0] for r in self.db.execute('select hash from retain'))
        all_files = {Hash(''.join(p.parts[-2:])): p for p in self.objects.glob('*/*')}
        n_files = 0
        for filehash in set(all_files.keys()) - retain:
            all_files[filehash].unlink()
            n_files += 1
        info(f'Removed {n_files} files.')
        self.db.execute(
            'delete from targets where buildid != '
            '(select id from builds order by created desc limit 1)'
        )
        self.db.execute(
            'delete from tasks '
            'where hash not in (select distinct(hash) from retain)'
        )
        self.commit()

    def store_text(self, hashid: Hash, text: str) -> bool:
        return self.store(hashid, text=text)

    def store_file(self, hashid: Hash, file: Path) -> bool:
        return self.store(hashid, file=file)

    def _get_task_obj(self, hashid: Hash) -> Optional[Dict[str, Any]]:
        row = self.execute(
            'select task from tasks where hash = ?', (hashid,)
        ).fetchone()
        if not row:
            return None
        blob, = row
        return cast(Dict[str, Any], json.loads(blob))

    def _update_outputs(
            self,
            hashid: Hash,
            state: State,
            outputs: Dict[str, Hash]
    ) -> None:
        obj = self._get_task_obj(hashid)
        assert obj
        obj['outputs'] = outputs
        self.execute(
            'update tasks set task = ?, state = ? where hash = ?',
            (json.dumps(obj), state.value, hashid)
        )
        self.commit()

    def seal_task(
            self,
            hashid: Hash,
            outputs: Dict[str, Path] = None,
            hashed_outputs: Dict[str, Hash] = None
    ) -> None:
        if outputs:
            hashed_outputs = {}
            for name, path in outputs.items():
                try:
                    with path.open() as f:
                        filehash = get_hash(f.read())
                except UnicodeDecodeError:
                    with path.open('rb') as f:
                        filehash = get_hash_bytes(f.read())
                self.store_file(filehash, path)
                hashed_outputs[name] = filehash
        assert hashed_outputs
        self._update_outputs(hashid, State.DONE, hashed_outputs)

    def reset_task(self, hashid: Hash) -> None:
        self._update_outputs(hashid, State.CLEAN, {})

    def store_build(
            self,
            tasks: Dict[Hash, TaskObject],
            targets: Dict[TPath, Hash],
            inputs: Dict[Hash, str],
            labels: Dict[Hash, TPath]
    ) -> Iterable[Tuple[Hash, State]]:
        self.execute('drop table if exists current_tasks')
        self.execute('create temporary table current_tasks(hash text)')
        self.executemany('insert into current_tasks values (?)', (
            (key,) for key in tasks.keys()
        ))
        existing = [hashid for hashid, in self.execute(
            'select tasks.hash from tasks join current_tasks '
            'on current_tasks.hash = tasks.hash'
        )]
        nnew = len(tasks)-len(existing)
        info(f'Will store {nnew} new tasks.')
        if nnew > 0 and 'TIMING' not in os.environ:
            while True:
                answer = input('Continue? ["y" to confirm, "l" to list]: ')
                if answer == 'y':
                    break
                elif answer == 'l':
                    for label in sorted(
                            labels[h] for h in set(tasks)-set(existing)
                    ):
                        print(label)
                else:
                    sys.exit()
        now = datetime.today().isoformat(timespec='seconds')  # type: ignore
        self.executemany('insert or ignore into tasks values (?,?,?,?)', (
            (hashid, json.dumps(task), now, 0) for hashid, task in tasks.items()
            # TODO sort_keys=True
        ))
        cur = self.execute('insert into builds values (?,?)', (None, now))
        buildid = cur.lastrowid
        self.executemany('insert into targets values (?,?,?)', (
            (hashid, buildid, path) for path, hashid in targets.items()
        ))
        for hashid, text in inputs.items():
            self.store_text(hashid, text)
        self.commit()
        return self.execute(
            'select tasks.hash, state from tasks join current_tasks '
            'on tasks.hash = current_tasks.hash',
        ).fetchall()

    def get_task(self, hashid: Hash) -> Optional[TaskObject]:
        with timing('get_task'):
            obj = self._get_task_obj(hashid)
            if not obj:
                return None
            return TaskObject.from_obj(obj)

    def get_tasks(self, hashes: Iterable[Hash]) -> Dict[Hash, TaskObject]:
        hashes = list(hashes)
        if len(hashes) < 10:
            cur = self.execute(
                'select hash, task from tasks where hash in ({})'.format(
                    ','.join(len(hashes)*['?'])
                ),
                hashes
            )
        else:
            self.execute('drop table if exists current_tasks')
            self.execute('create temporary table current_tasks(hash text)')
            self.executemany('insert into current_tasks values (?)', (
                (hashid,) for hashid in hashes
            ))
            cur = self.execute(
                'select tasks.hash, task from tasks join current_tasks '
                'on current_tasks.hash = tasks.hash'
            )
        return {hashid: TaskObject.from_obj(json.loads(blob)) for hashid, blob in cur}

    def get_file(self, hashid: Hash) -> Path:
        path = self.objects/hashid[:2]/hashid[2:]
        if hashid not in self.objectdb:
            if not path.is_file():
                raise FileNotFoundError()
        return path

    def checkout_task(
            self,
            task: TaskObject,
            path: Path,
            resolve: bool = True,
            nolink: bool = False
    ) -> List[str]:
        copier: Callable[[Path, Path], None] = copy_to if nolink else symlink_to
        children = self.get_tasks(task.children.values())
        all_files = []
        for target, filehash in task.inputs.items():
            copier(self.get_file(filehash), path/target)
            all_files.append(target)
        for target, (child, source) in task.childlinks.items():
            if resolve:
                childtask = children[task.children[child]]
                assert childtask.outputs
                childfile = childtask.outputs.get(
                    source, childtask.inputs.get(source)
                )
                assert childfile
                copier(self.get_file(childfile), path/target)
            else:
                symlink_to(Path(child)/source, path/target)
            all_files.append(target)
        if task.outputs is not None:
            for target, filehash in task.outputs.items():
                copier(self.get_file(filehash), path/target)
                all_files.append(target)
        return all_files

    def get_build(self, nth: int = 0) \
            -> Tuple[Dict[Hash, TaskObject], List[Tuple[Hash, Path]]]:
        targets = [(hashid, Path(path)) for hashid, path in self.db.execute(
            'select taskhash, path from targets join '
            '(select id from builds order by created desc limit 1 offset ?) b '
            'on targets.buildid = b.id',
            (nth,)
        )]
        tasks = {
            hashid: TaskObject.from_obj(json.loads(blob)) for hashid, blob
            in self.db.execute(
                'select tasks.hash, task from tasks join '
                '(select distinct(taskhash) as hash from targets join '
                '(select id from builds order by created desc limit 1) b '
                'on targets.buildid = b.id) build '
                'on tasks.hash = build.hash'
            )
        }
        return tasks, targets

    def get_builds(self) -> List[TimeStamp]:
        return [created for created, in self.db.execute(
            'select created from builds order by created desc',
        )]

    def get_tree(self, objects: bool = False, hashes: Iterable[Hash] = None) -> Tree:
        tasks, targets = self.get_build()
        if hashes:
            tasks.update(self.get_tasks(hashes))
        tree = [(TPath(str(path)), hashid) for hashid, path in targets]
        while targets:
            hashid, path = targets.pop()
            for name, childhash in tasks[hashid].children.items():
                childpath = path/name
                tree.append((TPath(str(childpath)), childhash))
                if childhash not in tasks:
                    task = self.get_task(childhash)
                    assert task
                    tasks[childhash] = task
                targets.append((childhash, childpath))
        return Tree(sorted(tree), objects=tasks if objects else None)

    def checkout(
            self,
            root: Path,
            patterns: Iterable[str],
            nth: int = 0,
            finished: bool = False,
            nolink: bool = False
    ) -> None:
        tasks, targets = self.get_build(nth=nth)
        root = root.resolve()
        paths: Dict[Hash, Path] = {}
        nsymlinks = 0
        ntasks = 0
        while targets:
            hashid, path = targets.pop()
            if hashid not in tasks:
                with timing('sql'):
                    task = self.get_task(hashid)
                    assert task
                    tasks[hashid] = task
            for name, childhash in tasks[hashid].children.items():
                childpath = path/name
                targets.append((childhash, childpath))
            if not any(match_glob(str(path), patt) for patt in patterns):
                continue
            if finished and 'outputs' not in tasks[hashid]:
                continue
            rootpath = root/path
            if hashid in paths:
                with timing('bones'):
                    rootpath.parent.mkdir(parents=True, exist_ok=True)
                    if not rootpath.exists():
                        rootpath.symlink_to(paths[hashid])
                        nsymlinks += 1
            else:
                with timing('bones'):
                    rootpath.mkdir(parents=True)
                with timing('checkout'):
                    nsymlinks += len(self.checkout_task(
                        tasks[hashid], rootpath, resolve=False, nolink=nolink
                    ))
                    ntasks += 1
                paths[hashid] = rootpath
        info(f'Checked out {ntasks} tasks: {nsymlinks} {"files" if nolink else "symlinks"}')
