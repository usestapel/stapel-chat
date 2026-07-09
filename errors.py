"""i18n error keys of stapel-chat.

Only ``error.<status>.<slug>`` keys leave this package — human-readable
strings are translations, never literals in responses.
"""
from stapel_core.django.api.errors import register_service_errors

ERR_400_INVALID_KIND = "error.400.chat_invalid_kind"
ERR_400_KIND_DISABLED = "error.400.chat_kind_disabled"
ERR_400_EMPTY_MESSAGE = "error.400.chat_empty_message"
ERR_400_BODY_TOO_LONG = "error.400.chat_body_too_long"
ERR_400_ATTACHMENTS_DISABLED = "error.400.chat_attachments_disabled"
ERR_400_INVALID_DIRECT = "error.400.chat_invalid_direct"
ERR_400_INVALID_REPLY = "error.400.chat_invalid_reply"
ERR_400_NOT_SUPPORT = "error.400.chat_not_support"
ERR_403_NOT_PARTICIPANT = "error.403.chat_not_participant"
ERR_403_NOT_OPERATOR = "error.403.chat_not_operator"
ERR_404_CONVERSATION_NOT_FOUND = "error.404.chat_conversation_not_found"
ERR_409_ALREADY_ASSIGNED = "error.409.chat_already_assigned"

STAPEL_CHAT_ERRORS = {
    ERR_400_INVALID_KIND: "Unknown conversation kind",
    ERR_400_KIND_DISABLED: "This conversation kind is not enabled in this deployment",
    ERR_400_EMPTY_MESSAGE: "A message must carry a body or at least one attachment",
    ERR_400_BODY_TOO_LONG: "Message body exceeds the maximum allowed length",
    ERR_400_ATTACHMENTS_DISABLED: "Attachments are not enabled in this deployment",
    ERR_400_INVALID_DIRECT: "A direct conversation needs exactly one other participant",
    ERR_400_INVALID_REPLY: "The replied-to message does not belong to this conversation",
    ERR_400_NOT_SUPPORT: "This operation applies only to support conversations",
    ERR_403_NOT_PARTICIPANT: "You are not a participant of this conversation",
    ERR_403_NOT_OPERATOR: "Only a support operator may perform this action",
    ERR_404_CONVERSATION_NOT_FOUND: "Conversation not found",
    ERR_409_ALREADY_ASSIGNED: "This support conversation is already assigned",
}

register_service_errors(STAPEL_CHAT_ERRORS)

__all__ = [
    "STAPEL_CHAT_ERRORS",
    "ERR_400_INVALID_KIND",
    "ERR_400_KIND_DISABLED",
    "ERR_400_EMPTY_MESSAGE",
    "ERR_400_BODY_TOO_LONG",
    "ERR_400_ATTACHMENTS_DISABLED",
    "ERR_400_INVALID_DIRECT",
    "ERR_400_INVALID_REPLY",
    "ERR_400_NOT_SUPPORT",
    "ERR_403_NOT_PARTICIPANT",
    "ERR_403_NOT_OPERATOR",
    "ERR_404_CONVERSATION_NOT_FOUND",
    "ERR_409_ALREADY_ASSIGNED",
]
