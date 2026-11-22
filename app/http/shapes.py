"""Request bodies. Routes use them as runtime.<Name>."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, EmailStr, Field

class BookRequest(BaseModel):
    event_type: str
    start: str
    duration_minutes: int | None = None
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr | None = None
    phone: str = Field(default='', max_length=40)
    notes: str = Field(default='', max_length=500)
    sms_consent: bool | None = None
    notify_emails: str = Field(default='', max_length=400)
    source: str = Field(default='web', max_length=20)
    verify_token: str = Field(default='', max_length=800)
    send_email: bool = True
    cipher_envelope_id: str = Field(default='', max_length=80)

class VerifyRequest(BaseModel):
    email: str = ''
    phone: str = ''
    full_name: str = ''
    first_name: str = ''
    last_name: str = ''
    dob: str = ''
    portal: bool = False
    sms_consent: str = ''

class VerifyCodeRequest(BaseModel):
    email: str = ''
    phone: str = ''
    code: str = ''

class AccountsRequest(BaseModel):
    contact_token: str

class PickAccountRequest(BaseModel):
    contact_token: str
    ref: int
    old_token: str = ''

class ChatThreadPost(BaseModel):
    body: str
    token: str = ''
    label: str = ''
    email: str = ''
    phone: str = ''
    hear_back: str = ''
    sms_consent: bool = False
    name: str = ''
    verify_token: str = ''

class SmsBridgeEnqueue(BaseModel):
    template: str
    phone: str
    source: str = 'voice'
    idempotency_key: str = ''

class SmsBridgeAck(BaseModel):
    id: str
    status: str

class PublicTokenRequest(BaseModel):
    verify_token: str

class PublicCancelVisitRequest(BaseModel):
    verify_token: str
    event_id: str

class CardLinkRequest(BaseModel):
    verify_token: str = ''

class MailSendRequest(BaseModel):
    kind: str
    to: EmailStr
    name: str = ''
    title: str = ''
    url: str = ''
    amount: str = ''
    form_name: str = ''
    notes: str = ''
    when_label: str = ''
    duration_minutes: int | None = None
    start_iso: str = ''
    rebook_url: str = ''
    doc_title: str = ''
    due_date: str = ''

class LateSendRequest(BaseModel):
    event_id: str
    calendar_id: str = ''
    kind: str = 'late_soon'

class DeskAuthEmail(BaseModel):
    email: str = Field(default='', max_length=200)

class DeskAuthCode(BaseModel):
    email: str = Field(default='', max_length=200)
    code: str = Field(default='', max_length=20)

class DeskAuthLegacy(BaseModel):
    secret: str = Field(default='', max_length=400)

class DeskBookRequest(BaseModel):
    event_type: str = 'follow-up'
    start: str
    duration_minutes: int | None = None
    client_id: str = ''
    name: str = ''
    email: str = ''
    phone: str = ''
    notes: str = ''
    send_email: bool = True

class DeskSendRequest(BaseModel):
    item: str = ''
    items: list[str] = []
    client_id: str = ''
    name: str = ''
    email: str = ''
    phone: str = ''
    notes: str = ''
    duration_minutes: int | None = None
    send_email: bool = True

class InternalIntakeRequest(BaseModel):
    form: str
    fields: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(default_factory=dict)

class HoldTimesRequest(BaseModel):
    deadline_minutes: float
    remind_minutes: float | None = None

class InternalCardRequest(BaseModel):
    client_id: str = ''
    email: str = ''
    customer_id: str = ''
    payment_method_id: str = ''
    brand: str = ''
    last4: str = ''
    exp_month: int | None = None
    exp_year: int | None = None

class DeskFeeRequest(BaseModel):
    client_id: str = ''
    fee_id: str = ''
    dos: str = ''
    amount: str = ''
    amount_cents: int | None = None
    kind: str = 'visit'
    note: str = ''
    event_id: str = ''
    charge: bool = False
    duration_minutes: int | None = None
    notify: bool = True

class DeskNoteSignRequest(BaseModel):
    client_id: str = ''
    email: str = ''
    event_id: str = ''
    dos: str = ''
    name: str = ''
    dob: str = ''
    physician: str = ''
    return_visit: str = ''
    medications: str = ''
    supplements: str = ''
    recommendations: str = ''
    long_run: str = ''
    template: str = 'soap'
    data: str = ''
    subjective: str = ''
    objective: str = ''
    assessment: str = ''
    plan: str = ''
    addendum: str = ''
    duration: str = ''
    values: dict[str, str] = {}

class DeskNoteUpdateRequest(BaseModel):
    client_id: str = ''
    event_id: str = ''
    transcript_id: str = ''
    fields: dict[str, str] = {}
    transcript: str = ''

class AvsShareRequest(BaseModel):
    note_id: str
    send_email: bool = True
    send_sms: bool = True
    to_email: str = ''
    to_phone: str = ''

class AvsRevokeRequest(BaseModel):
    note_id: str

class DeskNoteTemplateSave(BaseModel):
    id: str
    title: str = ''
    body: str = ''
    sections: list[dict] = []

class DeskNoteTemplateOrder(BaseModel):
    ids: list[str] = []

class DeskPhraseSave(BaseModel):
    trigger: str = ''
    id: str = ''
    title: str = ''
    body: str = ''

class DeskPhraseOrder(BaseModel):
    ids: list[str] = []

class DeskNoteDraftRequest(BaseModel):
    transcript: str = ''
    name: str = ''
    dos: str = ''
    duration: str = ''

class DeskScribeIngestRequest(BaseModel):
    transcript: str = ''
    note: str = ''
    title: str = ''
    template: str = 'avs'
    duration_minutes: int = 0
    client_id: str = ''
    email: str = ''
    name: str = ''
    room: str = ''
    ended_at: str = ''
    started_at: str = ''

class DeskVisitFlag(BaseModel):
    event_id: str = ''
    flag: str = ''

class DeskVisitCancel(BaseModel):
    event_id: str = ''
    calendar_id: str = ''
    notify: bool = False

class DeskVisitMove(BaseModel):
    event_id: str = ''
    calendar_id: str = ''
    start: str = ''
    notify: bool = False

class CipherDrainRequest(BaseModel):
    id: str = ''

class DeskAgentRequest(BaseModel):
    messages: list[dict[str, str]] = []

class DeskPracticeSave(BaseModel):
    display_name: str = ''
    legal_name: str = ''
    clinician_name: str = ''
    clinician_title: str = ''
    address: str = ''
    phone: str = ''
    fax: str = ''
    email: str = ''
    website: str = ''
    meet: str = ''
    ein: str = ''
    npi: str = ''
    licenses: list[dict] = []
    billing_role: str = ''
    theme: dict = {}
    page_titles: dict = {}
    clinician_callback: str = ''
    call_handset: str = ''

class DeskServiceSave(BaseModel):
    slug: str = ''
    title: str = ''
    enabled: bool = True
    public: bool = True
    duration_minutes: Any = None
    calendar_duration_minutes: int | None = None
    price_usd: float | None = None
    price_by_duration: dict | None = None
    weekly_hours: dict | None = None
    slot_interval_minutes: int = 30
    minimum_notice_minutes: int = 120
    booking_window_days: int = 30
    creating: bool = False

class DeskCallRequest(BaseModel):
    to: str = ''
    client_id: str = ''
    name: str = ''
    sid: str = ''
    handset: str = ''

class DeskSuperbillRequest(BaseModel):
    client_id: str = ''
    email: str = ''
    dos: str = ''
    pos: str = '10'
    address: str = ''
    diagnoses: list[dict] = []
    lines: list[dict] = []
    items: list[str] = []
    custom_fields: list[dict] = []
    preview: bool = False

class SuperbillSendRequest(BaseModel):
    superbill_id: str
    send_email: bool = True
    send_sms: bool = True

class BillConfirmRequest(BaseModel):
    client_id: str = ''
    event_id: str = ''
    dos: str = ''
    visit_code: str = ''
    confirm_code: str = ''
    diagnoses: list[Any] = []
    save_dx: bool = False
    payment: str = 'link'
    confirm_charge: bool = False
    send_email: bool = True
    send_sms: bool = False
    share_superbill: bool = True
    dry_run: bool = True
    specimen: bool = False
    share_ttl_seconds: int | None = None

class BillSuperbillRequest(BaseModel):
    billing_id: str = ''
    diagnoses: list[Any] = []
    save_dx: bool = False
    share_superbill: bool = True
    send_email: bool = True
    send_sms: bool = False
    specimen: bool = False
    share_ttl_seconds: int | None = None

class BillShareRequest(BaseModel):
    billing_id: str = ''
    pay_link: bool = False
    send_email: bool = True
    send_sms: bool = False
    share_ttl_seconds: int | None = None

class MailReplyFile(BaseModel):
    name: str = 'file'
    b64: str = ''

class MailReplyRequest(BaseModel):
    id: str
    body: str = ''
    files: list[MailReplyFile] = []

class DriveSaveRequest(BaseModel):
    path: str
    text: str = ''
    b64: str = ''

class DriveShareRequest(BaseModel):
    path: str
    client_id: str = ''
    name: str = ''
    email: str = ''
    phone: str = ''
    send_email: bool = True
    send_sms: bool = False

class BookPrefillRequest(BaseModel):
    token: str

class DeskBookLinkRequest(BaseModel):
    client_id: str
    duration: int | None = None
    send_email: bool = False
    dry_run: bool = False

class DeskBookLinkRevoke(BaseModel):
    client_id: str
