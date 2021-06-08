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
In da suit parlances: "Execution management systems"

"""
from pprint import pformat
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

from bidict import bidict
from pydantic import BaseModel
import trio
import tractor

from .. import data
from ..log import get_logger
from ..data._normalize import iterticks
from . import _paper_engine as paper
from ._messages import (
    Status, Order,
    BrokerdCancel, BrokerdOrder, BrokerdOrderAck, BrokerdStatus,
    BrokerdFill, BrokerdError, BrokerdPosition,
)


log = get_logger(__name__)


# TODO: numba all of this
def mk_check(
    trigger_price: float,
    known_last: float,
    action: str,
) -> Callable[[float, float], bool]:
    """Create a predicate for given ``exec_price`` based on last known
    price, ``known_last``.

    This is an automatic alert level thunk generator based on where the
    current last known value is and where the specified value of
    interest is; pick an appropriate comparison operator based on
    avoiding the case where the a predicate returns true immediately.

    """
    # str compares:
    # https://stackoverflow.com/questions/46708708/compare-strings-in-numba-compiled-function

    if trigger_price >= known_last:

        def check_gt(price: float) -> bool:
            return price >= trigger_price

        return check_gt

    elif trigger_price <= known_last:

        def check_lt(price: float) -> bool:
            return price <= trigger_price

        return check_lt

    else:
        return None


@dataclass
class _DarkBook:
    """Client-side execution book.

    Contains conditions for executions (aka "orders") which are not
    exposed to brokers and thus the market; i.e. these are privacy
    focussed "client side" orders.

    A singleton instance is created per EMS actor (for now).

    """
    broker: str

    # levels which have an executable action (eg. alert, order, signal)
    orders: dict[
        str,  # symbol
        dict[
            str,  # uuid
            tuple[
                Callable[[float], bool],  # predicate
                str,  # name
                dict,  # cmd / msg type
            ]
        ]
    ] = field(default_factory=dict)

    # tracks most recent values per symbol each from data feed
    lasts: dict[
        tuple[str, str],
        float
    ] = field(default_factory=dict)

    # mapping of piker ems order ids to current brokerd order flow message
    _ems_entries: dict[str, str] = field(default_factory=dict)
    _ems2brokerd_ids: dict[str, str] = field(default_factory=bidict)


# XXX: this is in place to prevent accidental positions that are too
# big. Now obviously this won't make sense for crypto like BTC, but
# for most traditional brokers it should be fine unless you start
# slinging NQ futes or something.
_DEFAULT_SIZE: float = 1.0


async def clear_dark_triggers(

    # ctx: tractor.Context,
    brokerd_orders_stream: tractor.MsgStream,
    ems_client_order_stream: tractor.MsgStream,
    quote_stream: tractor.ReceiveMsgStream,  # noqa

    broker: str,
    symbol: str,
    # client: 'Client',  # noqa
    # order_msg_stream: 'Client',  # noqa

    book: _DarkBook,

) -> None:
    """Core dark order trigger loop.

    Scan the (price) data feed and submit triggered orders
    to broker.

    """
    # this stream may eventually contain multiple symbols
    # XXX: optimize this for speed!
    async for quotes in quote_stream:

        # TODO: numba all this!

        # start = time.time()
        for sym, quote in quotes.items():

            execs = book.orders.get(sym, None)
            if execs is None:
                continue

            for tick in iterticks(
                quote,
                # dark order price filter(s)
                types=('ask', 'bid', 'trade', 'last')
            ):
                price = tick.get('price')
                ttype = tick['type']

                # update to keep new cmds informed
                book.lasts[(broker, symbol)] = price

                for oid, (
                    pred,
                    tf,
                    cmd,
                    percent_away,
                    abs_diff_away
                ) in (
                    tuple(execs.items())
                ):

                    if not pred or (ttype not in tf) or (not pred(price)):
                        # majority of iterations will be non-matches
                        continue

                    action: str = cmd['action']
                    symbol: str = cmd['symbol']

                    if action == 'alert':
                        # nothing to do but relay a status
                        # message back to the requesting ems client
                        resp = 'alert_triggered'

                    else:
                        # executable order submission

                        # submit_price = price + price*percent_away
                        submit_price = price + abs_diff_away

                        log.info(
                            f'Dark order triggered for price {price}\n'
                            f'Submitting order @ price {submit_price}')

                        # TODO: port to BrokerdOrder message sending
                        msg = BrokerdOrder(
                            action=cmd['action'],
                            oid=oid,
                            time_ns=time.time_ns(),


                            # this is a brand new order request for the
                            # underlying broker so we set a "broker
                            # request id" (brid) to "nothing" so that the
                            # broker client knows that we aren't trying
                            # to modify an existing order-request.
                            reqid=None,

                            symbol=sym,
                            price=submit_price,
                            size=cmd['size'],
                        )
                        await brokerd_orders_stream.send(msg.dict())
                        # mark this entry as having send an order request
                        book._ems_entries[oid] = msg

                        resp = 'dark_triggered'

                    msg = Status(
                        oid=oid,  # piker order id
                        resp=resp,
                        time_ns=time.time_ns(),

                        symbol=symbol,
                        trigger_price=price,

                        broker_details={'name': broker},

                        cmd=cmd,  # original request message

                    ).dict()

                    # remove exec-condition from set
                    log.info(f'removing pred for {oid}')
                    execs.pop(oid)

                    await ems_client_order_stream.send(msg)

                else:  # condition scan loop complete
                    log.debug(f'execs are {execs}')
                    if execs:
                        book.orders[symbol] = execs

        # print(f'execs scan took: {time.time() - start}')


# TODO: lots of cases still to handle
# XXX: right now this is very very ad-hoc to IB
# - short-sale but securities haven't been located, in this case we
#    should probably keep the order in some kind of weird state or cancel
#    it outright?
# status='PendingSubmit', message=''),
# status='Cancelled', message='Error 404,
#   reqId 1550: Order held while securities are located.'),
# status='PreSubmitted', message='')],

async def translate_and_relay_brokerd_events(

    broker: str,
    ems_client_order_stream: tractor.MsgStream,
    brokerd_trades_stream: tractor.MsgStream,
    book: _DarkBook,

) -> AsyncIterator[dict]:
    """Trades update loop - receive updates from broker, convert
    to EMS responses, transmit to ordering client(s).

    This is where trade confirmations from the broker are processed
    and appropriate responses relayed back to the original EMS client
    actor. There is a messaging translation layer throughout.

    Expected message translation(s):

        broker       ems
        'error'  ->  log it locally (for now)
        'status' ->  relabel as 'broker_<status>', if complete send 'executed'
        'fill'   ->  'broker_filled'

    Currently accepted status values from IB:
        {'presubmitted', 'submitted', 'cancelled', 'inactive'}

    """
    async for brokerd_msg in brokerd_trades_stream:

        name = brokerd_msg['name']

        log.info(f'Received broker trade event:\n{pformat(brokerd_msg)}')

        if name == 'position':

            # relay through position msgs immediately
            await ems_client_order_stream.send(
                BrokerdPosition(**brokerd_msg).dict()
            )
            continue

        # Get the broker (order) request id, this **must** be normalized
        # into messaging provided by the broker backend
        reqid = brokerd_msg['reqid']

        # all piker originated requests will have an ems generated oid field
        oid = brokerd_msg.get(
            'oid',
            book._ems2brokerd_ids.inverse.get(reqid)
        )

        if oid is None:

            # XXX: paper clearing special cases
            # paper engine race case: ``Client.submit_limit()`` hasn't
            # returned yet and provided an output reqid to register
            # locally, so we need to retreive the oid that was already
            # packed at submission since we already know it ahead of
            # time
            paper = brokerd_msg['broker_details'].get('paper_info')
            if paper:
                # paperboi keeps the ems id up front
                oid = paper['oid']

            else:
                # may be an order msg specified as "external" to the
                # piker ems flow (i.e. generated by some other
                # external broker backend client (like tws for ib)
                ext = brokerd_msg.get('external')
                if ext:
                    log.error(f"External trade event {ext}")

                continue
        else:
            # check for existing live flow entry
            entry = book._ems_entries.get(oid)

            # initial response to brokerd order request
            if name == 'ack':

                # register the brokerd request id (that was likely
                # generated internally) with our locall ems order id for
                # reverse lookup later. a BrokerdOrderAck **must** be
                # sent after an order request in order to establish this
                # id mapping.
                book._ems2brokerd_ids[oid] = reqid

                # new order which has not yet be registered into the
                # local ems book, insert it now and handle 2 cases:

                # - the order has previously been requested to be
                # cancelled by the ems controlling client before we
                # received this ack, in which case we relay that cancel
                # signal **asap** to the backend broker
                if entry.action == 'cancel':
                    # assign newly providerd broker backend request id
                    entry.reqid = reqid

                    # tell broker to cancel immediately
                    await brokerd_trades_stream.send(entry.dict())

                # - the order is now active and will be mirrored in
                # our book -> registered as live flow
                else:
                    # update the flow with the ack msg
                    book._ems_entries[oid] = BrokerdOrderAck(**brokerd_msg)

                continue

            # a live flow now exists
            oid = entry.oid

        resp = None
        broker_details = {}

        if name in (
            'error',
        ):
            # TODO: figure out how this will interact with EMS clients
            # for ex. on an error do we react with a dark orders
            # management response, like cancelling all dark orders?

            # This looks like a supervision policy for pending orders on
            # some unexpected failure - something we need to think more
            # about.  In most default situations, with composed orders
            # (ex.  brackets), most brokers seem to use a oca policy.

            msg = BrokerdError(**brokerd_msg)

            # XXX should we make one when it's blank?
            log.error(pformat(msg))

            # TODO: getting this bs, prolly need to handle status messages
            # 'Market data farm connection is OK:usfarm.nj'

            # another stupid ib error to handle
            # if 10147 in message: cancel

            # don't relay message to order requester client
            continue

        elif name in (
            'status',
        ):
            # TODO: templating the ib statuses in comparison with other
            # brokers is likely the way to go:
            # https://interactivebrokers.github.io/tws-api/interfaceIBApi_1_1EWrapper.html#a17f2a02d6449710b6394d0266a353313
            # short list:
            # - PendingSubmit
            # - PendingCancel
            # - PreSubmitted (simulated orders)
            # - ApiCancelled (cancelled by client before submission
            #                 to routing)
            # - Cancelled
            # - Filled
            # - Inactive (reject or cancelled but not by trader)

            # everyone doin camel case
            msg = BrokerdStatus(**brokerd_msg)

            if msg.status == 'filled':

                # conditional execution is fully complete, no more
                # fills for the noted order
                if not msg.remaining:

                    resp = 'broker_executed'

                    log.info(f'Execution for {oid} is complete!')

                # just log it
                else:
                    log.info(f'{broker} filled {msg}')

            else:
                # one of {submitted, cancelled}
                resp = 'broker_' + msg.status

            # pass the BrokerdStatus msg inside the broker details field
            broker_details = msg.dict()

        elif name in (
            'fill',
        ):
            msg = BrokerdFill(**brokerd_msg)

            # proxy through the "fill" result(s)
            resp = 'broker_filled'
            broker_details = msg.dict()

            log.info(f'\nFill for {oid} cleared with:\n{pformat(resp)}')

        else:
            raise ValueError(f'Brokerd message {brokerd_msg} is invalid')

        # Create and relay EMS response status message
        resp = Status(
            oid=oid,
            resp=resp,
            time_ns=time.time_ns(),
            broker_reqid=reqid,
            brokerd_msg=broker_details,
        )
        # relay response to requesting EMS client
        await ems_client_order_stream.send(resp.dict())


async def process_client_order_cmds(

    client_order_stream: tractor.MsgStream,  # noqa
    brokerd_order_stream: tractor.MsgStream,

    symbol: str,
    feed: 'Feed',  # noqa
    dark_book: _DarkBook,

) -> None:

    # cmd: dict
    async for cmd in client_order_stream:

        log.info(f'Received order cmd:\n{pformat(cmd)}')

        action = cmd['action']
        oid = cmd['oid']
        reqid = dark_book._ems2brokerd_ids.inverse.get(oid)
        live_entry = dark_book._ems_entries.get(oid)

        # TODO: can't wait for this stuff to land in 3.10
        # https://www.python.org/dev/peps/pep-0636/#going-to-the-cloud-mappings
        if action in ('cancel',):

            # check for live-broker order
            if live_entry:

                msg = BrokerdCancel(
                    oid=oid,
                    reqid=reqid or live_entry.reqid,
                    time_ns=time.time_ns(),
                )

                # send cancel to brokerd immediately!
                log.info("Submitting cancel for live order")

                # NOTE: cancel response will be relayed back in messages
                # from corresponding broker
                await brokerd_order_stream.send(msg.dict())

            else:
                # might be a cancel for order that hasn't been acked yet
                # by brokerd so register a cancel for then the order
                # does show up later
                dark_book._ems_entries[oid] = msg

                # check for EMS active exec
                try:
                    # remove from dark book clearing
                    dark_book.orders[symbol].pop(oid, None)

                    # tell client side that we've cancelled the
                    # dark-trigger order
                    await client_order_stream.send(
                        Status(
                            resp='dark_cancelled',
                            oid=oid,
                            time_ns=time.time_ns(),
                        ).dict()
                    )

                except KeyError:
                    log.exception(f'No dark order for {symbol}?')

        # TODO: 3.10 struct-pattern matching and unpacking here
        elif action in ('alert', 'buy', 'sell',):

            msg = Order(**cmd)

            sym = msg.symbol
            trigger_price = msg.price
            size = msg.size
            exec_mode = msg.exec_mode
            broker = msg.brokers[0]

            if exec_mode == 'live' and action in ('buy', 'sell',):

                if live_entry is not None:

                    # sanity check on emsd id
                    assert live_entry.oid == oid

                    # if we already had a broker order id then
                    # this is likely an order update commmand.
                    log.info(f"Modifying order: {live_entry.reqid}")

                # TODO: port to BrokerdOrder message sending
                # register broker id for ems id
                msg = BrokerdOrder(
                    oid=oid,  # no ib support for oids...
                    time_ns=time.time_ns(),

                    # if this is None, creates a new order
                    # otherwise will modify any existing one
                    reqid=reqid,

                    symbol=sym,
                    action=action,
                    price=trigger_price,
                    size=size,
                )

                # send request to backend
                # XXX: the trades data broker response loop
                # (``translate_and_relay_brokerd_events()`` above) will
                # handle relaying the ems side responses back to
                # the client/cmd sender from this request
                print(f'sending live order {msg}')
                await brokerd_order_stream.send(msg.dict())

                # an immediate response should be brokerd ack with order
                # id but we register our request as part of the flow
                dark_book._ems_entries[oid] = msg

            elif exec_mode in ('dark', 'paper') or (
                action in ('alert')
            ):
                # submit order to local EMS book and scan loop,
                # effectively a local clearing engine, which
                # scans for conditions and triggers matching executions

                # Auto-gen scanner predicate:
                # we automatically figure out what the alert check
                # condition should be based on the current first
                # price received from the feed, instead of being
                # like every other shitty tina platform that makes
                # the user choose the predicate operator.
                last = dark_book.lasts[(broker, sym)]
                pred = mk_check(trigger_price, last, action)

                spread_slap: float = 5
                min_tick = feed.symbols[sym].tick_size

                if action == 'buy':
                    tickfilter = ('ask', 'last', 'trade')
                    percent_away = 0.005

                    # TODO: we probably need to scale this based
                    # on some near term historical spread
                    # measure?
                    abs_diff_away = spread_slap * min_tick

                elif action == 'sell':
                    tickfilter = ('bid', 'last', 'trade')
                    percent_away = -0.005
                    abs_diff_away = -spread_slap * min_tick

                else:  # alert
                    tickfilter = ('trade', 'utrade', 'last')
                    percent_away = 0
                    abs_diff_away = 0

                # submit execution/order to EMS scan loop

                # NOTE: this may result in an override of an existing
                # dark book entry if the order id already exists

                dark_book.orders.setdefault(
                    sym, {}
                )[oid] = (
                    pred,
                    tickfilter,
                    cmd,
                    percent_away,
                    abs_diff_away
                )

                if action == 'alert':
                    resp = 'alert_submitted'
                else:
                    resp = 'dark_submitted'

                await client_order_stream.send(
                    Status(
                        resp=resp,
                        oid=oid,
                        time_ns=time.time_ns(),
                    ).dict()
                )


@tractor.context
async def _emsd_main(

    ctx: tractor.Context,
    # client_actor_name: str,
    broker: str,
    symbol: str,
    _exec_mode: str = 'dark',  # ('paper', 'dark', 'live')
    loglevel: str = 'info',

) -> None:
    """EMS (sub)actor entrypoint providing the
    execution management (micro)service which conducts broker
    order control on behalf of clients.

    This is the daemon (child) side routine which starts an EMS runtime
    (one per broker-feed) and and begins streaming back alerts from
    broker executions/fills.

    ``send_order_cmds()`` is called here to execute in a task back in
    the actor which started this service (spawned this actor), presuming
    capabilities allow it, such that requests for EMS executions are
    received in a stream from that client actor and then responses are
    streamed back up to the original calling task in the same client.

    The primary ``emsd`` task tree is:

    - ``_emsd_main()``:
      sets up brokerd feed, order feed with ems client, trades dialogue with
      brokderd trading api.
       |
        - ``clear_dark_triggers()``:
          run (dark order) conditions on inputs and trigger brokerd "live"
          order submissions.
       |
        - ``translate_and_relay_brokerd_events()``:
          accept normalized trades responses from brokerd, process and
          relay to ems client(s); this is a effectively a "trade event
          reponse" proxy-broker.
       |
        - ``process_client_order_cmds()``:
          accepts order cmds from requesting piker clients, registers
          execs with exec loop

    """
    # from ._client import send_order_cmds

    global _router
    dark_book = _router.get_dark_book(broker)

    ems_ctx = ctx

    cached_feed = _router.feeds.get((broker, symbol))
    if cached_feed:
        # TODO: use cached feeds per calling-actor
        log.warning(f'Opening duplicate feed for {(broker, symbol)}')

    # spawn one task per broker feed
    async with (
        trio.open_nursery() as n,

        # TODO: eventually support N-brokers
        data.open_feed(
            broker,
            [symbol],
            loglevel=loglevel,
        ) as feed,
    ):
        if not cached_feed:
            _router.feeds[(broker, symbol)] = feed

        # XXX: this should be initial price quote from target provider
        first_quote = await feed.receive()

        # open a stream with the brokerd backend for order
        # flow dialogue

        book = _router.get_dark_book(broker)
        book.lasts[(broker, symbol)] = first_quote[symbol]['last']

        trades_endpoint = getattr(feed.mod, 'trades_dialogue', None)
        portal = feed._brokerd_portal

        if trades_endpoint is None or _exec_mode == 'paper':

            # for paper mode we need to mock this trades response feed
            # so we load bidir stream to a new sub-actor running a
            # paper-simulator clearing engine.

            # load the paper trading engine
            _exec_mode = 'paper'
            log.warning(f'Entering paper trading mode for {broker}')

            # load the paper trading engine inside the brokerd
            # actor to simulate the real load it'll likely be under
            # when also pulling data from feeds
            open_trades_endpoint = paper.open_paperboi(
                broker=broker,
                symbol=symbol,
                loglevel=loglevel,
            )

        else:
            # open live brokerd trades endpoint
            open_trades_endpoint = portal.open_context(
                trades_endpoint,
                loglevel=loglevel,
            )

        async with (
            open_trades_endpoint as (brokerd_ctx, positions),
            brokerd_ctx.open_stream() as brokerd_trades_stream,
        ):
            # signal to client that we're started
            # TODO: we could eventually send back **all** brokerd
            # positions here?
            await ems_ctx.started(positions)

            # establish 2-way stream with requesting order-client and
            # begin handling inbound order requests and updates
            async with ems_ctx.open_stream() as ems_client_order_stream:

                # trigger scan and exec loop
                n.start_soon(
                    clear_dark_triggers,

                    brokerd_trades_stream,
                    ems_client_order_stream,
                    feed.stream,

                    broker,
                    symbol,
                    book
                )

                # begin processing order events from the target brokerd backend
                # by receiving order submission response messages,
                # normalizing them to EMS messages and relaying back to
                # the piker order client.
                n.start_soon(
                    translate_and_relay_brokerd_events,

                    broker,
                    ems_client_order_stream,
                    brokerd_trades_stream,
                    dark_book,
                )

                # start inbound (from attached client) order request processing
                await process_client_order_cmds(
                    ems_client_order_stream,
                    brokerd_trades_stream,
                    symbol,
                    feed,
                    dark_book,
                )


class _Router(BaseModel):
    '''Order router which manages per-broker dark books, alerts,
    and clearing related data feed management.

    '''
    nursery: trio.Nursery

    feeds: dict[str, tuple[trio.CancelScope, float]] = {}
    books: dict[str, _DarkBook] = {}

    class Config:
        arbitrary_types_allowed = True
        underscore_attrs_are_private = False

    def get_dark_book(
        self,
        brokername: str,

    ) -> _DarkBook:

        return self.books.setdefault(brokername, _DarkBook(brokername))


_router: _Router = None


@tractor.context
async def _setup_persistent_emsd(

    ctx: tractor.Context,

) -> None:

    global _router

    # spawn one task per broker feed
    async with trio.open_nursery() as service_nursery:
        _router = _Router(nursery=service_nursery)

        # TODO: send back the full set of persistent orders/execs persistent
        await ctx.started()

        # we pin this task to keep the feeds manager active until the
        # parent actor decides to tear it down
        await trio.sleep_forever()
