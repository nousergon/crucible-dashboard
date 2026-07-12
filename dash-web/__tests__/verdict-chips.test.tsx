import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { VerdictChips } from "@/components/verdict-chips";

describe("VerdictChips", () => {
  const verdicts = [
    { tile: "research", status: "RED", graded: 9, total: 13, reason: "scanner IC negative at n=24 weeks" },
    { tile: "behavioral", status: "GREEN", graded: 4, total: 5, reason: "all graded components within bands" },
  ];
  const details = {
    research: [{
      metric: "scanner", value: "-0.0025", ci: "[-0.05, 0.05]", n: 24, target: 0.05,
      red_line: -0.02, trend: "→", criticality: "critical", status: "RED",
      reason: "scanner IC negative at n=24 weeks",
    }],
  };

  it("renders each verdict with its reason — honest REDs stay visible", () => {
    render(<VerdictChips verdicts={verdicts} details={details} />);
    expect(screen.getByText("research")).toBeInTheDocument();
    expect(screen.getByText("scanner IC negative at n=24 weeks")).toBeInTheDocument();
    expect(screen.getByText("9/13")).toBeInTheDocument();
  });

  it("clicking a tile expands its full MetricRecord table", async () => {
    const { default: userEvent } = await import("@testing-library/user-event");
    render(<VerdictChips verdicts={verdicts} details={details} />);
    expect(screen.queryByText("Red line")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /research/i }));
    expect(screen.getByText("Red line")).toBeInTheDocument();
    expect(screen.getByText("scanner")).toBeInTheDocument();
    expect(screen.getByText("-0.0025")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /research/i })); // toggle closed
    expect(screen.queryByText("Red line")).not.toBeInTheDocument();
  });

  it("empty state is explicit, never a blank section", () => {
    render(<VerdictChips verdicts={[]} details={{}} />);
    expect(screen.getByText(/No graded card published yet/)).toBeInTheDocument();
  });
});
