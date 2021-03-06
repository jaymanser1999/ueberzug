#!/usr/bin/env python3
"""Usage:
    ueberzug ROUTINE [options]

Routines:
    layer                   Display images
    library                 Prints the path to the bash library

Image options:
    -p, --parser <parser>  one of json, simple, bash
                           json: Json-Object per line
                           simple: Key-Values separated by a tab
                           bash: associative array dumped via `declare -p`
                           [default: json]
    -s, --silent           print stderr to /dev/null


License:
    ueberzug  Copyright (C) 2018  Nico Baeurer
    This program comes with ABSOLUTELY NO WARRANTY.
    This is free software, and you are welcome to redistribute it
    under certain conditions.
"""
import atexit
import sys
import os
import asyncio
import signal
import pathlib
import tempfile

import docopt

import ueberzug.thread as thread
import ueberzug.files as files
import ueberzug.xutil as xutil
import ueberzug.parser as parser
import ueberzug.ui as ui
import ueberzug.batch as batch
import ueberzug.action as action
import ueberzug.result as result
import ueberzug.tmux_util as tmux_util


async def main_xevents(loop, display, windows):
    """Coroutine which processes X11 events"""
    async for event in xutil.Events(loop, display):
        windows.process_event(event)


async def main_commands(loop, shutdown_routine_factory,
                        parser_object, windows, view):
    """Coroutine which processes the input of stdin"""
    try:
        async for line in files.LineReader(loop, sys.stdin):
            if not line:
                break

            try:
                data = parser_object.parse(line[:-1])
                command = action.Command(data['action'])
                command.action_class(**data) \
                    .apply(parser_object, windows, view)
            except (OSError, KeyError, ValueError, TypeError) as error:
                result.ErrorResult(error) \
                    .print(parser_object)
    finally:
        asyncio.ensure_future(shutdown_routine_factory())


async def query_windows(window_factory, windows, view):
    """Signal handler for SIGUSR1.
    Searches for added and removed tmux clients.
    Added clients: additional windows will be mapped
    Removed clients: existing windows will be destroyed
    """
    parent_window_infos = xutil.get_parent_window_infos()
    view.offset = tmux_util.get_offset()
    map_parent_window_id_info = {info.window_id: info
                                 for info in parent_window_infos}
    parent_window_ids = map_parent_window_id_info.keys()
    map_current_windows = {window.parent_window.id: window
                           for window in windows}
    current_window_ids = map_current_windows.keys()
    diff_window_ids = parent_window_ids ^ current_window_ids
    added_window_ids = diff_window_ids & parent_window_ids
    removed_window_ids = diff_window_ids & current_window_ids
    draw = added_window_ids or removed_window_ids

    if added_window_ids:
        windows += window_factory.create(*[
            map_parent_window_id_info.get(wid)
            for wid in added_window_ids
        ])

    if removed_window_ids:
        windows -= [
            map_current_windows.get(wid)
            for wid in removed_window_ids
        ]

    if draw:
        windows.draw()


async def shutdown(loop):
    tasks = [task for task in asyncio.Task.all_tasks()
             if task is not asyncio.tasks.Task.current_task()]
    list(map(lambda task: task.cancel(), tasks))
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()


def shutdown_factory(loop):
    return lambda: asyncio.ensure_future(shutdown(loop))


def setup_tmux_hooks():
    """Registers tmux hooks which are
    required to notice a change in the visibility
    of the pane this program runs in.
    Also it's required to notice new tmux clients
    displaying our pane.

    Returns:
        function which unregisters the registered hooks
    """
    events = (
        'client-session-changed',
        'session-window-changed',
        'pane-mode-changed'
    )
    lock_directory_path = pathlib.PosixPath(tempfile.gettempdir()) / 'ueberzug'
    lock_file_path = lock_directory_path / tmux_util.get_session_id()
    own_pid = str(os.getpid())
    command_template = 'kill -USR1 '

    try:
        lock_directory_path.mkdir()
    except FileExistsError:
        pass

    def update_hooks(pid_file, pids):
        pids = ' '.join(pids)
        command = command_template + pids

        pid_file.seek(0)
        pid_file.truncate()
        pid_file.write(pids)
        pid_file.flush()

        for event in events:
            if pids:
                tmux_util.register_hook(event, command)
            else:
                tmux_util.unregister_hook(event)

    def remove_hooks():
        """Removes the hooks registered by the outer function."""
        with files.lock(lock_file_path) as lock_file:
            pids = set(lock_file.read().split())
            pids.discard(own_pid)
            update_hooks(lock_file, pids)

    with files.lock(lock_file_path) as lock_file:
        pids = set(lock_file.read().split())
        pids.add(own_pid)
        update_hooks(lock_file, pids)

    return remove_hooks


def main_layer(options):
    display = xutil.get_display()
    window_infos = xutil.get_parent_window_infos()
    loop = asyncio.get_event_loop()
    executor = thread.DaemonThreadPoolExecutor(max_workers=2)
    parser_class = parser.ParserOption(options['--parser']).parser_class
    view = ui.View()
    window_factory = ui.OverlayWindow.Factory(display, view)
    windows = batch.BatchList(window_factory.create(*window_infos))

    if tmux_util.is_used():
        atexit.register(setup_tmux_hooks())
        view.offset = tmux_util.get_offset()

    if options['--silent']:
        sys.stderr = open('/dev/null', 'w')

    with windows:
        loop.set_default_executor(executor)

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig, shutdown_factory(loop))

        loop.add_signal_handler(
            signal.SIGUSR1,
            lambda: asyncio.ensure_future(query_windows(
                window_factory, windows, view)))

        asyncio.ensure_future(main_xevents(loop, display, windows))
        asyncio.ensure_future(main_commands(
            loop, shutdown_factory(loop), parser_class(),
            windows, view))

        try:
            loop.run_forever()
        finally:
            loop.close()
            executor.shutdown(wait=False)


def main_library():
    directory = \
        pathlib.PosixPath(os.path.abspath(os.path.dirname(__file__))) / 'lib'
    print((directory / 'lib.sh').as_posix())


def main():
    options = docopt.docopt(__doc__)
    routine = options['ROUTINE']

    if routine == 'layer':
        main_layer(options)
    elif routine == 'library':
        main_library()


if __name__ == '__main__':
    main()
