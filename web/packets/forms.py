from django import forms


class PacketUploadForm(forms.Form):
    title = forms.CharField(max_length=255)
    notes = forms.CharField(required=False)
    send_via_email = forms.BooleanField(required=False)
    web_access_enabled = forms.BooleanField(required=False)
    team_extras = forms.CharField(required=False)
    packet_file = forms.FileField()

    def clean_packet_file(self) -> object:
        f = self.cleaned_data["packet_file"]
        max_size = 25 * 1024 * 1024
        if f.size and f.size > max_size:
            raise forms.ValidationError("File size must not exceed 25 MB.")
        return f


class PacketActionForm(forms.Form):
    action = forms.CharField()
    email = forms.EmailField(required=False)
    team_id = forms.IntegerField(required=False)

    def clean(self) -> dict[str, object]:
        cleaned = super().clean() or {}
        if cleaned.get("action") == "test_email":
            if not cleaned.get("email"):
                self.add_error("email", "Email is required for test_email action.")
            if not cleaned.get("team_id"):
                self.add_error("team_id", "Team is required for test_email action.")
        return cleaned


class PacketResendForm(forms.Form):
    # 254: SchoolInfo's email columns, where a resend saves them
    primary_email = forms.EmailField(max_length=254)
    secondary_email = forms.EmailField(max_length=254, required=False)
