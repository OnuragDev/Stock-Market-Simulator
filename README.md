# Market Simulator. 
A small stock market simulator in Python. 
This contains 20 fictional stocks, along with their ticker and domain, and moves their prices round by round using a drift, and a randomized volatility and a shared market mood. 

Each round prints every stock's price and its % change from the previous round.

## How it works
Each stock is an element in a list, which contains its ticker, name, sector, price, drift, volatility and beta. 

## Definition of the terms used

Every round - 
1) One market shock is drawn from a normal distribution and shared by all stocks.
2) The price is updates multiplicatively with `exp` a form of geometric brownian motion, so prices never go negative. 
3) The new price is saved in the stocks `history` list.

## Projected future updates 
- Add different ordertypes (market order, limit order, stop-loss etc)
- Pricing charts with `matplotlib`
- A section where you can manually change the volatility of a stock.

## Backstory 

I originally planned on making this for a techno-cultural fest in my college, but I couldn't get permission for that. 
I kept it in development as a standalone project.
