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
ERR_400_UNKNOWN_SUBJECT_TYPE = "error.400.chat_unknown_subject_type"
ERR_400_INCOMPLETE_SUBJECT = "error.400.chat_incomplete_subject"
ERR_400_INVALID_REPLY = "error.400.chat_invalid_reply"
ERR_400_NOT_SUPPORT = "error.400.chat_not_support"
ERR_400_INVALID_ATTACHMENT = "error.400.chat_invalid_attachment"
ERR_400_UNKNOWN_ATTACHMENT_TYPE = "error.400.chat_unknown_attachment_type"
ERR_400_UNKNOWN_ACTIVITY_STATE = "error.400.chat_unknown_activity_state"
ERR_400_MESSAGE_DELETED = "error.400.chat_message_deleted"
ERR_400_NOT_EDITABLE = "error.400.chat_not_editable"
ERR_403_NOT_PARTICIPANT = "error.403.chat_not_participant"
ERR_403_NOT_OPERATOR = "error.403.chat_not_operator"
ERR_403_NOT_AUTHOR = "error.403.chat_not_author"
ERR_404_CONVERSATION_NOT_FOUND = "error.404.chat_conversation_not_found"
ERR_404_MESSAGE_NOT_FOUND = "error.404.chat_message_not_found"
ERR_409_ALREADY_ASSIGNED = "error.409.chat_already_assigned"
# A send this sender may not make. The key deliberately does NOT name a block:
# an error key travels to the client, and the one thing a block must never do
# is announce itself to the person it is against. It reads the same as any
# other closed door, and it must stay that way.
ERR_403_SEND_REFUSED = "error.403.chat_send_refused"
# The block store is configured and could not be asked. 503, never 403 and
# never a delivered message: an outage is not consent, and answering 403 would
# tell a sender they are blocked when in fact a service is down.
ERR_503_BLOCKS_UNAVAILABLE = "error.503.chat_blocks_unavailable"

STAPEL_CHAT_ERRORS = {
    ERR_400_INVALID_KIND: "Unknown conversation kind",
    ERR_400_KIND_DISABLED: "This conversation kind is not enabled in this deployment",
    ERR_400_EMPTY_MESSAGE: "A message must carry a body or at least one attachment",
    ERR_400_BODY_TOO_LONG: "Message body exceeds the maximum allowed length",
    ERR_400_ATTACHMENTS_DISABLED: "Attachments are not enabled in this deployment",
    ERR_400_INVALID_DIRECT: "A direct conversation needs exactly one other participant",
    ERR_400_UNKNOWN_SUBJECT_TYPE: "This subject type is not registered in this deployment",
    ERR_400_INCOMPLETE_SUBJECT: "A subject needs both a type and a key",
    ERR_400_INVALID_REPLY: "The replied-to message does not belong to this conversation",
    ERR_400_NOT_SUPPORT: "This operation applies only to support conversations",
    ERR_400_INVALID_ATTACHMENT: "An attachment is malformed or its preview is too large",
    ERR_400_UNKNOWN_ATTACHMENT_TYPE: "This attachment type is not registered in this deployment",
    ERR_400_UNKNOWN_ACTIVITY_STATE: "This activity state is not registered in this deployment",
    ERR_400_MESSAGE_DELETED: "This message has been deleted",
    ERR_400_NOT_EDITABLE: "This message can no longer be edited",
    ERR_403_NOT_PARTICIPANT: "You are not a participant of this conversation",
    ERR_403_NOT_OPERATOR: "Only a support operator may perform this action",
    ERR_403_NOT_AUTHOR: "Only the author may edit or delete this message",
    ERR_404_CONVERSATION_NOT_FOUND: "Conversation not found",
    ERR_404_MESSAGE_NOT_FOUND: "Message not found in this conversation",
    ERR_409_ALREADY_ASSIGNED: "This support conversation is already assigned",
    ERR_403_SEND_REFUSED: "This message could not be sent",
    ERR_503_BLOCKS_UNAVAILABLE: "Messaging is temporarily unavailable, please try again",
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
    "ERR_400_UNKNOWN_SUBJECT_TYPE",
    "ERR_400_INCOMPLETE_SUBJECT",
    "ERR_400_INVALID_REPLY",
    "ERR_400_NOT_SUPPORT",
    "ERR_400_INVALID_ATTACHMENT",
    "ERR_400_UNKNOWN_ATTACHMENT_TYPE",
    "ERR_400_UNKNOWN_ACTIVITY_STATE",
    "ERR_400_MESSAGE_DELETED",
    "ERR_400_NOT_EDITABLE",
    "ERR_403_NOT_PARTICIPANT",
    "ERR_403_NOT_OPERATOR",
    "ERR_403_NOT_AUTHOR",
    "ERR_404_CONVERSATION_NOT_FOUND",
    "ERR_404_MESSAGE_NOT_FOUND",
    "ERR_409_ALREADY_ASSIGNED",
    "ERR_403_SEND_REFUSED",
    "ERR_503_BLOCKS_UNAVAILABLE",
]
