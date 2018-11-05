import asyncio
import subprocess

import pytest  # type: ignore

from mona import Session, Rule, run_shell, run_process, run_thread
from mona.files import File
from mona.dirtask import dir_task
from mona.plugins import Parallel

from tests.test_dirtask import analysis


@Rule
async def shell():
    out, _ = await run_shell('expr `cat` "*" 2', input=b'2')
    return int(out)


@Rule
async def process():
    out, _ = await run_process('bash', '-c', 'expr `cat` "*" 2', input=b'2')
    return int(out)


@Rule
async def calcs(n):
    return [
        [
            dist,
            dir_task(
                File.from_str(
                    'script', f'#!/bin/bash\nexpr $(cat data) "*" 2; sleep {n}'
                ),
                [File.from_str('data', str(dist))],
                label=f'/calcs/dist={dist}',
            )['STDOUT'],
        ]
        for dist in range(5)
    ]


@Rule
async def error():
    return int('x')


def test_shell():
    with Session([Parallel()]) as sess:
        sess.eval(shell()) == 4


def test_process():
    with Session([Parallel()]) as sess:
        sess.eval(process()) == 4


def test_thread():
    @Rule
    async def f():
        return await run_thread(lambda: 4)

    with Session([Parallel()]) as sess:
        sess.eval(f()) == 4


def test_calc():
    with Session([Parallel()]) as sess:
        assert sess.eval(analysis(calcs(0))) == 20


def test_exception():
    def handler(task, exc):
        if isinstance(exc, subprocess.CalledProcessError):
            return True

    with Session([Parallel()]) as sess:
        sess.eval(analysis(calcs('x')), exception_handler=handler)


def test_cancelling():
    async def main():
        with Session([Parallel()]) as sess:
            task = asyncio.create_task(sess.eval_async(analysis(calcs(1))))
            await asyncio.sleep(0.05)
            task.cancel()
            await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main())
