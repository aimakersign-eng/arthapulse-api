"""
ArthaPulse NSE/BSE Live Market & Excel API Backend (FastAPI)
=============================================================
Connects directly to NSE India official endpoints (with session cookie handling)
and exposes CORS-enabled JSON endpoints for:
  1. ArthaPulse Web Dashboard (Excel & API -> Sync Live)
  2. Microsoft Excel (Data -> From Web / Power Query)

Setup & Run:
  pip install fastapi uvicorn requests
  python nse_live_backend.py

For HTTPS access from browser preview, expose port 8000 via:
  cloudflared tunnel --url http://localhost:8000   (or ngrok http 8000)
Then paste the https://.../api/v1/quote/NIFTY URL into Dashboard -> Excel & API -> Sync Live.
"""

from datetime import datetime
from typing import Dict, Any, Optional
import time
import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="ArthaPulse NSE Live API", version="1.0.0")

# Enable CORS so the dashboard can fetch live JSON updates
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


class NSESession:
    """Maintains an active NSE session with fresh cookies."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(NSE_HEADERS)
        self.last_refresh = 0.0

    def ensure_cookies(self) -> None:
        if time.time() - self.last_refresh > 240:
            self.session.get("https://www.nseindia.com", timeout=10)
            self.last_refresh = time.time()

    def get_json(self, url: str) -> Dict[str, Any]:
        self.ensure_cookies()
        resp = self.session.get(url, timeout=10)
        if resp.status_code in (401, 403):
            self.last_refresh = 0.0
            self.ensure_cookies()
            resp = self.session.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()


nse = NSESession()
INDEX_MAP = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "FINNIFTY": "NIFTY FINANCIAL SERVICES",
    "MIDCPNIFTY": "NIFTY MIDCAP SELECT",
}


def compute_levels(spot: float, sl_pct: float = 0.55, rr: float = 1.0) -> Dict[str, float]:
    rng = spot * (sl_pct / 100.0) * 1.45
    sl = round(spot - rng * 1.15, 2)
    risk = spot - sl
    return {
        "s1": round(spot - rng, 2),
        "s2": round(spot - rng * 2.05, 2),
        "s3": round(spot - rng * 3.25, 2),
        "r1": round(spot + rng, 2),
        "r2": round(spot + rng * 2.05, 2),
        "r3": round(spot + rng * 3.25, 2),
        "buy_low": round(spot - rng * 0.45, 2),
        "buy_high": round(spot + rng * 0.10, 2),
        "stop_loss": sl,
        "target_1": round(spot + risk * rr, 2),
        "target_2": round(spot + risk * (rr + 0.5), 2),
    }


@app.get("/api/v1/quote/{symbol}")
def get_live_quote(symbol: str):
    sym = symbol.upper()
    try:
        if sym in INDEX_MAP:
            data = nse.get_json("https://www.nseindia.com/api/allIndices")
            target_name = INDEX_MAP[sym]
            match = next((x for x in data.get("data", []) if x.get("index") == target_name), None)
            if not match:
                raise HTTPException(status_code=404, detail=f"Index {sym} not found")
            spot = float(match["last"])
            chg_pct = float(match.get("percentChange", 0.0))
            ohlc = {
                "open": float(match.get("open", spot)),
                "high": float(match.get("high", spot)),
                "low": float(match.get("low", spot)),
                "prev_close": float(match.get("previousClose", spot)),
            }
        else:
            data = nse.get_json(f"https://www.nseindia.com/api/quote-equity?symbol={sym}")
            price_info = data.get("priceInfo", {})
            spot = float(price_info["lastPrice"])
            chg_pct = float(price_info.get("pChange", 0.0))
            intra = price_info.get("intraDayHighLow", {})
            ohlc = {
                "open": float(price_info.get("open", spot)),
                "high": float(intra.get("max", spot)),
                "low": float(intra.get("min", spot)),
                "prev_close": float(price_info.get("previousClose", spot)),
            }

        levels = compute_levels(spot)
        return {
            "timestamp": datetime.now().isoformat(),
            "symbol": sym,
            "spot": spot,
            "chgPct": round(chg_pct, 2),
            "ohlc": ohlc,
            "trade_setup": levels,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"NSE fetch error: {exc}")


@app.get("/api/v1/option-chain/{symbol}")
def get_option_chain(symbol: str):
    sym = symbol.upper()
    try:
        if sym in INDEX_MAP or sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
            url = f"https://www.nseindia.com/api/option-chain-indices?symbol={sym}"
        else:
            url = f"https://www.nseindia.com/api/option-chain-equities?symbol={sym}"
        raw = nse.get_json(url)
        records = raw.get("records", {})
        spot = float(records.get("underlyingValue", 0.0))
        rows = records.get("data", [])

        total_ce_oi = sum(r.get("CE", {}).get("openInterest", 0) for r in rows)
        total_pe_oi = sum(r.get("PE", {}).get("openInterest", 0) for r in rows)
        pcr = round(total_pe_oi / max(total_ce_oi, 1), 2)

        return {
            "timestamp": datetime.now().isoformat(),
            "symbol": sym,
            "spot": spot,
            "pcr": pcr,
            "strikes_count": len(rows),
            "data": rows[:25],
        }
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Option chain error: {exc}")


@app.get("/api/v1/fii-dii")
def get_fii_dii():
    try:
        data = nse.get_json("https://www.nseindia.com/api/fiidiiTradeReact")
        return {"timestamp": datetime.now().isoformat(), "activity": data}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"FII/DII fetch error: {exc}")


@app.get("/api/v1/excel-feed/{symbol}")
def get_excel_flat_feed(symbol: str, sl_pct: Optional[float] = 0.55, rr: Optional[float] = 1.0):
    """Flat JSON array ideal for Excel Data > From Web (Power Query)."""
    q = get_live_quote(symbol)
    lv = compute_levels(q["spot"], sl_pct=sl_pct or 0.55, rr=rr or 1.0)
    return [
        {
            "Timestamp": q["timestamp"],
            "Symbol": q["symbol"],
            "Spot": q["spot"],
            "ChangePct": q["chgPct"],
            "Open": q["ohlc"]["open"],
            "High": q["ohlc"]["high"],
            "Low": q["ohlc"]["low"],
            "PrevClose": q["ohlc"]["prev_close"],
            "S1": lv["s1"],
            "S2": lv["s2"],
            "S3": lv["s3"],
            "R1": lv["r1"],
            "R2": lv["r2"],
            "R3": lv["r3"],
            "BuyZone": f"{lv['buy_low']} - {lv['buy_high']}",
            "Target_1_1": lv["target_1"],
            "StopLoss": lv["stop_loss"],
        }
    ]


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)