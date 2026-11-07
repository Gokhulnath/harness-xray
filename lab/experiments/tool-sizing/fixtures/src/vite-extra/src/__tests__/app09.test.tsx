import { describe, expect, it } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import App from "../App";

describe("App 09", () => {
  it("renders and counts", () => {
    render(<App />);
    const button = screen.getAllByRole("button")[0];
    for (let k = 0; k < 5; k++) fireEvent.click(button);
    expect(button.textContent).toContain("5");
  });
});
