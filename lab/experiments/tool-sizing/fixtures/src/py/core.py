"""Small helpers the tests exercise."""
import re


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def fib(n: int) -> int:
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return a


def word_counts(text: str) -> dict:
    out = {}
    for w in re.findall(r"[a-z']+", text.lower()):
        out[w] = out.get(w, 0) + 1
    return out
