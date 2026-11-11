"""A plain TCP relay for stack mode (compose/stack.yaml).

RAGFlow keeps calling vllm-llm, vllm-embed and vllm-rerank on port 8000. In stack mode those
models run behind the direct QSFP cable (the head's own cable address, or the node), and each of
these names is a relay that passes every connection on, byte for byte. The relay holds no state and
reads nothing; RAGFlow's model settings stay the same in single and stack mode.

Usage: python3 relay.py LISTEN_PORT TARGET_HOST TARGET_PORT
"""
import asyncio
import sys

CHUNK = 1 << 16


async def pipe(reader, writer):
    """Copy one direction until it closes, then close the other side's writer."""
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except (ConnectionError, OSError):
            pass


def handler(host, port):
    async def handle(client_reader, client_writer):
        try:
            target_reader, target_writer = await asyncio.open_connection(host, port)
        except OSError:
            client_writer.close()          # the caller sees a refused request, as from a stopped model
            return
        await asyncio.gather(pipe(client_reader, target_writer), pipe(target_reader, client_writer))
    return handle


async def serve(listen_port, host, port, ready=None):
    server = await asyncio.start_server(handler(host, port), "0.0.0.0", listen_port)
    print("relay 0.0.0.0:%d -> %s:%d" % (listen_port, host, port), flush=True)
    if ready is not None:
        ready.set_result(server.sockets[0].getsockname()[1])
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit("usage: relay.py LISTEN_PORT TARGET_HOST TARGET_PORT")
    asyncio.run(serve(int(sys.argv[1]), sys.argv[2], int(sys.argv[3])))
