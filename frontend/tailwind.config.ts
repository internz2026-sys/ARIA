import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        primary: { DEFAULT: "#534AB7", light: "#EEEDFE" },
        success: "#1D9E75",
        warning: "#BA7517",
        danger: "#D85A30",
        "bg-secondary": "#F8F8F6",
        "text-primary": "#2C2C2A",
        "text-secondary": "#5F5E5A",
        border: "#E0DED8",
      },
      borderRadius: {
        card: "12px",
        input: "8px",
        cta: "24px",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
      },
      boxShadow: {
        card: "0 2px 8px rgba(0,0,0,0.06)",
      },
    },
  },
  plugins: [],
};

export default config;
