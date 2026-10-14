/**
 * Shared by the competition page and the Competition setup page: posting an action to the competition action
 * endpoint, the readiness check, and showing a stored time in the viewer's zone and in California's.
 * Loaded by both pages before Alpine.js initializes.
 */

/**
 * Actions and the readiness check.
 * @param {string} url — the competition action endpoint (rendered by Django template tag).
 */
function competitionActionsMixin(url) {
    return {
        csvContent: '',
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

        setField(e) {
            this[e.target.dataset.field] = e.target.value;
        },

        // A success reloads the page the action was taken on, so a save shows what is stored now
        async doAction(action, data = {}) {
            this.loading = true;
            this.message = '';
            try {
                const result = await wcPost(url, { action, ...data });
                if (result.success) {
                    this.message = result.message;
                    this.messageType = 'success';
                    if (result.csv) this.csvContent = result.csv;
                    if (result.apps) this.apps = result.apps;
                    setTimeout(() => location.reload(), 2000);
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
        },

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
                    if (result.csv) this.csvContent = result.csv;
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
