"""One-off probe: which websocket feeds are reachable, and what they send."""
import json
import time

import websocket

FEEDS = {
    "coinbase": ("wss://ws-feed.exchange.coinbase.com",
                 [{"type": "subscribe", "product_ids": ["BTC-USD", "ETH-USD"], "channels": ["ticker"]}]),
    "kraken": ("wss://ws.kraken.com/v2",
               [{"method": "subscribe", "params": {"channel": "ticker", "symbol": ["BTC/USD", "ETH/USD"]}}]),
    "bitstamp": ("wss://ws.bitstamp.net",
                 [{"event": "bts:subscribe", "data": {"channel": "order_book_btcusd"}}]),
    "gemini": ("wss://api.gemini.com/v1/marketdata/BTCUSD?top_of_book=true&bids=true&offers=true&trades=false", []),
    "kalshi": ("wss://api.elections.kalshi.com/trade-api/ws/v2",
               [{"id": 1, "cmd": "subscribe", "params": {"channels": ["ticker"]}}]),
}

for name, (url, subs) in FEEDS.items():
    t0 = time.time()
    try:
        ws = websocket.create_connection(url, timeout=8)
        for s in subs:
            ws.send(json.dumps(s))
        msgs = []
        while len(msgs) < 4 and time.time() - t0 < 8:
            msgs.append(ws.recv())
        ws.close()
        print(f"== {name}: OK, {len(msgs)} msgs in {time.time() - t0:.1f}s")
        for m in msgs[:4]:
            print("   ", m[:300])
    except Exception as e:
        print(f"== {name}: FAILED {type(e).__name__}: {str(e)[:200]}")
