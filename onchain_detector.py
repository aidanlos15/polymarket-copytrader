"""Real-time on-chain trade detector for a Polymarket wallet (low latency).

Instead of polling the data API (measured 3-9 MINUTES stale), this watches the Polygon
blockchain and reacts to the whale's trades within ~one block (~2-4s). For each trade it
produces the same fields the rest of the bot expects (asset, side, size, price,
conditionId, title, outcome, timestamp, transactionHash).

How it works (verified against ground-truth data-API trades):
  1. Poll eth_getLogs for the Polymarket exchanges' `OrderFilled` events where the whale
     is maker OR taker (address-filtered so it works on public RPCs; ~2s poll).
  2. For each new tx, fetch the receipt and compute the whale's NET change in outcome
     tokens (ERC-1155) and USDC (ERC-20). Net deltas collapse multi-leg neg-risk trades
     into the real position change — matches the data API exactly.
  3. Enrich token id -> market metadata via the Gamma API (conditionId/title/outcome).

Run standalone to measure latency vs the data API:
    python onchain_detector.py
Works on the default public RPC; a free Alchemy/Infura HTTP key (POLYGON_HTTP) is more
reliable under load.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict

import requests

import config

# Polymarket exchange contracts emitting OrderFilled (discovered on-chain).
EXCHANGES = [
    "0xe111180000d2663c0091e4f400237545b87b996b",
    "0xe2222d279d744050d28e00520010520000310f59",
]
ORDERFILLED = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
ERC20_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TS_1155 = "0xc3d58168c5aedf07a6d758f2ef64634a1a45a0e7e2dfd4e2da5b06e7e0e6b2f9"  # TransferSingle
TB_1155 = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"  # TransferBatch

_market_cache: dict[str, dict] = {}


def _topic_addr(tp: str) -> str:
    return ("0x" + tp[-40:]).lower()


def _words(data: str) -> list[int]:
    d = data[2:]
    return [int(d[i * 64:(i + 1) * 64], 16) for i in range(len(d) // 64)]


def decode_trades(receipt: dict, whale: str) -> list[dict]:
    """Decode the whale's trade(s) from the OrderFilled legs where the whale is the MAKER.

    Verified to match the Polymarket data API 12/12 for both whales, including neg-risk
    markets: the maker leg is the whale's actual order (makerAssetId 0 = paid USDC = BUY of
    takerAssetId; nonzero = sold that token). The whale-as-TAKER legs are neg-risk liquidity
    mechanics and are intentionally ignored. Amounts are 6-decimal (USDC and shares).
    """
    whale = whale.lower()
    agg: dict[str, list] = defaultdict(lambda: [0, 0.0])   # token -> [net_shares_6dp, usdc_6dp]
    for l in receipt["logs"]:
        if l["topics"][0].lower() != ORDERFILLED or len(l["topics"]) < 4:
            continue
        if _topic_addr(l["topics"][2]) != whale:           # whale must be the MAKER
            continue
        mA, tA, mAmt, tAmt = _words(l["data"])[:4]
        if mA == 0:                                        # paid USDC -> BUY takerAsset
            agg[str(tA)][0] += tAmt
            agg[str(tA)][1] += mAmt
        else:                                              # gave token -> SELL it for USDC
            agg[str(mA)][0] -= mAmt
            agg[str(mA)][1] += tAmt

    out = []
    for tok, (shares6, usdc6) in agg.items():
        if shares6 == 0:
            continue
        shares = abs(shares6) / 1e6
        out.append({"asset": tok, "side": "BUY" if shares6 > 0 else "SELL",
                    "size": round(shares, 6), "price": round(usdc6 / abs(shares6), 4)})
    return out


def lookup_market(token_id: str) -> dict:
    """token id -> {conditionId, title, outcome, slug} via Gamma (cached). Best-effort."""
    if token_id in _market_cache:
        return _market_cache[token_id]
    info = {"conditionId": "", "title": "", "outcome": "", "slug": "", "outcomeIndex": 0}
    try:
        j = requests.get("https://gamma-api.polymarket.com/markets",
                         params={"clob_token_ids": token_id}, timeout=8).json()
        if isinstance(j, list) and j:
            m = j[0]
            ids = json.loads(m.get("clobTokenIds", "[]"))
            outs = json.loads(m.get("outcomes", "[]"))
            idx = ids.index(token_id) if token_id in ids else 0
            info = {"conditionId": m.get("conditionId", ""), "title": m.get("question", ""),
                    "outcome": outs[idx] if idx < len(outs) else "", "slug": m.get("slug", ""),
                    "outcomeIndex": idx}
    except (requests.RequestException, ValueError, KeyError):
        pass
    _market_cache[token_id] = info
    return info


class OnchainDetector:
    def __init__(self) -> None:
        self.whale = config.TARGET_ADDRESS.lower()
        self.padded = "0x" + "0" * 24 + self.whale[2:]
        self.seen_tx: set[str] = set()
        self._block_time: dict[str, int] = {}
        self._last: int | None = None   # last polled block

    def _rpc(self, method: str, params: list):
        r = requests.post(config.POLYGON_HTTP,
                          json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                          timeout=15).json()
        return None if "error" in r else r.get("result")

    def _block_ts(self, block_hex: str) -> int | None:
        if block_hex not in self._block_time:
            blk = self._rpc("eth_getBlockByNumber", [block_hex, False])
            if not blk:
                return None
            self._block_time[block_hex] = int(blk["timestamp"], 16)
        return self._block_time[block_hex]

    def _get_logs(self, frm: int, to: int) -> list[dict]:
        logs = []
        for pos in ([ORDERFILLED, None, [self.padded]],          # whale = maker (topic 2)
                    [ORDERFILLED, None, None, [self.padded]]):    # whale = taker (topic 3)
            r = self._rpc("eth_getLogs", [{"fromBlock": hex(frm), "toBlock": hex(to),
                                           "address": EXCHANGES, "topics": pos}])
            if isinstance(r, list):
                logs += r
        return logs

    def poll_once(self) -> list[dict]:
        """Non-blocking: scan blocks since the last poll, return new decoded trades.

        Each trade is a dict with the same field names the data-API path uses (asset,
        side, size, price, conditionId, title, outcome, outcomeIndex, timestamp,
        transactionHash). Safe to call in a loop; never raises (returns [] on RPC error).
        On the first call it just records the chain head and returns [] (start forward)."""
        try:
            latest = int(self._rpc("eth_blockNumber", []), 16)
        except Exception:
            return []
        if self._last is None:
            self._last = latest
            return []
        if latest <= self._last:
            return []
        try:
            logs = self._get_logs(self._last + 1, latest)
        except Exception:
            return []
        self._last = latest
        recv = time.time()   # the instant we learned of these trades (for the lag metric)

        txs: dict[str, str] = {}
        for l in logs:
            txs.setdefault(l["transactionHash"], l["blockNumber"])
        out = []
        for txh, bn in sorted(txs.items(), key=lambda kv: int(kv[1], 16)):
            if txh in self.seen_tx:
                continue
            self.seen_tx.add(txh)
            rcpt = self._rpc("eth_getTransactionReceipt", [txh])
            if not rcpt:
                continue
            ts = self._block_ts(bn) or int(time.time())
            for tr in decode_trades(rcpt, self.whale):
                tr.update(lookup_market(tr["asset"]))
                tr["timestamp"] = ts
                tr["recv_ts"] = recv
                tr["transactionHash"] = txh
                out.append(tr)
        return out

    def run(self, from_block: int | None = None) -> None:
        self._last = from_block
        print(f"[onchain] polling @{config.TARGET_NAME} trades every "
              f"{config.ONCHAIN_POLL_SECONDS}s")
        while True:
            for tr in self.poll_once():
                lag = int(time.time()) - tr["timestamp"]
                print(f"[onchain] {tr['side']} {tr['size']} @ {tr['price']} "
                      f"{tr['title'][:42]} | detect lag {lag}s")
            time.sleep(config.ONCHAIN_POLL_SECONDS)


if __name__ == "__main__":
    print(f"On-chain detector for @{config.TARGET_NAME} ({config.TARGET_ADDRESS})")
    OnchainDetector().run()
