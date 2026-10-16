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

/** Shows a c-toggle_button as pressed or not: its pressed look and, for screen readers, aria-pressed. */
function setTogglePressed(button, pressed) {
    button.classList.toggle('box-toggle--selected', pressed);
    button.setAttribute('aria-pressed', String(pressed));
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
 * Plain action mixin: toast + loading + doAction().
 * @param {string} url: the POST endpoint; a call can name another with { url }.
 */
function actionMixin(url) {
    return compose(toastMixin(), {
        loading: false,

        setField(e) {
            this[e.target.dataset.field] = e.target.value;
        },

        // Posts the action and shows its message; returns the response, or null if the request failed. A success
        // reloads the page (so it shows what is stored now) unless reload is false: a result to keep on screen.
        async doAction(action, data = {}, { reload = true, url: target = url } = {}) {
            this.loading = true;
            this.message = '';
            let result = null;
            try {
                result = await wcPost(target, { action, ...data });
                this.message = result.success ? result.message : (result.error || result.message || 'Action failed');
                this.messageType = result.success ? 'success' : 'error';
                if (result.success && reload) setTimeout(() => location.reload(), 1500);
            } catch (e) {
                this.message = 'Request failed: ' + e.message;
                this.messageType = 'error';
            }
            this.loading = false;
            // A failure stays until the next action: it says what didn't happen
            if (this.messageType === 'success') setTimeout(() => { this.message = ''; }, 5000);
            return result;
        },
    });
}

/**
 * Streaming action mixin: actionMixin + progress + doStreamAction().
 * @param {string} url: the POST endpoint.
 */
function streamMixin(url) {
    return compose(actionMixin(url), progressMixin(), {

        streamAction: '',

        async doStreamAction(action, extraData = {}) {
            this.streamAction = action;
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

function jsKey(id) {
    // Mirror of the `jskey` template filter: a valid JS identifier for a row id.
    return 'k' + String(id).replace(/\W/g, '_');
}

/* ── Forms: asking first, and posting once ─────────────────────────── */

// The guard state of the Alpine component a form belongs to, or an empty object outside one.
function guardOf(form) {
    return window.Alpine ? Alpine.$data(form) : {};
}

// Any form's data-confirm is the question asked before it submits, read as text. Capture runs before the form's
// own handlers, so a cancelled question stops them; a form already submitting isn't asked again.
document.addEventListener('submit', (e) => {
    const question = e.target.dataset.confirm;
    if (!question || guardOf(e.target).submitting) return;
    if (!confirm(question)) {
        e.preventDefault();
        e.stopImmediatePropagation();
    }
}, true);

/**
 * Double-submit protection for a POST form (c-form's submitGuard; bind @submit="onSubmit"): the first submit goes
 * through and later ones are blocked until the page is left. A page brought back by Back, or an htmx request that
 * finished, frees its forms again (releaseForms below).
 */
function submitGuardMixin() {
    return {
        submitting: false,
        onSubmit(e) {
            if (this.submitting) { e.preventDefault(); return; }
            this.submitting = true;
        },
    };
}

function releaseForms(root) {
    const forms = root.tagName === 'FORM' ? [root] : root.querySelectorAll('form');
    forms.forEach(form => { const guard = guardOf(form); if (guard.submitting) guard.submitting = false; });
}
window.addEventListener('pageshow', (e) => { if (e.persisted) releaseForms(document); });
document.addEventListener('htmx:afterRequest', (e) => releaseForms(e.detail.elt.closest('form') || e.detail.elt));

/**
 * Bulk selection for a review table whose rows carry data-<dataAttr>="<id>" checkboxes. approveQuestion is the
 * bulk form's data-confirm (::data-confirm="approveQuestion"): it counts the checked rows, and with none checked
 * asks nothing, so the server says nothing was selected.
 * @param {string} dataAttr  the rows' data attribute suffix (e.g. 'incident-id')
 * @param {string} noun      what a row is, for the question (e.g. 'incident')
 */
function bulkSelectMixin(dataAttr, noun) {
    const camel = dataAttr.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    return {
        // Reactive map keyed by jsKey(id). Each row binds :checked="selectedMap.<key>",
        // so Alpine owns the checkbox state — the CSP build cannot evaluate selected.includes(id),
        // and an imperative sync was being clobbered by Alpine's render pass.
        selectedMap: {},
        selectableIds: [],
        init() { this.initBulkSelect(); },
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
        get approveQuestion() {
            const count = this.selected.length;
            return count ? `Approve ${count} ${noun}(s)? This cannot be undone.` : '';
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
