// Terminal Panel — interactive shell via xterm.js + backend PTY SSE
(function() {
    'use strict';

    let term = null;          // Terminal instance
    let fitAddon = null;      // FitAddon instance
    let eventSource = null;   // SSE EventSource
    let sessionId = null;     // Backend PTY session ID
    let modal = null;
    let isMinimized = false;

    function open() {
        if (term) {
            if (modal) modal.classList.remove('hidden');
            isMinimized = false;
            if (fitAddon) { setTimeout(() => fitAddon.fit(), 50); }
            term.focus();
            return;
        }

        modal = document.getElementById('terminal-modal');
        const container = document.getElementById('terminal-container');
        if (!modal || !container) return;
        modal.classList.remove('hidden');

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

        // 6. Wire minimize / close buttons
        const minimizeBtn = document.getElementById('terminal-minimize-btn');
        if (minimizeBtn) {
            minimizeBtn.addEventListener('click', (e) => { e.stopPropagation(); minimize(); });
        }
        const closeBtn = document.getElementById('terminal-close-btn');
        if (closeBtn) {
            closeBtn.addEventListener('click', (e) => { e.stopPropagation(); close(); });
        }

        // 7. Window resize → refit
        window.addEventListener('resize', _onWindowResize);

        // 8. Container resize (user drags modal corner) → refit
        if (typeof ResizeObserver !== 'undefined') {
            const _resizeObserver = new ResizeObserver(() => {
                if (fitAddon && modal && !modal.classList.contains('hidden')) {
                    fitAddon.fit();
                }
            });
            _resizeObserver.observe(container);
        }

        // 9. Click to reconnect if connection lost
        container.addEventListener('click', () => {
            if (!eventSource || eventSource.readyState === EventSource.CLOSED) {
                if (term) {
                    term.write('\r\n\x1b[33mReconnecting...\x1b[0m\r\n');
                }
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
        // Kill any existing connection
        if (eventSource) {
            eventSource.close();
            eventSource = null;
        }
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
                // Non-JSON data — write raw to terminal
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

    function close() {
        if (eventSource) { eventSource.close(); eventSource = null; }
        sessionId = null;
        if (term) {
            term.dispose();
            term = null;
            fitAddon = null;
        }
        if (modal) modal.classList.add('hidden');
        isMinimized = false;
        window.removeEventListener('resize', _onWindowResize);
    }

    function minimize() {
        if (modal) modal.classList.add('hidden');
        isMinimized = true;
        // Keep SSE + xterm alive so state is preserved
    }

    function restore() {
        if (modal) modal.classList.remove('hidden');
        isMinimized = false;
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

    // Self-wire: attach click handlers directly
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
