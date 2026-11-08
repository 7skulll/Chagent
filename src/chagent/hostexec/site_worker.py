"""Run one website and forward container port 8080 to its actual listen port."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import signal


STATUS = Path("/cache/site/chagent-site.json")
LOG = Path("/cache/site/chagent-site.log")


def process_start(pid: int) -> str:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]


def write_status(**values):
    target = STATUS.with_suffix(".tmp")
    target.write_text(json.dumps(values), encoding="utf-8")
    target.replace(STATUS)


async def wait_ready(port: int, timeout: float = 25.0) -> bool:
    """True once the child answers on its own port.

    "running" used to mean "the proxy is bound", which happens several seconds
    before the site is. The controller checks HTTP as well now, but this file is
    what every reader trusts, so it must not claim readiness it does not have.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.2)
            continue
        writer.close()
        return True


async def transfer(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--command", required=True)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or args.port == 18080:
        parser.error("Site port must be 1–65535 and not the reserved proxy port 18080")
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    worker_start = process_start(os.getpid())
    workdir = os.getcwd()
    with LOG.open("ab", buffering=0) as log:
        child = await asyncio.create_subprocess_exec("/bin/sh", "-c", args.command,
            stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True)
        write_status(state="starting", worker_pid=os.getpid(), worker_start=worker_start, child_pid=child.pid,
                     actual_port=args.port, command=args.command, workdir=workdir)

        async def handle(client_reader, client_writer):
            try:
                target_reader, target_writer = await asyncio.open_connection("127.0.0.1", args.port)
            except OSError:
                # Say "not yet" rather than dropping the connection: a silent close
                # reaches the caller as a read timeout and looks like a hang.
                try:
                    client_writer.write(b"HTTP/1.1 503 Service Unavailable\r\n"
                                        b"Content-Length: 16\r\nConnection: close\r\n\r\n"
                                        b"site is starting")
                    await client_writer.drain()
                except OSError:
                    pass
                client_writer.close()
                return
            await asyncio.gather(transfer(client_reader, target_writer),
                                 transfer(target_reader, client_writer), return_exceptions=True)

        server = await asyncio.start_server(handle, "0.0.0.0", 18080)
        ready = await wait_ready(args.port)
        write_status(state="running" if ready else "failed", worker_pid=os.getpid(),
                     worker_start=worker_start, child_pid=child.pid, actual_port=args.port,
                     command=args.command, workdir=workdir)
        try:
            stopping = asyncio.Event()
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGTERM, stopping.set)
            waiter = asyncio.create_task(child.wait())
            stopper = asyncio.create_task(stopping.wait())
            done, _ = await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if stopper in done and child.returncode is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(waiter, 2)
                except TimeoutError:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    await waiter
            stopper.cancel()
        finally:
            server.close()
            await server.wait_closed()
            write_status(state="stopped", worker_pid=os.getpid(), worker_start=worker_start, child_pid=child.pid,
                         actual_port=args.port, command=args.command, workdir=workdir, exit_code=child.returncode)


if __name__ == "__main__":
    asyncio.run(main())
