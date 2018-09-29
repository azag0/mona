# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
import asyncio
from enum import Enum
from typing import TypeVar, Deque, Set, Callable, Iterable, \
    MutableSequence, Dict, Awaitable, Container, Iterator, \
    AsyncIterator, Tuple, cast, Optional, Any

_T = TypeVar('_T')
NodeScheduler = Callable[[_T, Callable[[_T], None]], None]
NodeExecuted = Callable[[Iterable[_T]], None]
NodeExecutor = Callable[[_T, NodeExecuted[_T]], Awaitable[None]]
Priority = Tuple['Action', 'Action', 'Action']
Step = Tuple['Action', Optional[_T], Dict[str, int]]


def extend_from(src: Iterable[_T],
                seq: MutableSequence[_T], *,
                filter: Container[_T]) -> None:
    seq.extend(x for x in src if x not in filter)


class Action(Enum):
    RESULTS = 0
    EXECUTE = 1
    TRAVERSE = 2


default_priority = cast(Priority, tuple(Action))


# only limited override for use in traverse_async()
class SetDeque(Deque[_T]):
    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self._set: Set[_T] = set()

    def extend(self, xs: Iterable[_T]) -> None:
        xs = set(xs)
        super().extend(xs - self._set)
        self._set.update(xs)

    def pop(self) -> _T:  # type: ignore
        x = super().pop()
        self._set.remove(x)
        return x

    def popleft(self) -> _T:
        x = super().popleft()
        self._set.remove(x)
        return x


async def traverse_async(start: Iterable[_T],
                         edges_from: Callable[[_T], Iterable[_T]],
                         schedule: NodeScheduler[_T],
                         execute: NodeExecutor[_T],
                         sentinel: Callable[[_T], bool] = None,
                         depth: bool = False,
                         priority: Priority = default_priority
                         ) -> AsyncIterator[Step[_T]]:
    """
    Traverse a self-extending DAG, yield steps.

    :param start: Starting nodes
    :param edges_from: Returns nodes with incoming edge from the given node
    :param schedule: Schedule the given node for execution (not run on sentinels)
    :param execute: Execute the given node and return new generated nodes
                    with incoming edge from it (run only on scheduled nodes)
    :param sentinel: Should traversal stop at the given node?
    :param depth: Traverse depth-first if true, breadth-first otherwise
    :param priority: Priorize steps in order
    """
    visited: Set[_T] = set()
    to_visit, to_execute = SetDeque[_T](), Deque[_T]()
    done: 'asyncio.Queue[Iterable[_T]]' = asyncio.Queue()
    executing, executed = 0, 0
    actionable: Dict[Action, Callable[[], bool]] = {
        Action.RESULTS: lambda: not done.empty(),
        Action.EXECUTE: lambda: bool(to_execute),
        Action.TRAVERSE: lambda: bool(to_visit),
    }
    to_visit.extend(start)
    while True:
        for action in priority:
            if actionable[action]():
                break
        else:
            if executing == 0:
                break
            action = Action.RESULTS
        progress = {
            'executing': executing-done.qsize(),
            'to_execute': len(to_execute),
            'to_visit': len(to_visit),
            'with_result': done.qsize(),
            'done': executed,
            'visited': len(visited)
        }
        if action is Action.TRAVERSE:
            node = to_visit.pop() if depth else to_visit.popleft()
            yield action, node, progress
            visited.add(node)
            if sentinel and sentinel(node):
                continue
            schedule(node, to_execute.append)
            extend_from(edges_from(node), to_visit, filter=visited)
        elif action is Action.EXECUTE:
            node = to_execute.popleft()
            yield action, node, progress
            executing += 1
            await execute(node, done.put_nowait)
        elif action is Action.RESULTS:
            yield action, None, progress
            extend_from(await done.get(), to_visit, filter=visited)
            executing -= 1
            executed += 1


def traverse(start: Iterable[_T],
             edges_from: Callable[[_T], Iterable[_T]],
             sentinel: Callable[[_T], bool] = None,
             depth: bool = False) -> Iterator[_T]:
    """Traverse a DAG, yield visited notes."""
    visited: Set[_T] = set()
    queue = Deque[_T]()
    queue.extend(start)
    while queue:
        n = queue.pop() if depth else queue.popleft()
        visited.add(n)
        yield n
        if sentinel and sentinel(n):
            continue
        queue.extend(m for m in edges_from(n) if m not in visited)


def traverse_id(start: Iterable[_T],
                edges_from: Callable[[_T], Iterable[_T]]) -> Iterable[_T]:
    table: Dict[int, _T] = {}

    def ids_from(ns: Iterable[_T]) -> Iterable[int]:
        update = {id(n): n for n in ns}
        table.update(update)
        return update.keys()

    def edges_from_id(n: int) -> Iterable[int]:
        xs = edges_from(table[n])
        update = {id(x): x for x in xs}
        table.update(update)
        return update.keys()

    for n in traverse(ids_from(start), edges_from_id):
        yield table[n]
