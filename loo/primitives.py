"""Primitives shared by valuation and winner selection.

Mirrors `services/crates/winner-selection/src/primitives.rs`.

Addresses and order uids are lowercase hex strings *without* a `0x` prefix, which is
what `encode(col, 'hex')` returns from the analytics DB.
"""

from __future__ import annotations

from decimal import Decimal

# `Address::repeat_byte(0xee)` — the sentinel for the native token.
NATIVE_TOKEN = "ee" * 20

ONE_ETH = 10**18

# Wrapped native token per network. Only needed for `as_erc20`, i.e. only when a
# solution trades the `0xee..ee` sentinel directly. Deliberately incomplete: the
# autopilot takes this from per-deployment config rather than a table, so an
# unverified address is worse than a loud failure.
# Verified against the WETH9 per-network deployment table in the services repo
# (contracts/src/main.rs) — the same source the autopilot's `as_erc20` wrapping uses.
WRAPPED_NATIVE_TOKEN = {
    "mainnet": "c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
    "xdai": "e91d153e0b41518a2ce8dd3d7944fa863463a97d",  # WXDAI
    "base": "4200000000000000000000000000000000000006",  # WETH
    "arbitrum-one": "82af49447d8a07e3bd95bd0d56f35241523fbab1",  # WETH
    "sepolia": "fff9976782d46cc05630d1f6ebab18b2324d6b14",  # WETH
    "avalanche": "b31f66aa3c1e785363f0875a1b74e27b85fd66c7",  # WAVAX
    "polygon": "0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",  # WMATIC
    "bnb": "bb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
    "ink": "4200000000000000000000000000000000000006",  # WETH
    "linea": "e5d7c2a44ffddf6b295a15c148167daaaf5cf34f",  # WETH
    "plasma": "6100e367285b01f48d07953803a2d8dca5d19873",  # WXPL
}

# USD reference tokens per network, address -> decimals. The analytics DB has no USD
# price table, but `stg_backend_data__auction_prices` carries a native price for every
# token in the auction — so a major stablecoin's own native price implies the USD rate,
# per auction, from the same source every other number here already trusts. Measured on
# the M1 window the three tokens below agree within ~0.1%, so a median across them is
# robust to any one being off. Deliberately incomplete like WRAPPED_NATIVE_TOKEN:
# an unverified stablecoin address would silently fabricate every USD figure.
USD_REFERENCE_TOKENS: dict[str, dict[str, int]] = {
    "mainnet": {
        "a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6,  # USDC
        "dac17f958d2ee523a2206206994597c13d831ec7": 6,  # USDT
        "6b175474e89094c44da98b954eedeac495271d0f": 18,  # DAI
    },
    # The non-mainnet entries below were verified over July 2026 (~200-auction
    # samples): each token is priced in every sampled auction, the tokens of one
    # network agree with each other to 0.1-0.7% on the implied USD/native rate, and
    # that rate matches an independent anchor (mainnet USD/ETH x the two chains'
    # COW accounting rates) to a few percent. Polygon's bridged USDC.e is
    # deliberately absent: it disagreed with the other three by ~6%. The ink and
    # plasma entries labelled "dollar token" have no name to cite, only that
    # three-way agreement; the analytics DB carries no token metadata to name them.
    "xdai": {
        "2a22f9c3b484c3629090feed35f17ff8f88f76f0": 6,  # USDC.e
        "4ecaba5870353805a9f068101a40e0f32ed605c6": 6,  # USDT
    },
    "base": {
        "833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6,  # USDC
        "d9aaec86b65d86f6a7b5b1b0c42ffa531710b6ca": 6,  # USDbC
        "50c5725949a6f0c72e6c4a641f24049a917db0cb": 18,  # DAI
    },
    "arbitrum-one": {
        "af88d065e77c8cc2239327c5edb3a432268e5831": 6,  # USDC
        "ff970a61a04b1ca14834a43f5de4533ebddb5cc8": 6,  # USDC.e
        "fd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9": 6,  # USDT
    },
    "avalanche": {
        "b97ef9ef8734c71904d8002f8b6bc66dd9c48a6e": 6,  # USDC
        "9702230a8ea53601f5cd2dc00fdbc13d4df4a8c7": 6,  # USDT
        "d586e7f844cea2f87f50152665bcbc2c279d8d70": 18,  # DAI.e
    },
    "polygon": {
        "3c499c542cef5e3811e1192ce70d8cc03d5c3359": 6,  # USDC
        "c2132d05d31c914a87c6611c10748aeb04b58e8f": 6,  # USDT
        "8f3cf7ad23cd3cadbd9735aff958023239c6a063": 18,  # DAI
    },
    "bnb": {
        "55d398326f99059ff775485246999027b3197955": 18,  # USDT (18 decimals on BNB)
        "8ac76a51cc950d9822d68b83fe1ad97b32cd580d": 18,  # USDC (18 decimals on BNB)
        "e9e7cea3dedca5984780bafc599bd69add087d56": 18,  # BUSD
    },
    "linea": {
        "176211869ca2b568f2a7d4ee941e073a821ee1ff": 6,  # USDC.e
        "a219439258ca9da29e9cc4ce5596924745e12b93": 6,  # USDT
    },
    "ink": {
        "2d270e6886d130d724215a266106e6832161eaed": 6,  # dollar token
        "70a38b0c90441e991346b7a0cd98c8528dd1c234": 6,  # dollar token
        "99cbf1ff4527675ed3301671105c9f7748fb8a04": 6,  # dollar token
    },
    "plasma": {
        "b8ce59fc3717ada4c02eadf9682a9e934f625ebb": 6,  # USDT0
        "5d3a1ff2b6bab83b63cd9ad0787074081a52ef34": 18,  # dollar token
        "6695c0f8706c5ace3bdf8995073179cca47926dc": 18,  # dollar token
    },
}

