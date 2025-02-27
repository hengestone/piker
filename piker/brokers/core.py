# piker: trading gear for hackers
# Copyright (C) 2018-present  Tyler Goodlet (in stewardship of piker0)

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
Broker high level cross-process API layer.

This API should be kept "remote service compatible" meaning inputs to
routines should be primitive data types where possible.
"""
import inspect
from types import ModuleType
from typing import List, Dict, Any, Optional

import trio

from ..log import get_logger
from . import get_brokermod
from .._daemon import maybe_spawn_brokerd
from .api import open_cached_client


log = get_logger(__name__)


async def api(brokername: str, methname: str, **kwargs) -> dict:
    """Make (proxy through) a broker API call by name and return its result.
    """
    brokermod = get_brokermod(brokername)
    async with brokermod.get_client() as client:
        meth = getattr(client, methname, None)
        if meth is None:
            log.debug(
                f"Couldn't find API method {methname} looking up on client")
            meth = getattr(client.api, methname, None)

        if meth is None:
            log.error(f"No api method `{methname}` could be found?")
            return

        if not kwargs:
            # verify kwargs requirements are met
            sig = inspect.signature(meth)
            if sig.parameters:
                log.error(
                    f"Argument(s) are required by the `{methname}` method: "
                    f"{tuple(sig.parameters.keys())}")
                return

        return await meth(**kwargs)


async def stocks_quote(
    brokermod: ModuleType,
    tickers: List[str]
) -> Dict[str, Dict[str, Any]]:
    """Return quotes dict for ``tickers``.
    """
    async with brokermod.get_client() as client:
        return await client.quote(tickers)


# TODO: these need tests
async def option_chain(
    brokermod: ModuleType,
    symbol: str,
    date: Optional[str] = None,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Return option chain for ``symbol`` for ``date``.

    By default all expiries are returned. If ``date`` is provided
    then contract quotes for that single expiry are returned.
    """
    async with brokermod.get_client() as client:
        if date:
            id = int((await client.tickers2ids([symbol]))[symbol])
            # build contracts dict for single expiry
            return await client.option_chains(
                {(symbol, id, date): {}})
        else:
            # get all contract expiries
            # (takes a long-ass time on QT fwiw)
            contracts = await client.get_all_contracts([symbol])
            # return chains for all dates
            return await client.option_chains(contracts)


async def contracts(
    brokermod: ModuleType,
    symbol: str,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Return option contracts (all expiries) for ``symbol``.
    """
    async with brokermod.get_client() as client:
        # return await client.get_all_contracts([symbol])
        return await client.get_all_contracts([symbol])


async def bars(
    brokermod: ModuleType,
    symbol: str,
    **kwargs,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Return option contracts (all expiries) for ``symbol``.
    """
    async with brokermod.get_client() as client:
        return await client.bars(symbol, **kwargs)


async def symbol_info(
    brokermod: ModuleType,
    symbol: str,
    **kwargs,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Return symbol info from broker.
    """
    async with brokermod.get_client() as client:
        return await client.symbol_info(symbol, **kwargs)


async def search_w_brokerd(name: str, pattern: str) -> dict:

    async with open_cached_client(name) as client:

        # TODO: support multiple asset type concurrent searches.
        return await client.search_symbols(pattern=pattern)


async def symbol_search(
    brokermods: list[ModuleType],
    pattern: str,
    **kwargs,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Return symbol info from broker.
    """
    results = []

    async def search_backend(brokername: str) -> None:

        async with maybe_spawn_brokerd(
            brokername,
        ) as portal:

            results.append((
                brokername,
                await portal.run(
                    search_w_brokerd,
                    name=brokername,
                    pattern=pattern,
                ),
            ))

    async with trio.open_nursery() as n:

        for mod in brokermods:
            n.start_soon(search_backend, mod.name)

    return results
