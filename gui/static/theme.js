(function () {
  var STORAGE_KEY = 'meross-theme';
  var DARK = 'dim';
  var LIGHT = 'light';

  function getTheme() {
    var saved = localStorage.getItem(STORAGE_KEY);
    if (saved === 'dark') return DARK;
    if (saved === DARK || saved === LIGHT) return saved;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? DARK : LIGHT;
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem(STORAGE_KEY, theme);
    document.querySelectorAll('.theme-icon').forEach(function (el) {
      el.className = 'theme-icon fas ' + (theme === DARK ? 'fa-moon' : 'fa-sun');
    });
  }

  function toggleTheme() {
    var current = document.documentElement.getAttribute('data-theme');
    applyTheme(current === DARK ? LIGHT : DARK);
  }

  applyTheme(getTheme());

  window.MerossTheme = { toggle: toggleTheme, apply: applyTheme, get: getTheme };
})();
