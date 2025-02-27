# piker: trading gear for hackers
# Copyright (C) Tyler Goodlet (in stewardship for piker0)

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Data feed apis and infra.

This module is enabled for ``brokerd`` daemons.

"""
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from functools import partial
from types import ModuleType
from typing import (
    Any, Sequence,
    AsyncIterator, Optional,
    Awaitable, Callable,
)

import trio
from trio_typing import TaskStatus
import tractor
from pydantic import BaseModel

from ..brokers import get_brokermod
from ..log import get_logger, get_console_log
from .._daemon import (
    maybe_spawn_brokerd,
)
from ._sharedmem import (
    maybe_open_shm_array,
    attach_shm_array,
    ShmArray,
)
from .ingest import get_ingestormod
from ._source import base_iohlc_dtype, Symbol
from ..ui import _search
from ._sampling import (
    _shms,
    _incrementers,
    increment_ohlc_buffer,
    iter_ohlc_periods,
    sample_and_broadcast,
    uniform_rate_send,
)


log = get_logger(__name__)


class _FeedsBus(BaseModel):
    """Data feeds broadcaster and persistence management.

    This is a brokerd side api used to manager persistent real-time
    streams that can be allocated and left alive indefinitely.

    """
    brokername: str
    nursery: trio.Nursery
    feeds: dict[str, trio.CancelScope] = {}

    task_lock: trio.StrictFIFOLock = trio.StrictFIFOLock()

    # XXX: so weird but, apparently without this being `._` private
    # pydantic will complain about private `tractor.Context` instance
    # vars (namely `._portal` and `._cancel_scope`) at import time.
    # Reported this bug:
    # https://github.com/samuelcolvin/pydantic/issues/2816
    _subscribers: dict[
        str,
        list[tuple[tractor.MsgStream, Optional[float]]]
    ] = {}

    class Config:
        arbitrary_types_allowed = True
        underscore_attrs_are_private = False

    async def cancel_all(self) -> None:
        for sym, (cs, msg, quote) in self.feeds.items():
            log.debug(f'Cancelling cached feed for {self.brokername}:{sym}')
            cs.cancel()


_bus: _FeedsBus = None


def get_feed_bus(
    brokername: str,
    nursery: Optional[trio.Nursery] = None,
) -> _FeedsBus:
    """
    Retreive broker-daemon-local data feeds bus from process global
    scope. Serialize task access to lock.

    """

    global _bus

    if nursery is not None:
        assert _bus is None, "Feeds manager is already setup?"

        # this is initial setup by parent actor
        _bus = _FeedsBus(
            brokername=brokername,
            nursery=nursery,
        )
        assert not _bus.feeds

    assert _bus.brokername == brokername, "Uhhh wtf"
    return _bus


@tractor.context
async def _setup_persistent_brokerd(
    ctx: tractor.Context,
    brokername: str
) -> None:
    """Allocate a actor-wide service nursery in ``brokerd``
    such that feeds can be run in the background persistently by
    the broker backend as needed.

    """
    try:
        async with trio.open_nursery() as service_nursery:

            # assign a nursery to the feeds bus for spawning
            # background tasks from clients
            bus = get_feed_bus(brokername, service_nursery)

            # unblock caller
            await ctx.started()

            # we pin this task to keep the feeds manager active until the
            # parent actor decides to tear it down
            await trio.sleep_forever()
    finally:
        # TODO: this needs to be shielded?
        await bus.cancel_all()


async def allocate_persistent_feed(

    bus: _FeedsBus,
    brokername: str,
    symbol: str,
    loglevel: str,
    task_status: TaskStatus[trio.CancelScope] = trio.TASK_STATUS_IGNORED,

) -> None:

    try:
        mod = get_brokermod(brokername)
    except ImportError:
        mod = get_ingestormod(brokername)

    # allocate shm array for this broker/symbol
    # XXX: we should get an error here if one already exists

    shm, opened = maybe_open_shm_array(
        key=sym_to_shm_key(brokername, symbol),

        # use any broker defined ohlc dtype:
        dtype=getattr(mod, '_ohlc_dtype', base_iohlc_dtype),

        # we expect the sub-actor to write
        readonly=False,
    )

    # do history validation?
    # assert opened, f'Persistent shm for {symbol} was already open?!'
    # if not opened:
    #     raise RuntimeError("Persistent shm for sym was already open?!")

    send, quote_stream = trio.open_memory_channel(10)
    feed_is_live = trio.Event()

    # establish broker backend quote stream
    # ``stream_quotes()`` is a required backend func
    init_msg, first_quote = await bus.nursery.start(
        partial(
            mod.stream_quotes,
            send_chan=send,
            feed_is_live=feed_is_live,
            symbols=[symbol],
            shm=shm,
            loglevel=loglevel,
        )
    )

    init_msg[symbol]['shm_token'] = shm.token
    cs = bus.nursery.cancel_scope

    # TODO: make this into a composed type which also
    # contains the backfiller cs for individual super-based
    # resspawns when needed.

    # XXX: the ``symbol`` here is put into our native piker format (i.e.
    # lower case).
    bus.feeds[symbol.lower()] = (cs, init_msg, first_quote)

    if opened:
        # start history backfill task ``backfill_bars()`` is
        # a required backend func this must block until shm is
        # filled with first set of ohlc bars
        await bus.nursery.start(mod.backfill_bars, symbol, shm)

    times = shm.array['time']
    delay_s = times[-1] - times[times != times[-1]][-1]

    # pass OHLC sample rate in seconds (be sure to use python int type)
    init_msg[symbol]['sample_rate'] = int(delay_s)

    # yield back control to starting nursery
    task_status.started((init_msg,  first_quote))

    await feed_is_live.wait()

    if opened:
        _shms.setdefault(delay_s, []).append(shm)

        # start shm incrementing for OHLC sampling
        if _incrementers.get(delay_s) is None:
            cs = await bus.nursery.start(increment_ohlc_buffer, delay_s)

    sum_tick_vlm: bool = init_msg.get(
        'shm_write_opts', {}
    ).get('sum_tick_vlm', True)

    # start sample loop
    await sample_and_broadcast(bus, shm, quote_stream, sum_tick_vlm)


@tractor.context
async def attach_feed_bus(

    ctx: tractor.Context,
    brokername: str,
    symbol: str,
    loglevel: str,
    tick_throttle:  Optional[float] = None,

) -> None:

    if loglevel is None:
        loglevel = tractor.current_actor().loglevel

    # XXX: required to propagate ``tractor`` loglevel to piker logging
    get_console_log(loglevel or tractor.current_actor().loglevel)

    # ensure we are who we think we are
    assert 'brokerd' in tractor.current_actor().name

    bus = get_feed_bus(brokername)

    entry = bus.feeds.get(symbol)

    bus._subscribers.setdefault(symbol, [])

    # if no cached feed for this symbol has been created for this
    # brokerd yet, start persistent stream and shm writer task in
    # service nursery
    async with bus.task_lock:
        if entry is None:
            init_msg, first_quote = await bus.nursery.start(
                partial(
                    allocate_persistent_feed,

                    bus=bus,
                    brokername=brokername,

                    # here we pass through the selected symbol in native
                    # "format" (i.e. upper vs. lowercase depending on
                    # provider).
                    symbol=symbol,

                    loglevel=loglevel,
                )
            )
            assert isinstance(bus.feeds[symbol], tuple)

    # XXX: ``first_quote`` may be outdated here if this is secondary
    # subscriber
    cs, init_msg, first_quote = bus.feeds[symbol]

    # send this even to subscribers to existing feed?
    # deliver initial info message a first quote asap
    await ctx.started((init_msg, first_quote))

    async with (
        ctx.open_stream() as stream,
        trio.open_nursery() as n,
    ):

        if tick_throttle:

            # open a bg task which receives quotes over a mem chan
            # and only pushes them to the target actor-consumer at
            # a max ``tick_throttle`` instantaneous rate.

            send, recv = trio.open_memory_channel(2**10)
            n.start_soon(
                uniform_rate_send,
                tick_throttle,
                recv,
                stream,
            )
            sub = (send, tick_throttle)

        else:
            sub = (stream, tick_throttle)

        bus._subscribers[symbol].append(sub)

        try:
            await trio.sleep_forever()

        finally:
            log.info(
                f'Stopping {symbol}.{brokername} feed for {ctx.chan.uid}')
            if tick_throttle:
                n.cancel_scope.cancel()
            bus._subscribers[symbol].remove(sub)


@dataclass
class Feed:
    """A data feed for client-side interaction with far-process# }}}
    real-time data sources.

    This is an thin abstraction on top of ``tractor``'s portals for
    interacting with IPC streams and conducting automatic
    memory buffer orchestration.
    """
    name: str
    stream: AsyncIterator[dict[str, Any]]
    shm: ShmArray
    mod: ModuleType
    first_quote: dict

    _brokerd_portal: tractor._portal.Portal
    _index_stream: Optional[AsyncIterator[int]] = None
    _trade_stream: Optional[AsyncIterator[dict[str, Any]]] = None
    _max_sample_rate: int = 0

    search: Callable[..., Awaitable] = None

    # cache of symbol info messages received as first message when
    # a stream startsc.
    symbols: dict[str, Symbol] = field(default_factory=dict)

    async def receive(self) -> dict:
        return await self.stream.__anext__()

    @asynccontextmanager
    async def index_stream(
        self,
        delay_s: Optional[int] = None

    ) -> AsyncIterator[int]:

        if not self._index_stream:
            # XXX: this should be singleton on a host,
            # a lone broker-daemon per provider should be
            # created for all practical purposes
            async with self._brokerd_portal.open_stream_from(
                iter_ohlc_periods,
                delay_s=delay_s or self._max_sample_rate,
            ) as self._index_stream:

                yield self._index_stream
        else:
            yield self._index_stream


def sym_to_shm_key(
    broker: str,
    symbol: str,
) -> str:
    return f'{broker}.{symbol}'


@asynccontextmanager
async def install_brokerd_search(

    portal: tractor._portal.Portal,
    brokermod: ModuleType,

) -> None:

    async with portal.open_context(
        brokermod.open_symbol_search
    ) as (ctx, cache):

        # shield here since we expect the search rpc to be
        # cancellable by the user as they see fit.
        async with ctx.open_stream() as stream:

            async def search(text: str) -> dict[str, Any]:
                await stream.send(text)
                return await stream.receive()

            async with _search.register_symbol_search(

                provider_name=brokermod.name,
                search_routine=search,

                # TODO: should be make this a required attr of
                # a backend module?
                pause_period=getattr(
                    brokermod, '_search_conf', {}
                    ).get('pause_period', 0.0616),
            ):
                yield


@asynccontextmanager
async def open_feed(

    brokername: str,
    symbols: Sequence[str],
    loglevel: Optional[str] = None,

    tick_throttle: Optional[float] = None,  # Hz

) -> AsyncIterator[dict[str, Any]]:
    '''
    Open a "data feed" which provides streamed real-time quotes.

    '''
    sym = symbols[0].lower()

    # TODO: feed cache locking, right now this is causing
    # issues when reconnecting to a long running emsd?
    # global _searcher_cache

    # async with _cache_lock:
    #     feed = _searcher_cache.get((brokername, sym))

    #     # if feed is not None and sym in feed.symbols:
    #     if feed is not None:
    #         yield feed
    #         # short circuit
    #         return

    try:
        mod = get_brokermod(brokername)
    except ImportError:
        mod = get_ingestormod(brokername)

    # no feed for broker exists so maybe spawn a data brokerd

    async with (

        maybe_spawn_brokerd(
            brokername,
            loglevel=loglevel
        ) as portal,

        portal.open_context(

            attach_feed_bus,
            brokername=brokername,
            symbol=sym,
            loglevel=loglevel,

            tick_throttle=tick_throttle,

        ) as (ctx, (init_msg, first_quote)),

        ctx.open_stream() as stream,
    ):

        # we can only read from shm
        shm = attach_shm_array(
            token=init_msg[sym]['shm_token'],
            readonly=True,
        )

        feed = Feed(
            name=brokername,
            stream=stream,
            shm=shm,
            mod=mod,
            first_quote=first_quote,
            _brokerd_portal=portal,
        )
        ohlc_sample_rates = []

        for sym, data in init_msg.items():

            si = data['symbol_info']
            ohlc_sample_rates.append(data['sample_rate'])

            symbol = Symbol(
                key=sym,
                type_key=si.get('asset_type', 'forex'),
                tick_size=si.get('price_tick_size', 0.01),
                lot_tick_size=si.get('lot_tick_size', 0.0),
            )
            symbol.broker_info[brokername] = si

            feed.symbols[sym] = symbol

            # cast shm dtype to list... can't member why we need this
            shm_token = data['shm_token']

            # XXX: msgspec won't relay through the tuples XD
            shm_token['dtype_descr'] = list(
                map(tuple, shm_token['dtype_descr']))

            assert shm_token == shm.token  # sanity

        feed._max_sample_rate = max(ohlc_sample_rates)

        try:
            yield feed
        finally:
            # drop the infinite stream connection
            await ctx.cancel()
