#!/usr/bin/env python3

import unittest
import fakeredis

from pokerapp.pokerbotmodel import WalletManagerModel


class TestWalletManagerModel(unittest.TestCase):
    def test_dec_without_lupa(self):
        kv = fakeredis.FakeRedis()
        wallet = WalletManagerModel("user", kv)
        start = wallet.value()

        def raise_module_not_found(*args, **kwargs):
            raise ModuleNotFoundError("lupa")

        wallet._LUA_DECR_IF_GE = raise_module_not_found
        result = wallet.dec(100)
        self.assertEqual(start - 100, result)
        self.assertEqual(start - 100, wallet.value())


if __name__ == "__main__":
    unittest.main()
