import type { Config } from "tailwindcss";

export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#0f172a",
        muted: "#64748b",
        line: "#e2e8f0",
        ok: "#059669",
        warn: "#d97706",
        bad: "#dc2626",
        brand: "#2563eb",
      },
    },
  },
  plugins: [],
} satisfies Config;
