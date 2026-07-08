import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { VerdictChips } from "@/components/verdict-chips";

describe("VerdictChips", () => {
  it("renders each verdict with its reason — honest REDs stay visible", () => {
    render(
      <VerdictChips
        verdicts={[
          { tile: "research", status: "RED", graded: 9, total: 13, reason: "scanner IC negative at n=24 weeks" },
          { tile: "behavioral", status: "GREEN", graded: 4, total: 5, reason: "all graded components within bands" },
        ]}
      />,
    );
    expect(screen.getByText("research")).toBeInTheDocument();
    expect(screen.getByText("scanner IC negative at n=24 weeks")).toBeInTheDocument();
    expect(screen.getByText("9/13")).toBeInTheDocument();
  });

  it("empty state is explicit, never a blank section", () => {
    render(<VerdictChips verdicts={[]} />);
    expect(screen.getByText(/No graded card published yet/)).toBeInTheDocument();
  });
});
