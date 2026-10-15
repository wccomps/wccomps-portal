/**
 * The competition pages (Competition, Competition setup): the readiness check, and showing a stored time in the
 * viewer's zone and in California's. Loaded before Alpine.js initializes.
 */

/**
 * The readiness check and its fixes; compose after actionMixin or streamMixin, whose doAction it uses.
 * @param {string} url: the competition action endpoint.
 */
// Each result's word and badge class (cotton/badge.html's variants)
const READINESS_RESULTS = {
    pass: ['Pass', 'badge--success'],
    warn: ['Warning', 'badge--warning'],
    fail: ['Fail', 'badge--danger'],
};

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
                        const [resultLabel, resultClass] = READINESS_RESULTS[sev];
                        this.readinessResults.push({
                            step: msg.step,
                            resultLabel,
                            resultClass,
                            rowClass: this.readinessResults.length % 2 ? 'row2' : 'row1',
                            detail: msg.detail || '',
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
            this.readinessFixing = true;
            await this.doAction('readiness_fix', { fix: e.currentTarget.dataset.fix }, { reload: false });
            this.readinessFixing = false;
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
