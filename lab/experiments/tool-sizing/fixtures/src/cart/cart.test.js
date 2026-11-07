import assert from "node:assert/strict";
import { test } from "node:test";
import { applyDiscount, subtotal, total } from "./cart.js";

test("subtotal multiplies price by quantity", () => {
	assert.equal(subtotal([{ price: 2.5, qty: 4 }, { price: 10, qty: 1 }]), 20);
});

test("applyDiscount takes a whole-number percent", () => {
	assert.equal(applyDiscount(200, 10), 180);
});

test("total combines both", () => {
	assert.equal(total([{ price: 5, qty: 2 }], 50), 5);
});
