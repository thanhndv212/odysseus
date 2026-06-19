// hljsLoader.js — lazy-load highlight.js on demand
let _hljsPromise = null;

export function ensureHljs() {
    if (window.hljs) return Promise.resolve(window.hljs);
    if (_hljsPromise) return _hljsPromise;
    _hljsPromise = new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = '/static/lib/highlight.min.js';
        s.onload = () => resolve(window.hljs);
        s.onerror = (e) => reject(new Error('Failed to load highlight.js'));
        document.head.appendChild(s);
    });
    return _hljsPromise;
}
