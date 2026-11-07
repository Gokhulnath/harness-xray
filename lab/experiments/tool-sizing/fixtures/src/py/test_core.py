import numpy as np
import pandas as pd
import pytest

from core import fib, slugify, word_counts


@pytest.mark.parametrize("n,expected", [(0, 0), (1, 1), (2, 1), (10, 55), (20, 6765), (30, 832040)])
def test_fib(n, expected):
    assert fib(n) == expected


@pytest.mark.parametrize("i", range(40))
def test_slugify(i):
    assert slugify(f"Hello World {i}!") == f"hello-world-{i}"


def test_word_counts():
    assert word_counts("a b a") == {"a": 2, "b": 1}


@pytest.mark.parametrize("rows", [1_000, 10_000, 100_000])
def test_groupby(rows):
    rng = np.random.default_rng(rows)
    df = pd.DataFrame({"k": rng.integers(0, 50, rows), "v": rng.random(rows)})
    g = df.groupby("k")["v"].sum()
    assert len(g) == 50 and abs(g.sum() - df["v"].sum()) < 1e-6
