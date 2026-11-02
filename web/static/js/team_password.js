/**
 * Reset Password for one team (admin/includes/team_password_reset.html). The component's root carries
 * data-url, the team's admin_team_action URL.
 */
document.addEventListener('alpine:init', () => {
    Alpine.data('teamPasswordReset', () => compose(actionMixin(), {
        newPassword: '',

        async resetPassword() {
            this.newPassword = '';
            // No reload: the new password stays on screen until you leave the page
            const result = await this.doAction('reset_password', {}, { reload: false, url: this.$root.dataset.url });
            if (result && result.password) this.newPassword = result.password;
            if (result && result.members_unlinked) window.dispatchEvent(new CustomEvent('members-unlinked'));
        },
    }));
});
