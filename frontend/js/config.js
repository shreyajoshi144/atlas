// Deployment configuration. Leave empty when frontend and API share one origin.
// For a separate backend, set the deployed API origin, e.g. https://api.example.com/api
window.ATLAS_API_BASE = '';
// Public contact address shown on contact.html. Leave empty if the deployment has no public inbox.
window.ATLAS_CONTACT_EMAIL = '';

// Single source of truth for the API base URL (used by auth-page.js and script.js).
// Only this file's ATLAS_API_BASE can change it; browser storage never can, so a stale
// value from an old dev session cannot send requests to a URL the CSP (connect-src 'self') blocks.
window.AtlasApi = {
  resolveBase: function (configured, origin) {
    var c = String(configured || '').trim().replace(/\/+$/, '');
    if (!/^https?:\/\/[^\s/]+/i.test(c)) return origin + '/api';   // empty or malformed: same origin
    return /\/api$/.test(c) ? c : c + '/api';
  },
};
// Remove the override key written by earlier builds.
try { localStorage.removeItem('atlas_api_base'); } catch (e) { /* storage unavailable */ }
