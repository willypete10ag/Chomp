// Appearance (#105): System, Dark or Light, picked on You and kept on this device. Loaded
// without defer in the page head, so the page draws in the right theme from the start.
(function () {
  try {
    var theme = localStorage.getItem("chomp-theme");
    if (theme === "dark" || theme === "light") {
      document.documentElement.setAttribute("data-theme", theme);
    }
  } catch (err) {
    // private browsing: the device's setting applies
  }
})();
