"""Serializers for the stapel-chat API (dataclass-DTO backed).

Every view exposes request/response serializer seams (SerializerSeamMixin);
these are the defaults.
"""
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from stapel_core.django.api.serializers import StapelDataclassSerializer

from .dto import (
    ActivityRequest,
    AttachmentResponse,
    ConversationResponse,
    CreateConversationRequest,
    EditMessageRequest,
    MarkReadRequest,
    MessageResponse,
    ParticipantResponse,
    SendMessageRequest,
    SubjectResponse,
)


@extend_schema_field(
    {
        "oneOf": [
            {
                "type": "object",
                "required": ["key", "type"],
                "properties": {
                    "key": {"type": "string"},
                    "type": {
                        "type": "string",
                        "description": (
                            "Attachment type from the OPEN registry — image / "
                            "gif / video / voice / file out of the box, plus "
                            "whatever STAPEL_CHAT['ATTACHMENT_TYPES'] adds."
                        ),
                    },
                },
                "additionalProperties": True,
            },
            {"type": "string", "description": "Bare CDN ref (pre-0.3 form)."},
        ]
    }
)
class _AttachmentInputField(serializers.Field):
    """One inbound attachment: a descriptor object, or a bare CDN ref string.

    Deliberately untyped at this boundary — see the note in
    ``SendMessageRequestSerializer.get_fields``. Everything that decides what
    an attachment *means* lives in :mod:`stapel_chat.attachments`, behind the
    open registry.
    """

    def to_internal_value(self, data):
        if isinstance(data, (dict, str)):
            return data
        raise serializers.ValidationError(
            "an attachment must be an object or a CDN ref string"
        )

    def to_representation(self, value):
        return value


class ParticipantResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ParticipantResponse


class SubjectResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = SubjectResponse

    def get_fields(self):
        # The card belongs to whoever rendered it. A typed serializer here
        # would be this module's second, staler answer to what a listing looks
        # like — and it would drop every field the provider added since.
        fields = super().get_fields()
        fields["card"] = serializers.JSONField(required=False, allow_null=True)
        return fields


class AttachmentResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = AttachmentResponse


class MessageResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MessageResponse


class ConversationResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ConversationResponse


class CreateConversationRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = CreateConversationRequest

    def get_fields(self):
        # Both subject fields are optional and may be blank: a thread about
        # nothing in particular is the normal case, and "both or neither" is a
        # domain rule the view answers with the localized error envelope.
        fields = super().get_fields()
        for name in ("subject_type", "subject_key"):
            fields[name].required = False
            fields[name].allow_blank = True
        return fields


class SendMessageRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = SendMessageRequest

    def get_fields(self):
        # A message may be attachment-only, so the body is optional and may be
        # blank at the serializer boundary (the "empty message" rule — body OR
        # an attachment — is enforced in the view, which returns the localized
        # error envelope). The dataclass default maps to a CharField that
        # rejects "" by default; relax it here.
        fields = super().get_fields()
        fields["body"].required = False
        fields["body"].allow_blank = True
        fields["client_msg_id"].required = False
        fields["client_msg_id"].allow_blank = True
        # An attachment is an OPEN shape: the registry decides which types
        # exist and the CDN may add fields this release has never heard of, so
        # the boundary accepts opaque JSON and the meaning is applied by
        # `stapel_chat.attachments`, which owns the registry. A closed
        # per-field serializer here would be a second, staler answer to what an
        # attachment is — and it would reject a sticker the host registered.
        # Bare ref strings (the pre-0.3 form) pass through for the same reason.
        fields["attachments"] = serializers.ListField(
            child=_AttachmentInputField(), required=False, allow_empty=True
        )
        return fields


class EditMessageRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = EditMessageRequest

    def get_fields(self):
        # "an edit may not empty a message" is a domain rule, not a field
        # constraint — it belongs in the view, which answers with the
        # localized error envelope instead of DRF's raw field-error shape.
        fields = super().get_fields()
        fields["body"].allow_blank = True
        return fields


class MarkReadRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MarkReadRequest


class ActivityRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ActivityRequest
