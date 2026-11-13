import math
import uuid

from django import forms
from django.contrib.auth.models import User

from .assessment_forms import INPUT, StyledForm
from .communications import validate_recipients
from .models import AssessmentEvent, AssessmentPolicy, CuratedEvent, DiagnosticRule, ServiceDependency, SubjectArea


class CommunicationForm(StyledForm):
    subject = forms.CharField(max_length=512)
    body = forms.CharField(max_length=50000, widget=forms.Textarea(attrs={"rows": 8}), label="Message")
    recipients = forms.CharField(max_length=4000, help_text="Comma-separated addresses. Delivery stays in the local outbox.")
    review_seconds = forms.IntegerField(min_value=1, max_value=86400, initial=300, label="Human review window (seconds)")
    sla_seconds = forms.IntegerField(min_value=1, max_value=604800, initial=900, label="Communication SLA (seconds)")
    purpose = forms.ChoiceField(choices=[("update", "Status update"), ("initial", "Initial response")])
    auto_send = forms.BooleanField(required=False, label="Authorize automatic local delivery when review expires")

    def clean_recipients(self):
        try:
            return validate_recipients(self.cleaned_data["recipients"])
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc


class RevisionForm(CommunicationForm):
    review_seconds = None
    sla_seconds = None
    purpose = None
    expected_version = forms.IntegerField(min_value=1, widget=forms.HiddenInput)


class OperationalActionForm(StyledForm):
    kind = forms.ChoiceField(choices=[(s, s.replace("_", " ").title()) for s in ("hypothesis", "decision", "operational_action", "mitigation", "recovery", "bridge_opened")])
    body = forms.CharField(max_length=10000, widget=forms.Textarea(attrs={"rows": 3}), label="Action or observation")
    selected = forms.ModelMultipleChoiceField(queryset=AssessmentEvent.objects.none(), required=False, label="Supporting evidence")


class AttachmentForm(StyledForm):
    file = forms.FileField(label="Evidence file", help_text="PNG, JPEG, WebP, PDF, or plain text; up to 5 MB.")
    description = forms.CharField(max_length=2000, required=False, label="Evidence description")


class QueryForm(StyledForm):
    rule = forms.ModelChoiceField(queryset=DiagnosticRule.objects.all(), label="Saved diagnostic check")


class SmeForm(StyledForm):
    target = forms.ModelChoiceField(queryset=User.objects.filter(is_active=True), required=False, label="Named specialist")
    area = forms.ModelChoiceField(queryset=SubjectArea.objects.all(), required=False, label="Or subject area")
    reason = forms.CharField(max_length=5000, widget=forms.Textarea(attrs={"rows": 3}), label="Why is specialist help needed?")
    selected = forms.ModelMultipleChoiceField(queryset=AssessmentEvent.objects.none(), required=False, label="Handoff evidence")
    request_id = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)
    ack_seconds = forms.IntegerField(min_value=1, max_value=86400, initial=300, label="Acknowledgement window (seconds)")


class CurateForm(StyledForm):
    event = forms.ModelChoiceField(queryset=AssessmentEvent.objects.none(), label="Original event")
    section = forms.ChoiceField(choices=CuratedEvent._meta.get_field("section").choices)
    note = forms.CharField(max_length=5000, required=False, widget=forms.Textarea(attrs={"rows": 2}), label="Reviewer note")
    remove = forms.BooleanField(required=False, label="Remove this selection from the RCA")


class RcaForm(StyledForm):
    summary = forms.CharField(max_length=20000, required=False, widget=forms.Textarea(attrs={"rows": 3}))
    impact = forms.CharField(max_length=20000, required=False, widget=forms.Textarea(attrs={"rows": 3}))
    root_cause = forms.CharField(max_length=20000, required=False, widget=forms.Textarea(attrs={"rows": 4}))
    contributing_factors = forms.CharField(max_length=20000, required=False, widget=forms.Textarea(attrs={"rows": 3}))
    detection_gaps = forms.CharField(max_length=20000, required=False, widget=forms.Textarea(attrs={"rows": 3}))
    state = forms.ChoiceField(choices=[("draft", "Draft"), ("review", "In review"), ("final", "Final")])


class RemediationForm(StyledForm):
    title = forms.CharField(max_length=512, label="Corrective action")
    owner = forms.ModelChoiceField(queryset=User.objects.filter(is_active=True))
    due_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))


class StyledModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            css = "h-4 w-4" if isinstance(field.widget, forms.CheckboxInput) else INPUT
            field.widget.attrs.update({"class": css, "id": f"{self.prefix or 'config'}-{name}"})


