from django import forms

from .models import AssessmentConfidence, AssessmentVerdict

INPUT = "w-full bg-slate-950 border border-slate-600 rounded-lg px-3 py-2 text-base text-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"


class StyledForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            css = "h-4 w-4 rounded border-slate-500 text-indigo-500" if isinstance(field.widget, forms.CheckboxInput) else INPUT
            field.widget.attrs.update({"class": css, "id": f"{self.prefix or 'form'}-{name}"})


class SignalForm(StyledForm):
    title = forms.CharField(max_length=512, label="What needs assessment?")
    source = forms.ChoiceField(choices=[("operator", "Internal operator"), ("customer", "Customer concern"),
                                       ("supplier", "Supplier notice"), ("monitor", "Monitor signal")])
    customer = forms.CharField(max_length=128, required=False, label="Customer / partner")
    service = forms.CharField(max_length=128, required=False, label="Service / line of business")
    supplier = forms.CharField(max_length=128, required=False)
    signal = forms.CharField(max_length=10000, widget=forms.Textarea(attrs={"rows": 4}), label="Original signal")
    source_event_id = forms.UUIDField(widget=forms.HiddenInput)


class EvidenceForm(StyledForm):
    summary = forms.CharField(max_length=512, label="Evidence summary")
    source = forms.CharField(max_length=128, initial="operator", label="Evidence source")
    query = forms.CharField(max_length=10000, required=False, widget=forms.Textarea(attrs={"rows": 3}), label="Query or check performed")
    result = forms.CharField(max_length=20000, widget=forms.Textarea(attrs={"rows": 4}), label="Recorded result and observation window")


class ConclusionForm(StyledForm):
    verdict = forms.ChoiceField(choices=AssessmentVerdict.choices)
    confidence = forms.ChoiceField(choices=AssessmentConfidence.choices)
    reason = forms.CharField(max_length=10000, widget=forms.Textarea(attrs={"rows": 3}), label="Reasoning and evidence limits")


class ReasonForm(StyledForm):
    reason = forms.CharField(max_length=2000, widget=forms.Textarea(attrs={"rows": 2}), label="Disposition reason")


class PromotionForm(StyledForm):
    reason = forms.CharField(max_length=2000, widget=forms.Textarea(attrs={"rows": 2}), label="Why does this warrant an incident?")
    policy_reason = forms.CharField(max_length=2000, required=False, widget=forms.Textarea(attrs={"rows": 2}),
                                   label="Immediate-incident policy exception (optional)",
                                   help_text="Use only when policy requires an incident before degradation is established. The reason is audited.")


class CommentForm(StyledForm):
    body = forms.CharField(max_length=10000, widget=forms.Textarea(attrs={"rows": 2}), label="Add to the history")
