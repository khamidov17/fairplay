"""Admin side menu (dmoj-wpadmin) that only offers "+" (add) where the user may add.

wpadmin's own menu shows an add link for every model, so staff without the add permission
(and everyone for submissions, profiles and point votes, which are never added by hand) got a
"+" that leads to 403.
"""
from wpadmin.menu.custom import CustomModelLeftMenuWithDashboard, SubModelMenu
from wpadmin.utils import get_admin_site, get_admin_site_name, get_wpadmin_settings


def addable_urls(context):
    site = get_admin_site(context)
    return {model['add_url'] for app in site.get_app_list(context.get('request'))
            for model in app['models'] if model.get('add_url')}


class PermissionAwareSubModelMenu(SubModelMenu):
    def init_with_context(self, context):
        super().init_with_context(context)
        allowed = addable_urls(context)
        for child in self.children:
            if child.add_url and child.add_url not in allowed:
                child.add_url = None
            # Nested groups are initialised later, when the menu is drawn.
            if type(child) is SubModelMenu:
                child.__class__ = PermissionAwareSubModelMenu


class PermissionAwareLeftMenu(CustomModelLeftMenuWithDashboard):
    def get_model_children(self, context):
        if not self.is_user_allowed(context.get('request').user):
            return []
        tree = get_wpadmin_settings(get_admin_site_name(context)).get('custom_menu', [])
        menu = PermissionAwareSubModelMenu('', tree)
        menu.init_with_context(context)
        return menu.children
