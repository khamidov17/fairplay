"""Organization/class membership helpers shared by admin and views."""


def sync_class_members_to_organization(class_obj):
    """Ensure every class member is also a course (organization) member."""
    org = class_obj.organization
    member_ids = list(class_obj.members.values_list('id', flat=True))
    if member_ids:
        org.members.add(*member_ids)
