import type { Config } from "tailwindcss";

// Mirrors metron/web's semantic-token convention: components style through
// these tokens (resolved from CSS variables in globals.css), literal Tailwind
// colors stay out of components.
const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        paper: "rgb(var(--c-paper) / <alpha-value>)",
        surface: "rgb(var(--c-surface) / <alpha-value>)",
        ink: "rgb(var(--c-ink) / <alpha-value>)",
        muted: "rgb(var(--c-muted) / <alpha-value>)",
        line: "rgb(var(--c-line) / <alpha-value>)",
        positive: "rgb(var(--c-positive) / <alpha-value>)",
        negative: "rgb(var(--c-negative) / <alpha-value>)",
        accent: "rgb(var(--c-accent) / <alpha-value>)",
        warn: "rgb(var(--c-warn) / <alpha-value>)",
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
    },
  },
  plugins: [],
};

export default config;
