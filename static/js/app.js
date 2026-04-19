/* Micro-interactions and UI enhancements */

/* Page fade-in is handled via CSS to avoid flash of invisible content.
   The body starts at opacity:1 by default. The animate-in class on cards
   handles the per-element entrance animations. */

document.addEventListener('DOMContentLoaded', () => {
    // Add loaded class for any CSS-driven page transitions
    document.body.classList.add('loaded');
});
