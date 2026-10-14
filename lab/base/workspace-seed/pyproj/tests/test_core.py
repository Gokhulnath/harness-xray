import unittest
from inventory import Inventory, Item


class InventoryTests(unittest.TestCase):
    def test_add_merges_quantity(self):
        inv = Inventory()
        inv.add(Item("A1", "bolt", 3, 10))
        inv.add(Item("A1", "bolt", 4, 10))
        self.assertEqual(inv.items["A1"].qty, 7)

    def test_remove_rejects_negative(self):
        inv = Inventory()
        inv.add(Item("B2", "nut", 2, 5))
        with self.assertRaises(ValueError):
            inv.remove("B2", 3)

    def test_total_value(self):
        inv = Inventory()
        inv.add(Item("A1", "bolt", 3, 10))
        inv.add(Item("C3", "gear", 2, 250))
        self.assertEqual(inv.total_value_cents(), 530)

    def test_low_stock(self):
        inv = Inventory()
        inv.add(Item("A1", "bolt", 3, 10))
        inv.add(Item("C3", "gear", 20, 250))
        self.assertEqual(inv.low_stock(), ["A1"])


if __name__ == "__main__":
    unittest.main()
