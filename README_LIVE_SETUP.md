# Quantum Engine — Netlify + FastAPI live-data setup

## Architecture
Netlify (`index.html`) -> FastAPI (`/api/signals`, `/api/state`) -> Yahoo Finance daily NSE data -> QAOA.

## Deploy backend on Render
1. Create a new Web Service from this folder/repository.
2. Render will use `render.yaml`, or set:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
3. Keep the generated `REFRESH_TOKEN` environment variable.
4. The expected service name is `quantumengine-api`, giving a URL like `https://quantumengine-api.onrender.com`.
5. Test `/health` and confirm it returns `status: ok`.

## Connect Netlify
`index.html` is already configured for:
`https://quantumengine-api.onrender.com/api/signals`
`https://quantumengine-api.onrender.com/api/state`

If Render gives a different URL, change `BACKEND_URL` near the bottom of `index.html` and redeploy Netlify.

## Refresh automation
GitHub Actions runs every 15 minutes on weekdays and calls the protected `/api/refresh` endpoint.
Create repository secrets:
- `BACKEND_URL` = your Render URL, e.g. `https://quantumengine-api.onrender.com`
- `REFRESH_TOKEN` = the same value configured on Render

You can also run the workflow manually with **Run workflow**.

## Important data limitation
The engine uses Yahoo Finance with `interval="1d"`. This is the latest available daily market data, not a tick-by-tick/live NSE feed. For true intraday live prices, replace `fetch_market_prices()` with a broker/licensed market-data API such as Zerodha Kite Connect or another provider that supplies intraday quotes.
