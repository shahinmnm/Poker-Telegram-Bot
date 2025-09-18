#!/usr/bin/env python3

import pytest
import fakeredis.aioredis

from pokerapp.pokerbotmodel import WalletManagerModel


@pytest.mark.asyncio
async def test_dec_without_lupa():
    kv = fakeredis.aioredis.FakeRedis()
    wallet = WalletManagerModel("user", kv)
    start = await wallet.value()

    async def raise_module_not_found(*args, **kwargs):
        raise ModuleNotFoundError("lupa")

    wallet._LUA_DECR_IF_GE = raise_module_not_found
    result = await wallet.dec(100)
    assert start - 100 == result
    assert start - 100 == await wallet.value()


if __name__ == "__main__":
    pytest.main()
