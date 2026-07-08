import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatCard } from "@/components/stat-card";

describe("StatCard", () => {
  it("renders value with provenance and hover definition", () => {
    render(
      <StatCard
        stat={{ label: "Sharpe (ann.)", value: "0.36", sub: "vectorbt production sim", help: "Annualized." }}
      />,
    );
    expect(screen.getByText("0.36")).toBeInTheDocument();
    expect(screen.getByText("vectorbt production sim")).toBeInTheDocument();
    expect(screen.getByTitle("Annualized.")).toBeInTheDocument();
  });

  it("negative values wear the negative token, absent values the muted one", () => {
    const { rerender } = render(
      <StatCard stat={{ label: "Alpha", value: "-11.68%", sub: "n=83", help: "h" }} />,
    );
    expect(screen.getByText("-11.68%").className).toContain("text-negative");
    rerender(<StatCard stat={{ label: "Alpha", value: "—", sub: "absent", help: "h" }} />);
    expect(screen.getByText("—").className).toContain("text-muted");
  });
});
