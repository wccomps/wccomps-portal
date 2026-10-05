/**
 * WCComps shared Alpine.js mixins and utility functions.
 *
 * Loaded globally in base_site.html before Alpine.js initializes.
 * Combine mixins with a component's own fields using compose(): Alpine.data('x', () => compose(toastMixin(), { ... })).
 */

/* ── Helpers ─────────────────────────────────────────────────────── */

/**
 * Merge objects into one, keeping getters live: object spread evaluates each getter once and copies
 * the value, this copies the property descriptors.
 */
function compose(...parts) {
    const merged = {};
    for (const part of parts) Object.defineProperties(merged, Object.getOwnPropertyDescriptors(part));
    return merged;
}

// Tell the server this browser's timezone so it shows times in it and reads date/time
// inputs in it (core.middleware.UserTimezoneMiddleware). Times are stored in UTC.
(function rememberTimezone() {
    let tz;
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { return; }
    if (!tz) return;
    const value = 'tz=' + encodeURIComponent(tz);
    if (document.cookie.split('; ').includes(value)) return;
    const secure = location.protocol === 'https:' ? '; Secure' : '';
    document.cookie = value + '; path=/; max-age=31536000; SameSite=Lax' + secure;
})();

function getCSRFToken() {
    const c = document.cookie.split('; ').find(c => c.startsWith('csrftoken='));
    if (c) return c.split('=')[1];
    // Fallback: base_site.html renders the token (cookie may be missing in a fresh session)
    const meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.content : '';
}

function _toFormData(data) {
    if (data instanceof FormData) return data;
    const fd = new FormData();
    for (const [k, v] of Object.entries(data)) fd.append(k, v);
    return fd;
}

/** POST with CSRF token, return parsed JSON. */
async function wcPost(url, data = {}) {
    const response = await fetch(url, {
        method: 'POST',
        headers: { 'X-CSRFToken': getCSRFToken() },
        body: _toFormData(data),
    });
    return response.json();
}

/** POST and read NDJSON stream. Calls onProgress(msg) per line, onDone(msg) for {done:true}. */
async function wcStream(url, data, onProgress, onDone) {
    const response = await fetch(url, {
        method: 'POST',
        headers: { 'X-CSRFToken': getCSRFToken() },
        body: _toFormData(data),
    });
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
            if (!line.trim()) continue;
            const msg = JSON.parse(line);
            if (msg.done) onDone(msg);
            else onProgress(msg);
        }
    }
}

/* ── Alpine Mixins ───────────────────────────────────────────────── */

/** Toast notification state: message, messageType, alertClass. */
function toastMixin() {
    return {
        message: '',
        messageType: 'success',
        get alertClass() {
            return this.messageType === 'success' ? 'alert--success' : 'alert--error';
        },
    };
}

/** Progress bar state and computed properties. */
function progressMixin() {
    return {
        progressStep: '',
        progressCurrent: 0,
        progressTotal: 0,
        progressFailed: 0,
        get hasProgress() { return this.progressTotal > 0; },
        get progressText() { return this.progressCurrent + '/' + this.progressTotal; },
        get progressBarClass() { return this.progressFailed > 0 ? 'progress-bar__fill--warn' : ''; },
        get progressStyle() { return 'width:' + Math.round(this.progressCurrent / this.progressTotal * 100) + '%'; },
        get hasFailures() { return this.progressFailed > 0; },
        get failedText() { return this.progressFailed + ' failed'; },
    };
}

/**
 * Streaming action mixin: toast + progress + doStreamAction().
 * @param {string} url — POST endpoint (rendered by Django template tag).
 */
function streamMixin(url) {
    return compose(toastMixin(), progressMixin(), {
        loading: false,
        get notLoading() { return !this.loading; },
        get disabledClass() { return this.loading ? 'disabled' : ''; },

        async doStreamAction(action, extraData = {}) {
            this.loading = true;
            this.message = '';
            this.progressStep = 'Starting...';
            this.progressCurrent = 0;
            this.progressTotal = 1;
            this.progressFailed = 0;

            try {
                await wcStream(
                    url,
                    { action, ...extraData },
                    (msg) => {
                        this.progressStep = msg.step;
                        this.progressCurrent = msg.current;
                        this.progressTotal = msg.total;
                        if (!msg.ok) this.progressFailed++;
                    },
                    (msg) => {
                        this.message = msg.message;
                        this.messageType = msg.success ? 'success' : 'error';
                        if (msg.success) setTimeout(() => location.reload(), 2000);
                    },
                );
            } catch (e) {
                this.message = 'Request failed: ' + e.message;
                this.messageType = 'error';
            }

            this.loading = false;
            if (!this.message) this.progressTotal = 0;
            // A failure stays until the next action: it says what didn't happen
            if (this.messageType === 'success') setTimeout(() => { this.message = ''; }, 5000);
        },
    });
}

/**
 * Bulk selection mixin for review/list pages.
 * @param {string} dataAttr — HTML data attribute suffix (e.g. 'incident-id').
 * Call this.initBulkSelect() from your init() method.
 */
function jsKey(id) {
    // Mirror of the `jskey` template filter: a valid JS identifier for a row id.
    return 'k' + String(id).replace(/\W/g, '_');
}

function bulkSelectMixin(dataAttr) {
    const camel = dataAttr.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    return {
        // Reactive map keyed by jsKey(id). Each row binds :checked="selectedMap.<key>",
        // so Alpine owns the checkbox state — the CSP build cannot evaluate selected.includes(id),
        // and an imperative sync was being clobbered by Alpine's render pass.
        selectedMap: {},
        selectableIds: [],
        submitting: false,
        initBulkSelect() {
            this.selectableIds = Array.from(
                this.$el.querySelectorAll('[data-' + dataAttr + ']'),
            ).map(el => el.dataset[camel]);
            const map = {};
            this.selectableIds.forEach(id => { map[jsKey(id)] = false; });
            this.selectedMap = map;
        },
        get selected() {
            return this.selectableIds.filter(id => this.selectedMap[jsKey(id)]);
        },
        get allSelected() {
            return this.selectableIds.length > 0
                && this.selectableIds.every(id => this.selectedMap[jsKey(id)]);
        },
        get someSelected() {
            const count = this.selected.length;
            return count > 0 && count < this.selectableIds.length;
        },
        toggleAll() {
            const target = !this.allSelected;
            const map = {};
            this.selectableIds.forEach(id => { map[jsKey(id)] = target; });
            this.selectedMap = map;
        },
        toggleItem(e) {
            this.selectedMap[jsKey(e.target.value)] = e.target.checked;
        },
    };
}
