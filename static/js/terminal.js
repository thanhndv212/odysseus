// Terminal Panel — interactive shell via xterm.js + backend PTY SSE
(function() {
    'use strict';

    const MODAL_ID = 'terminal-modal';
    const Modals = () => window._Modals;

    let term = null;          // Terminal instance
    let fitAddon = null;      // FitAddon instance
    let eventSource = null;   // SSE EventSource
    let sessionId = null;     // Backend PTY session ID
    let modal = null;
    let _resizeObserver = null;

    function open() {
        if (term) {
            // Already open — restore if minimized
            if (Modals() && Modals().isMinimized(MODAL_ID)) {
                Modals().restore(MODAL_ID);
            } else if (modal) {
                modal.classList.remove('hidden');
            }
            if (fitAddon) { setTimeout(() => fitAddon.fit(), 50); }
            term.focus();
            return;
        }

        modal = document.getElementById(MODAL_ID);
        const container = document.getElementById('terminal-container');
        if (!modal || !container) return;

        // Register with modalManager for minimize/dock/drag lifecycle
        if (Modals()) {
            if (Modals().isMinimized(MODAL_ID)) {
                Modals().restore(MODAL_ID);
                if (fitAddon) { setTimeout(() => fitAddon.fit(), 50); }
                term.focus();
                return;
            }

            Modals().register(MODAL_ID, {
                railBtnId: 'rail-terminal',
                sidebarBtnId: 'tool-terminal-btn',
                closeFn: () => _doClose(),
                restoreFn: () => {
                    if (fitAddon) { setTimeout(() => fitAddon.fit(), 50); }
                    if (term) term.focus();
                },
            });

            // Inject minimize button (modalManager handles this)
            Modals().injectMinimizeButton(modal, MODAL_ID);
        }

        // Show the modal
        modal.classList.remove('hidden');

        // Wire drag via windowDrag module
        const content = modal.querySelector('.modal-content');
        const header = content ? content.querySelector('.modal-header') : null;
        if (window._makeWindowDraggable && content && header) {
            window._makeWindowDraggable(modal, { content, header });
        }

        // Wire close button via modalManager
        const closeBtn = document.getElementById('terminal-close-btn');
        if (closeBtn) {
            closeBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                if (Modals()) Modals().close(MODAL_ID);
                else _doClose();
            });
        }

        // 1. Create xterm
        if (typeof Terminal === 'undefined') {
            container.textContent = 'xterm.js not loaded. Check script tags.';
            return;
        }

        term = new Terminal({
            cursorBlink: true,
            cursorStyle: 'bar',
            fontSize: 14,
            fontFamily: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Menlo', monospace",
            theme: {
                background: '#0d1117',
                foreground: '#c9d1d9',
                cursor: '#58a6ff',
                selectionBackground: '#264f78',
                black: '#484f58',
                red: '#ff7b72',
                green: '#3fb950',
                yellow: '#d29922',
                blue: '#58a6ff',
                magenta: '#bc8cff',
                cyan: '#39c2d3',
                white: '#b1bac4',
                brightBlack: '#6e7681',
                brightRed: '#ffa198',
                brightGreen: '#56d364',
                brightYellow: '#e3b341',
                brightBlue: '#79c0ff',
                brightMagenta: '#d2a8ff',
                brightCyan: '#56d4dd',
                brightWhite: '#f0f6fc',
            },
            allowProposedApi: true,
        });

        // 2. Fit addon
        if (typeof FitAddon !== 'undefined') {
            fitAddon = new FitAddon.FitAddon();
            term.loadAddon(fitAddon);
        }
        term.open(container);
        if (fitAddon) { setTimeout(() => fitAddon.fit(), 50); }

        // 3. Connect SSE
        connectSSE();

        // 4. Keyboard input → send to backend
        term.onData((data) => {
            if (!sessionId) return;
            fetch('/api/shell/pty/input', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId, input: data }),
            }).catch(() => {});
        });

        // 5. Resize handler
        term.onResize(({ cols, rows }) => {
            if (!sessionId) return;
            fetch('/api/shell/pty/resize', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId, cols: cols, rows: rows }),
            }).catch(() => {});
        });

        // 6. Window resize → refit
        window.addEventListener('resize', _onWindowResize);

        // 7. Container resize (modal resize handle) → refit
        if (typeof ResizeObserver !== 'undefined') {
            _resizeObserver = new ResizeObserver(() => {
                if (fitAddon && modal && !modal.classList.contains('hidden')) {
                    fitAddon.fit();
                }
            });
            _resizeObserver.observe(container);
        }

        // 8. Click to reconnect if connection lost
        container.addEventListener('click', () => {
            if (!eventSource || eventSource.readyState === EventSource.CLOSED) {
                if (term) term.write('\r\n\x1b[33mReconnecting...\x1b[0m\r\n');
                connectSSE();
                if (term) term.focus();
            }
        });

        term.focus();
    }

    function _onWindowResize() {
        if (fitAddon && modal && !modal.classList.contains('hidden')) {
            fitAddon.fit();
        }
    }

    function connectSSE() {
        if (eventSource) { eventSource.close(); eventSource = null; }
        sessionId = null;

        const cols = term ? term.cols : 80;
        const rows = term ? term.rows : 24;

        eventSource = new EventSource('/api/shell/pty/start?cmd=bash&cols=' + cols + '&rows=' + rows);

        eventSource.addEventListener('message', (e) => {
            try {
                const msg = JSON.parse(e.data);
                if (msg.type === 'session') {
                    sessionId = msg.session_id;
                } else if (msg.type === 'output') {
                    if (term) term.write(msg.data);
                } else if (msg.type === 'exit') {
                    if (term) term.write('\r\n\x1b[33m[Process exited with code ' + msg.code + ']\x1b[0m\r\n');
                    sessionId = null;
                } else if (msg.type === 'error') {
                    if (term) term.write('\r\n\x1b[31m[Error: ' + msg.message + ']\x1b[0m\r\n');
                }
            } catch (err) {
                if (term) term.write(e.data);
            }
        });

        eventSource.addEventListener('error', () => {
            if (eventSource && eventSource.readyState === EventSource.CLOSED) {
                if (term && sessionId) {
                    term.write('\r\n\x1b[31m[Connection lost — click terminal to reconnect]\x1b[0m\r\n');
                }
                sessionId = null;
            }
        });
    }

    function _doClose() {
        if (eventSource) { eventSource.close(); eventSource = null; }
        sessionId = null;
        if (_resizeObserver) { _resizeObserver.disconnect(); _resizeObserver = null; }
        if (term) {
            term.dispose();
            term = null;
            fitAddon = null;
        }
        if (modal) modal.classList.add('hidden');
        window.removeEventListener('resize', _onWindowResize);
    }

    function close() {
        if (Modals() && Modals().isRegistered(MODAL_ID)) {
            Modals().close(MODAL_ID);
        } else {
            _doClose();
        }
    }

    function minimize() {
        if (Modals() && Modals().isRegistered(MODAL_ID)) {
            Modals().minimize(MODAL_ID);
        } else if (modal) {
            modal.classList.add('hidden');
        }
        // Keep SSE + xterm alive so state is preserved
    }

    function restore() {
        if (Modals() && Modals().isMinimized(MODAL_ID)) {
            Modals().restore(MODAL_ID);
        } else if (modal) {
            modal.classList.remove('hidden');
        }
        if (fitAddon) { setTimeout(() => fitAddon.fit(), 50); }
        if (term) term.focus();
    }

    function focus() {
        if (term) term.focus();
    }

    function isOpen() {
        return term !== null;
    }

    window.terminalManager = { open, close, minimize, restore, focus, isOpen };

    // Self-wire click handlers
    function _wireButtons() {
        const sidebarBtn = document.getElementById('tool-terminal-btn');
        if (sidebarBtn) {
            sidebarBtn.addEventListener('click', () => open());
        } else {
            setTimeout(_wireButtons, 500);
            return;
        }
        const railBtn = document.getElementById('rail-terminal');
        if (railBtn) {
            railBtn.addEventListener('click', () => open());
        }
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', _wireButtons);
    } else {
        _wireButtons();
    }
})();
