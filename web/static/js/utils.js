/**
 * WCComps shared Alpine.js mixins and utility functions.
 *
 * Loaded globally in base_site.html before Alpine.js initializes.
 * Combine mixins with a component's own fields using compose(): Alpine.data('x', () => compose(toastMixin(), { ... })).
 */

/* Helpers */

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
        // Asking for JSON gets a refusal's reason as JSON on any browser (core.errors.wants_json_error)
        headers: { 'X-CSRFToken': getCSRFToken(), 'Accept': 'application/json' },
        body: _toFormData(data),
    });
    return response.json();
}

/** POST and read NDJSON stream. Calls onProgress(msg) per line, onDone(msg) for {done:true}. */
async function wcStream(url, data, onProgress, onDone) {
    const response = await fetch(url, {
        method: 'POST',
        // Asking for JSON gets a refusal's reason as JSON on any browser (core.errors.wants_json_error)
        headers: { 'X-CSRFToken': getCSRFToken(), 'Accept': 'application/json' },
        body: _toFormData(data),
    });
    if (!response.ok) {
        // A refusal is a JSON error (core.errors), not a stream: the caller's catch shows it
        const refusal = await response.json().catch(() => ({}));
        throw new Error(refusal.error || 'the server answered ' + response.status);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let sawDone = false;
    const handleLine = (line) => {
        if (!line.trim()) return;
        const msg = JSON.parse(line);
        if (msg.done) {
            sawDone = true;
            onDone(msg);
        } else {
            onProgress(msg);
        }
    };
    while (true) {
        const { done, value } = await reader.read();
        if (done) {
            buffer += decoder.decode();
            handleLine(buffer);
            break;
        }
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop();
        for (const line of lines) {
            handleLine(line);
        }
    }
    if (!sawDone) {
        throw new Error('Stream ended unexpectedly');
    }
}

/** Keeps a ToggleButtonSelect(Multiple)'s buttons and hidden select in step; each click fires the select's change. */
function togglePicker() {
    return {
        init() {
            const select = this.$el.querySelector('select');
            const buttons = this.$el.querySelectorAll('[data-value]');
            const show = () => {
                const picked = new Set(Array.from(select.selectedOptions, option => option.value));
                buttons.forEach(button => button.setAttribute('aria-pressed', String(picked.has(button.dataset.value))));
            };
            this.$el.addEventListener('click', event => {
                const button = event.target.closest('button');
                if (!button) return;
                if (button.dataset.value !== undefined) {
                    const option = Array.from(select.options).find(o => o.value === button.dataset.value);
                    option.selected = !option.selected;
                } else {
                    for (const option of select.options) option.selected = button.dataset.pick === 'all';
                }
                select.dispatchEvent(new Event('change', { bubbles: true }));
            });
            select.addEventListener('change', show);
            show();
        }
    };
}
document.addEventListener('alpine:init', () => Alpine.data('togglePicker', togglePicker));

/* Alpine Mixins */

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

        // A button's stream action, its data-action (its data-confirm is asked first)
        runStreamAction(e) {
            return this.doStreamAction(e.currentTarget.dataset.action);
        },

        // Streams the action and shows its progress and message; returns the stream's last message, or null if the
        // request failed. A success reloads the page unless reload is false, as with doAction.
        async doStreamAction(action, extraData = {}, { reload = true } = {}) {
            this.streamAction = action;
            this.loading = true;
            this.message = '';
            this.progressStep = 'Starting...';
            this.progressCurrent = 0;
            this.progressTotal = 1;
            this.progressFailed = 0;
            let done = null;

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
                        done = msg;
                        this.message = msg.message;
                        this.messageType = msg.success ? 'success' : 'error';
                        if (msg.success && reload) setTimeout(() => location.reload(), 2000);
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
            return done;
        },
    });
}

function jsKey(id) {
    // Mirror of the `jskey` template filter: a valid JS identifier for a row id.
    return 'k' + String(id).replace(/\W/g, '_');
}

/* Forms: asking first, and posting once */