# `crates/configs/src/autopilot/run_loop.rs:11`
MAX_WINNERS = 20

# A directed token pair: (sell_token, buy_token).
Pair = tuple[str, str]


def wrapped_native_token(network: str) -> str:
    try:
        return WRAPPED_NATIVE_TOKEN[network]
    except KeyError:
        raise KeyError(
            f"no verified wrapped native token for network {network!r}; "
            f"add it to WRAPPED_NATIVE_TOKEN after checking the autopilot config"
        ) from None


def usd_reference_tokens(network: str) -> dict[str, int]:
    try:
        return USD_REFERENCE_TOKENS[network]
    except KeyError:
        raise KeyError(
            f"no verified USD reference tokens for network {network!r}; add the "
            f"network's major stablecoins to USD_REFERENCE_TOKENS after verifying "
            f"their addresses and decimals"
        ) from None


def usd_per_native(price: int, decimals: int) -> Decimal:
    """USD per native token implied by a stablecoin's native price.

    1 USD = `10**decimals` atoms, worth `10**decimals * price / 1e18` wei, so one
    native token (1e18 wei) is `10**(36 - decimals) / price` USD. Display only —
    nothing on the valuation path consumes this.
    """
    return Decimal(10) ** (36 - decimals) / Decimal(price)


def as_erc20(token: str, weth: str) -> str:
    """Map the native token sentinel to its wrapped ERC20 (`primitives.rs:9`)."""
    return weth if token == NATIVE_TOKEN else token


def price_in_eth(price: int, amount: int) -> int:
    """Convert a token amount to native (`primitives.rs:20`): `amount * price / 1e18`."""
    return amount * price // ONE_ETH


def ceil_div(numerator: int, denominator: int) -> int:
    """`U256Ext::checked_ceil_div` — division rounding away from zero.

    Both operands are non-negative here, so `-(-a // b)` is plain ceiling division.
    """
    if denominator == 0:
        raise ZeroDivisionError("ceil_div by zero")
    return -(-numerator // denominator)


def order_owner(order_uid: str) -> str:
    """Owner address embedded in a 56-byte order uid (`primitives.rs:46`).

    Layout: 32 bytes digest, 20 bytes owner, 4 bytes valid_to.
    """
    if len(order_uid) != 112:
        raise ValueError(f"order uid is not 56 bytes: {order_uid!r}")
    return order_uid[64:104]
