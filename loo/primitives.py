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