class DiagnosticRuleForm(StyledModelForm):
    class Meta:
        model = DiagnosticRule
        fields = ["name", "provider", "query", "baseline_query", "sample_query", "minimum_samples", "comparator", "threshold", "lookback_minutes", "fixture_result"]
        widgets = {key: forms.Textarea(attrs={"rows": 3}) for key in ["query", "baseline_query", "sample_query", "fixture_result"]}

    def clean(self):
        data = super().clean()
        data["fixture_result"] = data.get("fixture_result") or {}
        if data.get("minimum_samples", 0) < 1:
            self.add_error("minimum_samples", "At least one sample is required.")
        if data.get("threshold") is not None and not math.isfinite(data["threshold"]):
            self.add_error("threshold", "Use a finite threshold.")
        if data.get("provider") != "fixture" and not data.get("sample_query"):
            self.add_error("sample_query", "A sample-count query is required for real diagnostics.")
        if data.get("comparator") == "deviation" and data.get("provider") != "fixture" and not data.get("baseline_query"):
            self.add_error("baseline_query", "Deviation checks require a baseline query.")
        if not 1 <= data.get("lookback_minutes", 0) <= 10080:
            self.add_error("lookback_minutes", "Choose between 1 and 10,080 minutes.")
        return data


class PolicyForm(StyledModelForm):
    recipients = forms.CharField(required=False, help_text="Comma-separated; leave empty to skip communication drafting.")

    class Meta:
        model = AssessmentPolicy
        fields = ["name", "customer", "service", "supplier", "active", "rules", "recipients", "auto_promote", "auto_close_no_issue", "auto_send", "communication_sla_seconds", "review_seconds", "severity", "impact", "route_to_t2"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial["recipients"] = ", ".join(self.instance.recipients)

    def clean_recipients(self):
        text = self.cleaned_data["recipients"]
        try:
            return validate_recipients(text) if text.strip() else []
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc

    def clean(self):
        data = super().clean()
        if data.get("active") and not data.get("rules"):
            self.add_error("rules", "An active policy needs diagnostic checks.")
        if data.get("severity") not in {"P1", "P2", "P3", "P4"}:
            self.add_error("severity", "Use P1, P2, P3 or P4.")
        if data.get("impact") not in {"low", "moderate", "high"}:
            self.add_error("impact", "Choose low, moderate or high for confirmed degradation.")
        if not 1 <= data.get("review_seconds", 0) <= 86400:
            self.add_error("review_seconds", "Choose a review window between 1 and 86,400 seconds.")
        if not 1 <= data.get("communication_sla_seconds", 0) <= 604800:
            self.add_error("communication_sla_seconds", "Choose an SLA between 1 and 604,800 seconds.")
        return data


class DependencyForm(StyledModelForm):
    class Meta:
        model = ServiceDependency
        fields = ["customer", "service", "supplier", "active"]


class SubjectAreaForm(StyledModelForm):
    class Meta:
        model = SubjectArea
        fields = ["name", "description", "members"]


class ContextForm(StyledForm):
    severity = forms.ChoiceField(choices=[(f"P{n}", f"P{n}") for n in range(1, 5)])
    impact = forms.ChoiceField(choices=[(s, s.capitalize()) for s in ("unknown", "none", "low", "moderate", "high")])
    reason = forms.CharField(max_length=2000, widget=forms.Textarea(attrs={"rows": 2}))


class MultiplePhotoInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultiplePhotoField(forms.FileField):
    widget = MultiplePhotoInput

    def clean(self, data, initial=None):
        if not data:
            return []
        files = data if isinstance(data, (list, tuple)) else [data]
        if len(files) > 8:
            raise forms.ValidationError("Attach up to 8 photos per note.")
        cleaned = [super(MultiplePhotoField, self).clean(file, initial) for file in files]
        if any(file.size > 5 * 1024 * 1024 for file in cleaned):
            raise forms.ValidationError("Each photo must be at most 5 MB.")
        return cleaned


class PhotoNoteForm(StyledForm):
    body = forms.CharField(max_length=10000, required=False, label="Add to the history",
                           widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Add a note, paste a dashboard link or screenshot…"}))
    photos = MultiplePhotoField(required=False, label="Photos / screenshots",
                               widget=MultiplePhotoInput(attrs={"accept": "image/png,image/jpeg,image/webp"}),
                               help_text="Choose or paste photos. Up to 8 PNG, JPEG or WebP images; 5 MB each.")

    def clean(self):
        data = super().clean()
        if not self.errors and not (data.get("body", "").strip() or data.get("photos")):
            raise forms.ValidationError("Add a note or at least one photo.")
        return data
