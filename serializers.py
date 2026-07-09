"""Serializers for the stapel-chat API (dataclass-DTO backed).

Every view exposes request/response serializer seams (SerializerSeamMixin);
these are the defaults.
"""
from stapel_core.django.api.serializers import StapelDataclassSerializer

from .dto import (
    ConversationResponse,
    CreateConversationRequest,
    MarkReadRequest,
    MessageResponse,
    ParticipantResponse,
    SendMessageRequest,
)


class ParticipantResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ParticipantResponse


class MessageResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MessageResponse


class ConversationResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = ConversationResponse


class CreateConversationRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = CreateConversationRequest


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
        return fields


class MarkReadRequestSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = MarkReadRequest
