import requests
import json
import pandas as pd
from typing import Optional, List, Dict
from datetime import datetime, timedelta
import config


class NSEAPI:
    def __init__(self):
        self.api_key = config.NSE_API_KEY
        self.api_secret = config.NSE_API_SECRET
        self.base_url = "https://api-nse.fyers.in"
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        self.access_token = None

    def _request(self, method: str, endpoint: str, data: dict = None) -> dict:
        url = f"{self.base_url}{endpoint}"
        
        try:
            if method == "GET":
                response = self.session.get(url, params=data)
            elif method == "POST":
                if self.access_token:
                    data["app_id"] = self.api_key
                    data["access_token"] = self.access_token
                response = self.session.post(url, json=data)
            
            response.raise_for_status()
            return response.json()
        
        except requests.exceptions.HTTPError as e:
            return {"error": str(e), "status": "error"}
        except requests.exceptions.RequestException as e:
            return {"error": str(e), "status": "error"}

    def generate_token(self, api_key: str = None, api_secret: str = None,
                     app_id: str = None) -> Dict:
        api_key = api_key or self.api_key
        api_secret = api_secret or self.api_secret
        
        endpoint = "/auth/v1/generateToken"
        data = {
            "app_id": api_key,
            "app_secret": api_secret,
            "grant_type": "client_credentials"
        }
        
        if app_id:
            data["app_id"] = app_id
        
        result = self._request("POST", endpoint, data)
        
        if "access_token" in result:
            self.access_token = result["access_token"]
            return {"status": "success", "access_token": self.access_token}
        
        return result

    def get_historical_data(self, symbol: str, resolution: str = "D",
                        from_date: str = None, to_date: str = None) -> pd.DataFrame:
        if from_date is None:
            # `%s` is Linux-only; .timestamp() is portable across Windows/macOS.
            from_date = int((datetime.now() - timedelta(days=30)).timestamp())
        if to_date is None:
            to_date = int(datetime.now().timestamp())
        
        resolution_map = {
            "D": "D",
            "W": "W",
            "M": "M",
            "1": "1",
            "5": "5",
            "15": "15",
            "30": "30",
            "60": "60"
        }
        
        resolution = resolution_map.get(resolution, "D")
        
        endpoint = "/history/v1/datetimeSeries"
        data = {
            "symbol": symbol,
            "resolution": resolution,
            "from_date": str(int(from_date)),
            "to_date": str(to_date)
        }
        
        result = self._request("POST", endpoint, data)
        
        if "error" in result:
            return pd.DataFrame()
        
        candles = result.get("candles", [])
        
        if not candles:
            return pd.DataFrame()
        
        df = pd.DataFrame(candles, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
        df['date'] = pd.to_datetime(df['date'], unit='s')
        
        return df

    def get_quote(self, symbol: str) -> Dict:
        endpoint = "/market/v1/quote"
        data = {"symbol": [symbol]}
        
        result = self._request("POST", endpoint, data)
        
        if "data" in result and symbol in result["data"]:
            return result["data"][symbol]
        
        return {}

    def get_quotes(self, symbols: List[str]) -> Dict:
        endpoint = "/market/v1/quote"
        data = {"symbol": symbols}
        
        result = self._request("POST", endpoint, data)
        
        if "data" in result:
            return result["data"]
        
        return {}

    def get_market_depth(self, symbol: str) -> Dict:
        endpoint = "/market/v1/depth"
        data = {"symbol": symbol}
        
        result = self._request("POST", endpoint, data)
        
        if "data" in result:
            return result["data"]
        
        return {}

    def get_option_chain(self, symbol: str, expiry: int = 0, strike_count: int = 10) -> List[Dict]:
        endpoint = "/market/v1/optionChain"
        data = {
            "symbol": symbol,
            "expiry": expiry,
            "strikeCount": strike_count
        }
        
        result = self._request("POST", endpoint, data)
        
        option_chain = result.get("optionChain", [])
        return option_chain

    def get_fno_stocks(self) -> List[str]:
        endpoint = "/market/v1/fnoStocks"
        result = self._request("GET", endpoint)
        
        if "data" in result:
            return result["data"]
        
        return []

    def get_indices(self) -> List[Dict]:
        endpoint = "/market/v1/indices"
        result = self._request("GET", endpoint)
        
        if "data" in result:
            return result["data"]
        
        return []

    def place_order(self, symbol: str, side: str, qty: int,
                   order_type: str = "LIMIT", price: float = None,
                   stop_loss: float = None, target: float = None,
                   product_type: str = "INTRADAY") -> Dict:
        endpoint = "/orders/v1/place"
        
        side_map = {"buy": 1, "sell": -1}
        product_map = {
            "INTRADAY": "C",
            "CNC": "M",
            "MARGIN": "M",
            "CO": "C"
        }
        
        order_data = {
            "symbol": symbol,
            "side": side_map.get(side.lower(), 1),
            "qty": qty,
            "orderType": order_type.upper(),
            "productType": product_map.get(product_type.upper(), "C")
        }
        
        if price:
            order_data["limitPrice"] = price
        
        if stop_loss:
            order_data["stopLoss"] = stop_loss
        
        if target:
            order_data["targetPrice"] = target
        
        result = self._request("POST", endpoint, order_data)
        return result

    def modify_order(self, order_id: str, price: float = None,
                   qty: int = None, order_type: str = "LIMIT") -> Dict:
        endpoint = "/orders/v1/modify"
        data = {
            "id": order_id,
            "orderType": order_type.upper()
        }
        
        if price:
            data["limitPrice"] = price
        if qty:
            data["qty"] = qty
        
        return self._request("PUT", endpoint, data)

    def cancel_order(self, order_id: str) -> Dict:
        endpoint = "/orders/v1/cancel"
        data = {"id": order_id}
        
        return self._request("POST", endpoint, data)

    def get_orders(self) -> List[Dict]:
        endpoint = "/orders/v1/books"
        result = self._request("GET", endpoint)
        
        if "data" in result and "orderBook" in result["data"]:
            return result["data"]["orderBook"]
        
        return []

    def get_positions(self) -> List[Dict]:
        endpoint = "/positions/v1/convertpositionquantity"
        result = self._request("GET", endpoint)
        
        if result.get("code") == 200:
            return result.get("data", [])
        
        return []

    def get_trades(self) -> List[Dict]:
        endpoint = "/trades/v1/books"
        result = self._request("GET", endpoint)
        
        if result.get("code") == 200:
            return result.get("data", [])
        
        return []

    def get_holdings(self) -> List[Dict]:
        endpoint = "/holdings/v1"
        result = self._request("GET", endpoint)
        
        if result.get("code") == 200:
            return result.get("data", [])
        
        return []


nse_api = NSEAPI()
