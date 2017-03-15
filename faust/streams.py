import asyncio
from typing import (
    Any, AsyncIterable, Awaitable, Callable, MutableMapping, Tuple, cast,
)
from .event import FieldDescriptor, from_tuple
from .transport.base import Consumer
from .types import AppT, K, V, Message, Topic
from .utils.service import Service


class stream:

    def __init__(self, topic: Topic,
                 group_by: FieldDescriptor = None,
                 **kwargs) -> None:
        self.topic = topic
        self.group_by = group_by

    def __call__(self, fun: Callable) -> 'Stream':
        return AsyncGeneratorStream(
            topic=self.topic,
            group_by=self.group_by,
            callback=fun,
        )


class Stream(Service):

    app: AppT = None
    _consumer: Consumer = None

    def __init__(self, name: str = None,
                 topic: Topic = None,
                 group_by: FieldDescriptor = None,
                 callback: Callable = None,
                 app: AppT = None,
                 loop: asyncio.AbstractEventLoop = None) -> None:
        self.app = app
        self.name = name
        self.topic = topic
        self.group_by = group_by
        self.callback = callback
        self.type = self.topic.type
        self._quick_deserialize_k = self.topic.key_serializer
        self._quick_deserialize_v = self.topic.value_serializer
        super().__init__(loop=loop)

    def bind(self, app: AppT) -> 'Stream':
        stream = self.clone(name=app.new_stream_name(), app=app)
        app.add_source(stream)
        return stream

    def clone(self, **kwargs) -> 'Stream':
        defaults = {
            'name': self.name,
            'topic': self.topic,
            'group_by': self.group_by,
            'callback': self.callback,
            'loop': self.loop,
        }
        final = {**defaults, **kwargs}
        return self.__class__(**final)

    async def process(self, key: K, value: V) -> V:
        print('Received K/V: %r %r' % (key, value))
        if self.callback is not None:
            self.callback(key, value)
        return value

    async def on_start(self) -> None:
        if self.app is None:
            raise RuntimeError('Cannot start stream not bound to app.')
        self._consumer = self.get_consumer()
        await self._consumer.start()

    async def on_stop(self) -> None:
        if self._consumer is not None:
            await self._consumer.stop()

    async def on_message(self,
                         topic: str,
                         partition: str,
                         message: Message) -> None:
        print('Received message: %r' % (message,))
        k, v = self.to_KV(message)
        await self.process(k, v)

    def to_KV(self, message: Message) -> Tuple[K, V]:
        key = message.key
        if self._quick_deserialize_k:
            key = self._quick_deserialize_k(message.key)
        value = message.value
        if self._quick_deserialize_v:
            value = self._quick_deserialize_v(message.value)
        k = cast(K, key)
        return k, cast(V, from_tuple(self.type, k, value))

    def get_consumer(self) -> Consumer:
        return self.app.transport.create_consumer(
            topic=self.topic,
            callback=self.on_message,
        )

    def __copy__(self) -> 'Stream':
        return self.clone()


class Table(Stream):

    #: This maintains the state of the stream (the K/V store).
    _state: MutableMapping

    def on_init(self) -> None:
        self._state = {}

    async def on_message(self,
                         topic: str,
                         partition: str,
                         message: Message) -> None:
        k, v = self.to_KV(message)
        self._state[k] = await self.process(k, v)

    def __getitem__(self, key: Any) -> Any:
        return self._state[key]

    def __setitem__(self, key: Any, value: Any) -> None:
        self._state[key] = value

    def __delitem__(self, key: Any) -> None:
        del self._state[key]


class AsyncInputStream(AsyncIterable):

    queue: asyncio.Queue

    def __init__(self, stream: 'Stream', *,
                 loop: asyncio.AbstractEventLoop = None) -> None:
        self.stream = stream
        self.loop = loop or asyncio.get_event_loop()
        self.queue = asyncio.Queue(loop=self.loop)

    async def put(self, value: V) -> None:
        await self.queue.put(value)

    async def __aiter__(self) -> 'AsyncIterable':
        return self

    async def __anext__(self) -> Awaitable:
        return await self.queue.get()


class AsyncGeneratorStream(Stream):

    inbox: AsyncInputStream
    outbox: asyncio.Queue

    def on_init(self) -> None:
        self.inbox = AsyncInputStream(self, loop=self.loop)
        self.outbox = asyncio.Queue(loop=self.loop)
        self.gen = self.callback(self.inbox)

    async def __aiter__(self) -> 'AsyncGeneratorStream':
        await self.maybe_start()
        return self

    async def __anext__(self) -> Awaitable:
        return await self.outbox.get()

    async def send(self, value: V) -> None:
        await self.inbox.put(value)
        new_value = await self.gen.__anext__()
        await self.outbox.put(new_value)

    async def process(self, key: K, value: V) -> None:
        print('Received K=%r V=%r' % (key, value))
        await self.send(value)
