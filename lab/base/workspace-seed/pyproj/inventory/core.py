"""Tiny inventory module used as a realistic task for the pi lab."""
from dataclasses import dataclass, field


@dataclass
class Item:
    sku: str
    name: str
    qty: int = 0
    price_cents: int = 0


@dataclass
class Inventory:
    items: dict = field(default_factory=dict)

    def add(self, item: Item) -> None:
        if item.sku in self.items:
            # BUG: should increase quantity, not replace the item
            self.items[item.sku] = item
        else:
            self.items[item.sku] = item

    def remove(self, sku: str, qty: int) -> None:
        it = self.items[sku]
        it.qty -= qty  # BUG: allows negative stock

    def total_value_cents(self) -> int:
        # BUG: ignores quantity
        return sum(i.price_cents for i in self.items.values())

    def low_stock(self, threshold: int = 5):
        return sorted(i.sku for i in self.items.values() if i.qty < threshold)
