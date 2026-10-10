import random
import math
class Securities:
    def __init__(self, ticker, name, price, sector, drift, volatility, beta):
        self.ticker = ticker
        self.name = name
        self.price = price
        self.sector = sector
        self.drift = drift
        self.volatility = volatility
        self.beta = beta
        self.history = [price]

    def pricing(self, market=0):
        noise = random.gauss(0, self.volatility)
        ret = self.drift + self.beta * market + noise
        self.price *= math.exp(ret - 0.5 * self.volatility**2) 
        round(self.price, 2)
        self.history.append(self.price)
        return self.price
# ticker, name, start price, sector,drift/round, volatility/round, beta
# drift      = average pull per round (0.003 = +0.3% a round on average)
# volatility = how wild the stock is (0.03 = typically moves about 3% a round)
# beta = how strongly it follows the market wide mood (1.0 = moves with the market)
stocks = [
Securities("ZNTH", "Zenith Motors",     100, "Automobiles",   0.002, random.uniform(0.01, 0.05), 1.1),
Securities("NOVA", "Nova Pharma",       250, "Healthcare",    0.003, random.uniform(0.01, 0.05), 0.6),
Securities("BLRV", "BlueRiver Energy",    80, "Energy",       0.001, random.uniform(0.01, 0.05), 1.2),
Securities("QNTB", "Quanta Bank",       150, "Banking",       0.002, random.uniform(0.01, 0.05), 1.3),
Securities("LUMN", "Lumen Telecom",      60, "Telecom",       0.001, random.uniform(0.01, 0.05), 0.6),
Securities("IRPK", "IronPeak Steel",    300, "Materials",     0.001, random.uniform(0.01, 0.05), 1.2),
Securities("ORBT", "Orbit Airlines",     45, "Travel",        0.000, random.uniform(0.01, 0.05), 1.5),
Securities("VEDA", "Veda Foods",        120, "Consumer",      0.002, random.uniform(0.01, 0.05), 0.5),
Securities("PXLG", "Pixel Games",       200, "Gaming",        0.004, random.uniform(0.01, 0.05), 1.3),
Securities("ATLS", "Atlas Logistics",    90, "Logistics",     0.002, random.uniform(0.01, 0.05), 1.0),
Securities("SLRG", "SolarGrid",         175, "Clean energy",  0.005, random.uniform(0.01, 0.05), 1.2),
Securities("HLXB", "Helix Biotech",      70, "Biotech",       0.004, random.uniform(0.01, 0.05), 0.9),
Securities("CRWN", "Crown Hotels",      400, "Hospitality",   0.002, random.uniform(0.01, 0.05), 1.1),
Securities("SWFT", "Swift Retail",       55, "Retail",        0.001, random.uniform(0.01, 0.05), 1.0),
Securities("TERA", "Terra Mining",      130, "Mining",        0.001, random.uniform(0.01, 0.05), 1.3),
Securities("ECHO", "Echo Media",        220, "Media",         0.002, random.uniform(0.01, 0.05), 1.0),
Securities("SPRK", "Spark Fintech",      35, "Fintech",       0.005, random.uniform(0.01, 0.05), 1.4),
Securities("VRTX", "Vertex Software",   160, "Software",      0.004, random.uniform(0.01, 0.05), 1.2),
Securities("HRBR", "Harbor Shipping",   110, "Shipping",      0.001, random.uniform(0.01, 0.05), 1.1),
Securities("MSCT", "Mosaic Textiles",   280, "Textiles",      0.001, random.uniform(0.01, 0.05), 0.7),
]

def run_round(stocks, round_num):
    market = random.gauss(0, 0.02)       # one shared shock per round
    print(f"\n--- Round {round_num} ---")
    for asset in stocks:
        asset.pricing(market)
        prev = asset.history[-2]
        change = (asset.price - prev) / prev * 100
        print(f"{asset.name:<18} {asset.price:>8.2f}  {change:+.1f}%")

if __name__ == "__main__":

    for r in range(1,3):
            run_round(stocks,r)





