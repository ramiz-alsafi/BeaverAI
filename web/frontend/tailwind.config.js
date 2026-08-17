/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Same palette as the old static UI, ported so the visual
        // identity doesn't jump around while we rebuild underneath it.
        ink:      "#12100d",   // page background
        panel:    "#1b1815",   // bubble/panel background
        cream:    "#ece4d6",   // primary text
        muted:    "#8f887c",   // secondary text
        gold:     "#d9a441",   // accent / spinner
        goldDim:  "#8a6a2c",
        sage:     "#7fa66b",   // success
        rust:     "#c1666b",   // error/stop
        amber:    "#e0a52f",   // running state
      },
      fontFamily: {
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
      },
      keyframes: {
        // Very slow, very subtle drift for the background glows in App.tsx
        // — long duration + small travel distance so it reads as "alive"
        // ambience rather than an obvious animation.
        drift: {
          "0%, 100%": { transform: "translate(0, 0) scale(1)" },
          "50%": { transform: "translate(1.5%, 1%) scale(1.03)" },
        },
      },
      animation: {
        drift: "drift 22s ease-in-out infinite",
        "drift-slow": "drift 30s ease-in-out infinite reverse",
      },
    },
  },
  plugins: [],
};
