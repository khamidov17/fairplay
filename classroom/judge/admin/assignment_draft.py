from django.contrib import admin
from django.utils.translation import gettext_lazy as _


class AssignmentDraftAdmin(admin.ModelAdmin):
    """A read-only window for the last save a student made while their window was open.

    Drafts are written only by the student's workspace. Staff must not edit them here:
    that would put words in a student's mouth. Rows cannot be added or changed.
    """
    list_display = ('username', 'contest_name', 'problem', 'language', 'updated_on', 'harvested_at')
    list_filter = ('harvested_at', 'problem__submission_type')
    list_select_related = ('participation__user__user', 'participation__contest', 'problem', 'language')
    search_fields = (
        'participation__user__user__username', 'problem__code', 'participation__contest__key',
    )
    readonly_fields = ('participation', 'problem', 'language', 'content', 'updated_on', 'harvested_at')
    date_hierarchy = 'updated_on'

    @admin.display(description=_('user'), ordering='participation__user__user__username')
    def username(self, obj):
        return obj.participation.user.username

    @admin.display(description=_('assignment'), ordering='participation__contest__name')
    def contest_name(self, obj):
        return obj.participation.contest.name

    def has_add_permission(self, request):
        return False  # drafts are written by the student's workspace only

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
