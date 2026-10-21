/**
 * Applies the saved theme before the page is painted.
 *
 * This runs in the document head, before any content is rendered, so a reader
 * who chose the dark theme never sees a light flash. It is a classic external
 * script on purpose: the page's Content-Security-Policy allows scripts from
 * this origin only, never an inline one.
 *
 * The saved preference and the system preference are read separately: a
 * browser that refuses storage still resolves the system preference and marks
 * the document, so the page never paints in the wrong theme. The marker is
 * always explicit, so the stylesheet never has to guess.
 */
(function () {
  var STORAGE_KEY = 'apt-theme'
  var root = document.documentElement

  var stored = null
  try {
    stored = window.localStorage.getItem(STORAGE_KEY)
  } catch (error) {
    // A browser that refuses storage keeps the system preference.
    stored = null
  }

  var prefersDark = false
  try {
    prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches
  } catch (error) {
    prefersDark = false
  }

  var dark = stored === 'dark' || (stored !== 'light' && prefersDark)
  root.classList.remove('light', 'dark')
  root.classList.add(dark ? 'dark' : 'light')
})()