// A c-form (data-post-once) posts once: the first submit marks it data-posting and disables its submit buttons until
// the page is left, and a later submit is dropped. Before that, any form's data-confirm question is asked, read as
// text. Capture runs before the form's own handlers, so a dropped or cancelled submit stops them too. Other forms,
// Django admin's among them (an action that only downloads a file leaves the page as it was), aren't guarded.
document.addEventListener('submit', (e) => {
    const form = e.target;
    const question = form.dataset.confirm;
    if (form.dataset.posting || (question && !confirm(question))) {
        e.preventDefault();
        e.stopImmediatePropagation();
    }
}, true);

// A button's or link's data-confirm is asked the same way when it is clicked, before the page's own click handler
// (an Alpine @click that posts by fetch): a question answered no stops the click. ::data-confirm binds a question built
// from the page's state; an empty one asks nothing.
document.addEventListener('click', (e) => {
    const control = e.target.closest('button[data-confirm], a[data-confirm]');
    if (control && control.dataset.confirm && !confirm(control.dataset.confirm)) {
        e.preventDefault();
        e.stopImmediatePropagation();
    }
}, true);

// Marked last (window, bubbling), so a form whose own handler cancels the submit (one that posts by fetch) isn't
// marked
window.addEventListener('submit', (e) => {
    const form = e.target;
    if (e.defaultPrevented || !('postOnce' in form.dataset)) return;
    form.dataset.posting = 'true';
    // Disabled after the browser has read the form, so the button pressed still sends its name and value
    // (form.elements: also a button outside the form that joins it with form=)
    setTimeout(() => [...form.elements]
        .filter(control => control.type === 'submit' && !control.disabled)
        .forEach(button => { button.disabled = true; button.dataset.postingDisabled = 'true'; }));
});

// A page brought back by Back posts again. A POST that fails without leaving the page (offline, Stop) keeps its
// form blocked until a reload, and a refresh covering that form keeps skipping its turns until then too.
window.addEventListener('pageshow', (e) => {
    if (!e.persisted) return;
    document.querySelectorAll('form[data-posting]').forEach(form => delete form.dataset.posting);
    document.querySelectorAll('[data-posting-disabled]').forEach(button => {
        button.disabled = false;
        delete button.dataset.postingDisabled;
    });
});

// A poll (a request no event triggered, like the ticket list's 30 s refresh) replaces what it targets, so it skips its
// turn while a form there is posting: the swap would reset its guard. A request the person made goes ahead.
document.addEventListener('htmx:beforeRequest', (e) => {
    if (e.detail.requestConfig && e.detail.requestConfig.triggeringEvent) return;
    if (e.detail.target.querySelector('form[data-posting]')) e.preventDefault();
});

/* The ticket list a tab last showed: its ticket pages' way back */

function rememberTicketList() {
    try { sessionStorage.setItem('ticketListQuery', location.search); } catch (e) { /* storage blocked: plain list */ }
}

// On a ticket page's breadcrumb (c-page_header x-data="ticketListLink"): its list link gets the tab's list query
function ticketListLink() {
    return {
        init() {
            let query = '';
            try { query = sessionStorage.getItem('ticketListQuery') || ''; } catch (e) { /* storage blocked */ }
            const link = this.$el.querySelector('a');
            if (link && query) link.search = query;
        },
    };
}

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
        // so Alpine owns the checkbox state: the CSP build cannot evaluate selected.includes(id),
        // and an imperative sync was being clobbered by Alpine's render pass.
        selectedMap: {},
        selectableIds: [],
        init() {
            this.readRows();
            // A refresh that swaps the rows (the ticket list's) keeps what is still listed checked
            this.$el.addEventListener('htmx:afterSettle', () => this.readRows());
        },
        readRows() {
            this.selectableIds = Array.from(
                this.$el.querySelectorAll('[data-' + dataAttr + ']'),
            ).map(el => el.dataset[camel]);
            const map = {};
            this.selectableIds.forEach(id => { map[jsKey(id)] = Boolean(this.selectedMap[jsKey(id)]); });
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
