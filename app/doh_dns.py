"""A small DNS-over-HTTPS forwarder for the Crawl4AI container.

Only fixed, TLS-verified public resolvers are used; query names are never logged.
This resolver does not grant permission to connect to any address: Crawl4AI's
SSRF validation and connection-time IP pinning remain independently enabled.
"""
import asyncio
import os
import struct

import httpx

UPSTREAMS = ("https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query")
MAX_UDP = 4096
MAX_TCP = 65535
MAX_CONCURRENT = 64
TIMEOUT_SECONDS = 3.0
IO_TIMEOUT_SECONDS = 8.0


def question_end(query: bytes) -> int | None:
    """Validate one ordinary DNS query and locate its question's end."""
    if len(query) < 17:
        return None
    _, flags, qdcount, ancount, nscount, _ = struct.unpack("!HHHHHH", query[:12])
    if flags & 0xF800 or qdcount != 1 or ancount or nscount:
        return None
    position = 12
    name_length = 0
    while position < len(query):
        length = query[position]
        position += 1
        name_length += length + 1
        if name_length > 255 or length > 63 or position + length > len(query):
            return None
        if length == 0:
            return position + 4 if position + 4 <= len(query) else None
        position += length
    return None


def error_response(query: bytes, question_stop: int, flags: int) -> bytes:
    # Preserve the transaction ID and RD bit, and return only the question.
    request_flags = struct.unpack_from("!H", query, 2)[0]
    header = struct.pack("!HHHHHH", int.from_bytes(query[:2], "big"),
                         0x8000 | 0x0080 | (request_flags & 0x0100) | flags,
                         1, 0, 0, 0)
    return header + query[12:question_stop]


class Resolver:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.slots = asyncio.Semaphore(MAX_CONCURRENT)

    async def resolve(self, query: bytes, udp: bool = False) -> bytes | None:
        limit = MAX_UDP if udp else MAX_TCP
        stop = question_end(query)
        if stop is None or len(query) > limit:
            return None
        if self.slots.locked():
            return error_response(query, stop, 2)  # SERVFAIL under load
        async with self.slots:
            for upstream in UPSTREAMS:
                try:
                    async with asyncio.timeout(TIMEOUT_SECONDS):
                        async with self.client.stream(
                            "POST", upstream, content=query,
                            headers={"content-type": "application/dns-message",
                                     "accept": "application/dns-message",
                                     "accept-encoding": "identity"},
                            timeout=TIMEOUT_SECONDS,
                        ) as response:
                            response.raise_for_status()
                            if response.headers.get("content-type", "").split(";")[0].strip().lower() != "application/dns-message":
                                continue
                            chunks = bytearray()
                            async for chunk in response.aiter_raw(chunk_size=4096):
                                chunks.extend(chunk)
                                if len(chunks) > MAX_TCP:
                                    break
                            answer = bytes(chunks)
                    if (len(answer) < 12 or len(answer) > MAX_TCP or answer[:2] != query[:2]
                            or not answer[2] & 0x80):
                        continue
                    if udp and len(answer) > MAX_UDP:
                        return error_response(query, stop, 0x0200)  # TC: retry over TCP
                    return answer
                except (httpx.HTTPError, OSError, TimeoutError):
                    continue
        return error_response(query, stop, 2)


class DNSDatagram(asyncio.DatagramProtocol):
    def __init__(self, resolver: Resolver):
        self.resolver = resolver
        self.tasks: set[asyncio.Task] = set()
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        # Bound the number of scheduled tasks as well as active HTTP requests.
        if len(self.tasks) >= MAX_CONCURRENT or len(data) > MAX_UDP:
            return
        task = asyncio.create_task(self.answer(data, addr))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def answer(self, data, addr):
        answer = await self.resolver.resolve(data, udp=True)
        if answer is not None:
            self.transport.sendto(answer, addr)


def tcp_handler(resolver: Resolver):
    connections = asyncio.Semaphore(MAX_CONCURRENT)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        if connections.locked():
            writer.close()
            return
        async with connections:
            try:
                # Support persistent DNS-over-TCP clients with bounded idle time.
                while True:
                    header = await asyncio.wait_for(reader.readexactly(2), IO_TIMEOUT_SECONDS)
                    length = int.from_bytes(header, "big")
                    if length == 0 or length > MAX_TCP:
                        break
                    query = await asyncio.wait_for(reader.readexactly(length), IO_TIMEOUT_SECONDS)
                    answer = await resolver.resolve(query)
                    if answer is None:
                        break
                    writer.write(len(answer).to_bytes(2, "big") + answer)
                    await asyncio.wait_for(writer.drain(), IO_TIMEOUT_SECONDS)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
    return handle


async def serve(host: str = "0.0.0.0", port: int = 53):
    # Do not inherit HTTP_PROXY, HTTPS_PROXY, SSL_CERT_FILE or other host settings.
    # Verification stays enabled and pinned upstreams are HTTPS IP literals.
    async with httpx.AsyncClient(trust_env=False, verify=True, follow_redirects=False) as client:
        resolver = Resolver(client)
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: DNSDatagram(resolver), local_addr=(host, port))
        try:
            async with await asyncio.start_server(tcp_handler(resolver), host, port) as server:
                await server.serve_forever()
        finally:
            transport.close()


if __name__ == "__main__":
    asyncio.run(serve(port=int(os.environ.get("DNS_PORT", "53"))))
