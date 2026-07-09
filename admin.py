"""Admin registrations for stapel-chat (observability; kept minimal)."""
from django.contrib import admin

from .models import Conversation, ConversationParticipant, Message


class ParticipantInline(admin.TabularInline):
    model = ConversationParticipant
    extra = 0
    fields = ("user", "role", "last_read_seq")


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "kind", "scope_key", "support_status", "assigned_operator", "last_seq")
    list_filter = ("kind", "support_status")
    search_fields = ("id", "scope_key")
    inlines = [ParticipantInline]


@admin.register(Message)
class MessageAdmin(admin.ModelAdmin):
    list_display = ("conversation", "seq", "kind", "sender", "created_at")
    list_filter = ("kind",)
    search_fields = ("conversation__id", "body")
    date_hierarchy = "created_at"
