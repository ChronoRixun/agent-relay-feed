#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 ChronoRixun
"""Start a process outside the caller's Windows job object, with the caller's environment.

delegate.py starts this script through WMI (Win32_Process.Create), so it is created outside the
caller's job object. It connects back to the caller over an authenticated named pipe, receives the
command, working directory and full environment (in memory, never on disk), starts that command as
its own child (which is therefore outside the job too), sends back the child's process id and exits.

    python detach.py <pipe address> <hex auth key>
"""
import subprocess
import sys
from multiprocessing.connection import Client

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def main(address, key_hex):
    with Client(address, family="AF_PIPE", authkey=bytes.fromhex(key_hex)) as conn:
        order = conn.recv()
        try:
            proc = subprocess.Popen(order["cmd"], cwd=order["cwd"], env=order["env"], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                                    creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW)
            conn.send(dict(pid=proc.pid))
        except OSError as e:
            conn.send(dict(error=repr(e)))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
