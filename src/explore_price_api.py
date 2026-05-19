import json
import requests
from gex import get_access_token

def explore_price_history(symbol: str):
    token = get_access_token()

    response = requests.get(
        f"https://api.schwabapi.com/marketdata/v1/pricehistory",
        headers={"Authorization": f"Bearer {token}"},
        params={
            "symbol": symbol,
            "periodType": "day",
            "period": 1,
            "frequencyType": "minute",
            "frequency": 1,
            "needExtendedHoursData": True
        }
    )

    print(f"\nStatus: {response.status_code}")
    print(f"\nRaw response for {symbol}:")
    print(json.dumps(response.json(), indent=2))

if __name__ == "__main__":
    explore_price_history("SPY")
