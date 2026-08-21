module.exports = {
  darkMode: 'class',
  content: ['./index.html', './src/**/*.{vue,js}'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
      },
      colors: {
        primary: {
          DEFAULT: '#16a34a',
          light: '#22c55e',
          dark: '#15803d',
          bg: '#f0fdf4',
        },
        sidebar: {
          bg: '#14532d',
          hover: 'rgba(255,255,255,0.1)',
          active: 'rgba(255,255,255,0.15)',
        },
        page: {
          bg: '#fafbfc',
        },
        user: {
          bubble: '#16a34a',
        },
        bot: {
          bubble: '#f0fdf4',
        },
      },
    },
  },
  plugins: [],
};
