// Shopping cart helpers.
export function subtotal(items) {
	return items.reduce((sum, item) => sum + item.price, 0);
}

export function applyDiscount(total, percent) {
	return total - total * percent;
}

export function total(items, discountPercent = 0) {
	return Math.round(applyDiscount(subtotal(items), discountPercent) * 100) / 100;
}
