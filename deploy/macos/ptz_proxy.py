"""Forward local device TCP connections to the LAN from inside OrbStack."""

import asyncio
import os


async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def handle(client_reader: asyncio.StreamReader,
                 client_writer: asyncio.StreamWriter, host: str, port: int) -> None:
    try:
        camera_reader, camera_writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=5)
        tasks = [asyncio.create_task(relay(client_reader, camera_writer)),
                 asyncio.create_task(relay(camera_reader, client_writer))]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for task in done:
            if task.exception():
                print(f"PTZ relay {host}:{port}: {task.exception()}", flush=True)
    except (OSError, asyncio.TimeoutError) as exc:
        print(f"PTZ target {host}:{port}: {exc}", flush=True)
    finally:
        client_writer.close()
        await client_writer.wait_closed()


async def main() -> None:
    servers = []
    for target in os.environ["PTZ_TARGETS"].split(","):
        listen, destination = target.split("=", 1)
        host, port = destination.rsplit(":", 1)
        server = await asyncio.start_server(
            lambda reader, writer, h=host, p=int(port): handle(reader, writer, h, p),
            "0.0.0.0", int(listen))
        servers.append(server)
        print(f"PTZ proxy listening on {listen} for {host}:{port}", flush=True)
    await asyncio.gather(*(server.serve_forever() for server in servers))


if __name__ == "__main__":
    asyncio.run(main())
