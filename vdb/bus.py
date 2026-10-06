import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

log = logging.getLogger(__name__)
Handler = Callable[[str, bytes], Awaitable[None]]
STREAM = "VDB"
STREAM_MAX_AGE_SECONDS = 24 * 3600


class Bus(Protocol):
    async def publish(self, subject: str, data: bytes) -> None: ...
    async def subscribe(self, prefix: str, handler: Handler, durable: str | None = None) -> None: ...
    async def close(self) -> None: ...


class MemoryBus:
    """In-process bus for running everything in one process (local dev, tests)."""

    def __init__(self) -> None:
        self._subs: list[tuple[str, Handler]] = []

    async def publish(self, subject: str, data: bytes) -> None:
        for prefix, handler in self._subs:
            if subject.startswith(prefix):
                try:
                    await handler(subject, data)
                except Exception:
                    log.exception("handler for %s failed", subject)

    async def subscribe(self, prefix: str, handler: Handler, durable: str | None = None) -> None:
        self._subs.append((prefix, handler))

    async def close(self) -> None:
        self._subs.clear()


class NatsBus:
    """NATS JetStream: messages are stored for a day, and a durable subscriber that was down (e.g. the API
    restarting) receives everything it missed. A message is acked only after its handler succeeded."""

    def __init__(self, nc, js) -> None:
        self._nc = nc
        self._js = js

    @classmethod
    async def connect(cls, url: str) -> "NatsBus":
        import nats
        from nats.js.api import StreamConfig

        nc = await nats.connect(url, max_reconnect_attempts=-1)
        js = nc.jetstream()
        config = StreamConfig(name=STREAM, subjects=["vdb.>"], max_age=STREAM_MAX_AGE_SECONDS)
        try:
            await js.add_stream(config)
        except Exception:
            await js.update_stream(config)
        return cls(nc, js)

    async def publish(self, subject: str, data: bytes) -> None:
        await self._js.publish(subject, data)

    async def subscribe(self, prefix: str, handler: Handler, durable: str | None = None) -> None:
        async def on_msg(msg) -> None:
            try:
                await handler(msg.subject, msg.data)
            except Exception:
                log.exception("handler for %s failed; message will be redelivered", msg.subject)
                return
            await msg.ack()

        await self._js.subscribe(prefix + ">", durable=durable, cb=on_msg, manual_ack=True)

    async def close(self) -> None:
        await self._nc.drain()


async def make_bus(url: str | None) -> Bus:
    return await NatsBus.connect(url) if url else MemoryBus()
