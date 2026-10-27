import type { Config } from 'tailwindcss'

// Design tokens. The product color is light blue ("תכלת"): `brand` is the only
// accent; `ink` covers text. Everything else is white and soft grey-blue.
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#f0f9ff',
          100: '#e0f2fe',
          200: '#bae6fd',
          300: '#7dd3fc',
          400: '#38bdf8',
          500: '#0ea5e9',
          600: '#0284c7',
          700: '#0369a1',
          800: '#075985',
          900: '#0c4a6e',
        },
        ink: {
          DEFAULT: '#0f172a',
          soft: '#475569',
          muted: '#94a3b8',
        },
        surface: '#f5fafe',
      },
      fontFamily: {
        sans: ['Heebo', 'system-ui', 'Segoe UI', 'Arial', 'sans-serif'],
      },
      boxShadow: {
        card: '0 1px 2px rgba(2, 132, 199, 0.06), 0 10px 30px -16px rgba(2, 132, 199, 0.35)',
        pop: '0 12px 40px -12px rgba(2, 132, 199, 0.35)',
      },
    },
  },
  plugins: [],
} satisfies Config
