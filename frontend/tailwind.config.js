/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        navy: {
          900: '#080f16',
          800: '#0a1520',
          700: '#0c1e2e',
          600: '#0c3649',
          500: '#0e4a63',
        },
      },
    },
  },
  plugins: [],
}
