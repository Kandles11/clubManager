from django import forms
from django.contrib.auth.forms import SetPasswordForm
from common.forms import NetIDField


class RegistrationRequestForm(forms.Form):
    net_id = NetIDField(label="Net ID", max_length=20, widget=forms.TextInput(attrs={"autofocus": True}))


class RegistrationCompletionForm(SetPasswordForm):
    first_name = forms.CharField(label="First name", max_length=150)
    last_name = forms.CharField(label="Last name", max_length=150)

    field_order = ["first_name", "last_name", "new_password1", "new_password2"]

    def save(self, commit=True):
        user = super().save(commit=False)
        user.first_name = self.cleaned_data["first_name"]
        user.last_name = self.cleaned_data["last_name"]
        if commit:
            user.save()
        return user
