"""Primitives shared by valuation and winner selection.

Mirrors `services/crates/winner-selection/src/primitives.rs`.

Addresses and order uids are lowercase hex strings *without* a `0x` prefix, which is
what `encode(col, 'hex')` returns from the analytics DB.
"""

from __future__ import annotations

# `Address::repeat_byte(0xee)` — the sentinel for the native token.
NATIVE_TOKEN = "ee" * 20

ONE_ETH = 10**18

# Wrapped native token per network. Only needed for `as_erc20`, i.e. only when a
# solution trades the `0xee..ee` sentinel directly. Deliberately incomplete: the
# autopilot takes this from per-deployment config rather than a table, so an
# unverified address is worse than a loud failure.
WRAPPED_NATIVE_TOKEN = {
    "mainnet": "c02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
    "xdai": "e91d153e0b41518a2ce8dd3d7944fa863463a97d",  # WXDAI
    "base": "4200000000000000000000000000000000000006",  # WETH
    "arbitrum-one": "82af49447d8a07e3bd95bd0d56f35241523fbab1",  # WETH
    "sepolia": "fff9976782d46cc05630d1f6ebab18b2324d6b14",  # WETH
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
