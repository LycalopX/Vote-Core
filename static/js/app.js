/* Micro-interactions and UI enhancements */
document.addEventListener('DOMContentLoaded', () => {
    // Smooth page transitions
    document.body.style.opacity = '0';
    requestAnimationFrame(() => {
        document.body.style.transition = 'opacity 0.3s ease';
        document.body.style.opacity = '1';
    });
});
