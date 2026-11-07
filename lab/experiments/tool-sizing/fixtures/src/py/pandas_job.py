"""A data-wrangling tool call: 2M rows, groupby, merge, sort, write a 500k-row CSV into the workspace."""
import time
import numpy as np
import pandas as pd

t = time.time()
rng = np.random.default_rng(42)
n = 2_000_000
df = pd.DataFrame({"user": rng.integers(0, 100_000, n), "item": rng.integers(0, 5_000, n),
                   "price": rng.random(n) * 100, "qty": rng.integers(1, 5, n)})
df["revenue"] = df["price"] * df["qty"]
by_user = df.groupby("user").agg(revenue=("revenue", "sum"), orders=("item", "count")).reset_index()
items = pd.DataFrame({"item": np.arange(5_000), "category": rng.integers(0, 40, 5_000)})
merged = df.merge(items, on="item").groupby("category")["revenue"].sum().sort_values(ascending=False)
df.head(500_000).to_csv("orders_sample.csv", index=False)
print(len(by_user), round(merged.iloc[0], 2), f"{time.time() - t:.2f}s")
