/**
 * The competition pages (Competition, Competition setup, Danger Zone): posting to the competition action endpoint,
 * the readiness check, and showing a stored time in the viewer's zone and in California's.
 * Loaded before Alpine.js initializes.
 */

/**
 * Streamed and plain actions with the toast and progress bar.
 * @param {string} url: the competition action endpoint (rendered by Django template tag).
 */
function competitionActionsMixin(url) {
    return compose(streamMixin(url), {
        setField(e) {
            this[e.target.dataset.field] = e.target.value;
        },

        // Posts the action, shows its message and returns the response (null if the request failed). A success
        // reloads the page, so a save shows what is stored now, unless the caller keeps a result on screen
        // (reload: false).
        async doAction(action, data = {}, { reload = true } = {}) {
            this.loading = true;
            this.message = '';
            let result = null;
            try {
                result = await wcPost(url, { action, ...data });
                if (result.success) {
                    this.message = result.message;
                    this.messageType = 'success';
                    if (reload) setTimeout(() => location.reload(), 2000);
                } else {
                    this.message = result.error || 'Action failed';
                    this.messageType = 'error';
                }
            } catch (e) {
                this.message = 'Request failed: ' + e.message;
                this.messageType = 'error';
            }
            this.loading = false;
            setTimeout(() => this.message = '', 5000);
            return result;
        },
    });
}

/**
 * The readiness check and its fixes; compose after competitionActionsMixin, whose toast it uses.
 * @param {string} url: the competition action endpoint.
 */
function readinessMixin(url) {
    return {
        readinessResults: [],
        readinessLoading: false,
        readinessFixing: false,
        readinessDone: false,
        readinessSuccess: false,
        readinessSummary: '',

        get notReadinessLoading() { return !this.readinessLoading; },
        get hasReadinessResults() { return this.readinessResults.length > 0; },
        get readinessOk() { return this.readinessDone && this.readinessSuccess; },
        get readinessNotOk() { return this.readinessDone && !this.readinessSuccess; },

        async runReadinessChecks() {
            this.readinessLoading = true;
            this.readinessResults = [];
            this.readinessDone = false;
            this.readinessSummary = '';
            try {
                await wcStream(
                    url,
                    { action: 'readiness_check' },
                    (msg) => {
                        const sev = msg.severity || (msg.ok ? 'pass' : 'fail');
                        const act = msg.action || null;
                        this.readinessResults.push({
                            step: msg.step,
                            severity: sev,
                            detail: msg.detail || '',
                            isPass: sev === 'pass',
                            isFail: sev === 'fail',
                            isWarn: sev === 'warn',
                            isLinkAction: act && act.type === 'link',
                            isFixAction: act && act.type === 'fix',
                            actionUrl: act ? act.url : '',
                            actionLabel: act ? act.label : '',
                            fixKey: act ? (act.key || '') : '',
                        });
                    },
                    (msg) => {
                        this.readinessSummary = msg.message;
                        this.readinessSuccess = msg.success;
                        this.readinessDone = true;
                    },
                );
            } catch (e) {
                this.message = 'Readiness check failed: ' + e.message;
                this.messageType = 'error';
            }
            this.readinessLoading = false;
        },

        async doReadinessFix(e) {
            const key = e.currentTarget.dataset.fix;
            this.readinessFixing = true;
            try {
                const result = await wcPost(url, { action: 'readiness_fix', fix: key });
                if (result.success) {
                    this.message = result.message;
                    this.messageType = 'success';
                } else {
                    this.message = result.error || 'Fix failed';
                    this.messageType = 'error';
                }
            } catch (e) {
                this.message = 'Fix failed: ' + e.message;
                this.messageType = 'error';
            }
            this.readinessFixing = false;
            setTimeout(() => this.message = '', 5000);
        },
    };
}

document.addEventListener('alpine:init', () => {
    // <span x-data="formatTime" data-iso="..." x-text="formatted">: the time in the viewer's zone and in California's
    Alpine.data('formatTime', function() {
        return {
            formatted: '',
            init() {
                const date = new Date(this.$el.dataset.iso);
                const opts = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true };
                const local = date.toLocaleString('en-US', opts);
                const ca = date.toLocaleString('en-US', { ...opts, timeZone: 'America/Los_Angeles' });
                this.formatted = `${local} (Local) / ${ca} (CA)`;
            }
        };
    });
});
