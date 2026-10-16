/**
 * Reset Password for one team (admin/includes/team_password_reset.html). The component's root carries
 * data-url, the team's admin_team_action URL.
 */
document.addEventListener('alpine:init', () => {
    Alpine.data('teamPasswordReset', () => compose(actionMixin(), {
        newPassword: '',

        get resetQuestion() {
            return 'Reset this team\'s password?\n\nThis will:\n- Generate a new password and set it in Authentik\n'
                + '- End the team account\'s Authentik sessions\n\n'
                + 'Discord links are kept. Anyone using the old password must use the new one.';
        },

        async resetPassword() {
            this.newPassword = '';
            // No reload: the new password stays on screen until you leave the page
            const result = await this.doAction('reset_password', {}, { reload: false, url: this.$root.dataset.url });
            if (result && result.password) this.newPassword = result.password;
        },
    }));
});
