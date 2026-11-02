import asyncio
import struct

import httpx

from app import doh_dns


QUERY = bytes.fromhex('123401000001000000000000076578616d706c6503636f6d0000010001')
ANSWER = bytes.fromhex('123481800001000100000000076578616d706c6503636f6d0000010001c00c000100010000003c000401010101')


class ResponseStream(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        yield self.content


def dns_response(data):
    return httpx.Response(200, headers={'content-type': 'application/dns-message'}, stream=ResponseStream(data))


def test_dns_resolver_falls_back_after_timeout_without_logging_query():
    async def run():
        attempted = []
        def handler(req):
            attempted.append(req.url.host)
            if len(attempted) == 1:
                raise httpx.ConnectTimeout('unavailable')
            assert req.content == QUERY
            return dns_response(ANSWER)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await doh_dns.Resolver(client).resolve(QUERY) == ANSWER
        assert attempted == ['1.1.1.1', '8.8.8.8']
    asyncio.run(run())


def test_dns_resolver_failure_and_malformed_messages():
    async def run():
        def handler(req):
            raise httpx.ReadTimeout('timeout')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resolver = doh_dns.Resolver(client)
            answer = await resolver.resolve(QUERY, udp=True)
            assert answer[:2] == QUERY[:2]
            assert answer[3] & 0x0f == 2
            assert answer[12:] == QUERY[12:]
            assert await resolver.resolve(b'bad', udp=True) is None
            assert await resolver.resolve(QUERY + b'x' * doh_dns.MAX_UDP, udp=True) is None
    asyncio.run(run())


def test_dns_applies_whole_request_deadline(monkeypatch):
    async def run():
        attempts = []

        async def handler(req):
            attempts.append(req.url.host)
            if len(attempts) == 1:
                await asyncio.sleep(1)
            return dns_response(ANSWER)

        monkeypatch.setattr(doh_dns, 'TIMEOUT_SECONDS', 0.02)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            answer = await asyncio.wait_for(doh_dns.Resolver(client).resolve(QUERY), 0.5)
            assert answer == ANSWER
        assert attempts == ['1.1.1.1', '8.8.8.8']
    asyncio.run(run())


def test_dns_rejects_wrong_id_and_truncates_large_udp_response():
    async def run():
        calls = 0
        def handler(req):
            nonlocal calls
            calls += 1
            data = (b'\xff\xff' + ANSWER[2:]) if calls == 1 else ANSWER + b'x' * doh_dns.MAX_UDP
            return dns_response(data)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            answer = await doh_dns.Resolver(client).resolve(QUERY, udp=True)
            assert calls == 2
            assert answer[:2] == QUERY[:2]
            assert answer[2] & 0x02  # Truncation flag, allowing TCP retry.
            assert len(answer) == len(QUERY)
    asyncio.run(run())


def test_udp_and_persistent_tcp_forwarding():
    async def run():
        def handler(req):
            return dns_response(ANSWER)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            resolver = doh_dns.Resolver(client)
            class FakeTransport:
                def sendto(self, data, addr):
                    self.response = (data, addr)
            protocol = doh_dns.DNSDatagram(resolver)
            transport = FakeTransport()
            protocol.connection_made(transport)
            await protocol.answer(QUERY, ('127.0.0.1', 10000))
            assert transport.response == (ANSWER, ('127.0.0.1', 10000))

            class FakeWriter:
                def __init__(self):
                    self.output = b''
                def write(self, data):
                    self.output += data
                async def drain(self):
                    pass
                def close(self):
                    pass
                async def wait_closed(self):
                    pass
            reader = asyncio.StreamReader()
            reader.feed_data((struct.pack('!H', len(QUERY)) + QUERY) * 2)
            reader.feed_eof()
            writer = FakeWriter()
            await doh_dns.tcp_handler(resolver)(reader, writer)
            assert writer.output == (struct.pack('!H', len(ANSWER)) + ANSWER) * 2
    asyncio.run(run())
