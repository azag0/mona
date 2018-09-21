# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import json
from collections import deque
import logging
import hashlib
from abc import ABC, abstractmethod

from .json_utils import ClassJSONEncoder, ClassJSONDecoder

from typing import Iterable, Set, Any, NewType, Dict, Callable, Optional, \
    List, Deque, TypeVar, Generic, Union, Tuple

log = logging.getLogger(__name__)

Hash = NewType('Hash', str)
_T = TypeVar('_T')
_Fut = TypeVar('_Fut', bound='Future')
CallbackFut = Callable[[_Fut], None]


def get_hash(text: Union[str, bytes]) -> Hash:
    if isinstance(text, str):
        text = text.encode()
    return Hash(hashlib.sha1(text).hexdigest())


class FutureNotDone(Exception):
    pass


class Future(ABC, Generic[_Fut]):
    def __init__(self, deps: Iterable['Future']) -> None:
        self._pending: Set['Future'] = set()
        for fut in deps:
            if not fut.done():
                self._pending.add(fut)
                fut.add_depant(self)
        self._depants: Set['Future'] = set()
        self._result: Any = FutureNotDone
        self._done_callbacks: List[CallbackFut] = []
        self._ready_callbacks: List[CallbackFut] = []

    def __repr__(self) -> str:
        return self.hashid

    def ready(self) -> bool:
        return not self._pending

    def done(self) -> bool:
        return self._result is not FutureNotDone

    def add_depant(self, fut: 'Future') -> None:
        self._depants.add(fut)

    def add_ready_callback(self, callback: CallbackFut) -> None:
        if self.ready():
            callback(self)
        else:
            self._ready_callbacks.append(callback)

    def add_done_callback(self, callback: CallbackFut) -> None:
        assert not self.done()
        self._done_callbacks.append(callback)

    def dep_done(self, fut: 'Future') -> None:
        self._pending.remove(fut)
        if self.ready():
            log.debug(f'future ready: {self}')
            for callback in self._ready_callbacks:
                callback(self)

    def result(self) -> Any:
        assert self._result is not FutureNotDone
        return self._result

    def set_result(self, result: Any) -> None:
        assert self.ready()
        assert self._result is FutureNotDone
        self._result = result
        log.debug(f'future done: {self}')
        for fut in self._depants:
            fut.dep_done(self)
        for callback in self._done_callbacks:
            callback(self)

    @property
    @abstractmethod
    def hashid(self) -> Hash:
        ...


class Template(Future):
    def __init__(self, jsonstr: str, futures: Iterable[Future]) -> None:
        super().__init__(futures)
        self._jsonstr = jsonstr
        self._futures = {fut.hashid: fut for fut in futures}
        self._hashid = Hash(f'{{}}{get_hash(self._jsonstr)}')
        log.debug(f'{self._hashid} <= {self._jsonstr}')
        self.add_ready_callback(
            lambda tmpl: tmpl.set_result(tmpl.substitute())  # type: ignore
        )

    @property
    def hashid(self) -> Hash:
        return self._hashid

    def substitute(self) -> Any:
        return json.loads(
            self._jsonstr,
            classes={
                Task: lambda dct: self._futures[dct['hashid']].result(),
                Indexor: lambda dct: self._futures[dct['hashid']].result(),
            },
            cls=ClassJSONDecoder
        )

    @staticmethod
    def parse(obj: Any) -> Tuple[str, Set[Future]]:
        futures: Set[Future] = set()
        jsonstr = json.dumps(
            obj,
            sort_keys=True,
            tape=futures,
            classes={
                Task: lambda fut: {'hashid': fut.hashid},
                Indexor: lambda fut: {'hashid': fut.hashid},
            },
            cls=ClassJSONEncoder
        )
        return jsonstr, futures


class Indexor(Future):
    def __init__(self, task: 'Task', keys: List[Union[str, int]]) -> None:
        super().__init__([task])
        self._task = task
        self._keys = keys
        self._hashid = Hash('/'.join(['@' + task.hashid, *map(str, keys)]))
        self.add_ready_callback(
            lambda idx: idx.set_result(idx.resolve())  # type: ignore
        )

    def __getitem__(self, key: Union[str, int]) -> 'Indexor':
        return Indexor(self._task, self._keys + [key])

    @property
    def hashid(self) -> Hash:
        return self._hashid

    def resolve(self) -> Any:
        obj = self._task.result()
        for key in self._keys:
            obj = obj[key]
        return obj


def wrap_input(obj: Any) -> Future:
    if isinstance(obj, Future):
        return obj
    return Template(*Template.parse(obj))


def wrap_output(obj: Any) -> Any:
    if isinstance(obj, Future):
        return obj
    jsonstr, futures = Template.parse(obj)
    if futures:
        return Template(jsonstr, futures)
    return obj


class Task(Future):
    def __init__(self, hashid: Hash, hash_str: str, f: Callable, *args: Future
                 ) -> None:
        super().__init__(args)
        self._hashid = hashid
        self._hash_str = hash_str
        self._f = f
        self._args = args
        log.info(f'{hashid} <= {hash_str}')

    def __getitem__(self, key: Union[str, int]) -> Indexor:
        return Indexor(self, [key])

    @property
    def hashid(self) -> Hash:
        return self._hashid

    @property
    def hash_str(self) -> str:
        return self._hash_str

    def run(self) -> None:
        assert self.ready()
        log.debug(f'task will run: {self}')
        args = [arg.result() for arg in self._args]
        result = wrap_output(self._f(*args))
        if isinstance(result, Future):
            log.info(f'task has run, pending: {self}')
            result.add_done_callback(lambda fut: self.set_result(fut.result()))
        else:
            self.set_result(result)


class Session:
    _active: Optional['Session'] = None

    def __init__(self) -> None:
        self._pending: Set[Task] = set()
        self._waiting: Deque[Task] = deque()
        self._tasks: Dict[Hash, Task] = {}

    def __enter__(self) -> 'Session':
        assert Session._active is None
        Session._active = self
        return self

    def __exit__(self, *args: Any) -> None:
        Session._active = None
        self._pending.clear()
        self._waiting.clear()
        self._tasks.clear()

    def _task_ready(self, task: Task) -> None:
        self._pending.remove(task)
        self._waiting.append(task)

    def create_task(self, f: Callable, *args: Any) -> Task:
        args = tuple(map(wrap_input, args))
        hash_obj = [get_fullname(f), *(fut.hashid for fut in args)]
        hash_str = json.dumps(hash_obj, sort_keys=True)
        hashid = get_hash(hash_str)
        try:
            return self._tasks[hashid]
        except KeyError:
            pass
        task = Task(hashid, hash_str, f, *args)
        self._pending.add(task)
        task.add_ready_callback(self._task_ready)
        self._tasks[hashid] = task
        return task

    def eval(self, obj: Any) -> Any:
        if isinstance(obj, Future):
            fut = obj
        else:
            jsonstr, futures = Template.parse(obj)
            if not futures:
                return obj
            fut = Template(jsonstr, futures)
        while self._waiting:
            task = self._waiting.popleft()
            task.run()
        return fut.result()

    @classmethod
    def active(cls) -> 'Session':
        assert cls._active is not None
        return cls._active


class Rule:
    def __init__(self, f: Callable) -> None:
        self._f = f

    def __repr__(self) -> str:
        return f'<Rule f={self._f!r}>'

    def __call__(self, *args: Any) -> Task:
        return Session.active().create_task(self._f, *args)


def get_fullname(obj: Any) -> str:
    return f'{obj.__module__}:{obj.__qualname__}'
