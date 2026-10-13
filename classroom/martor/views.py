import json

from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist
from django.http import HttpResponse
from django.utils.translation import gettext_lazy as _

from judge.utils.user_search import auth_user_search_q, format_user_label, profile_person_name
from .utils import LazyEncoder


def _mention_user_payload(user):
    override = ''
    try:
        override = user.profile.username_display_override or ''
    except ObjectDoesNotExist:
        pass
    name = profile_person_name(user.first_name, user.last_name, override, user.username)
    return {
        'username': user.username,
        'name': name,
        'text': format_user_label(name, user.username),
    }


@login_required
def markdown_search_user(request):
    """
    Json usernames of the users registered & actived.

    url(method=get):
        /martor/search-user/?username={username}

    Response:
        error:
            - `status` is status code (204)
            - `error` is error message.
        success:
            - `status` is status code (204)
            - `data` is list dict of usernames.
                { 'status': 200,
                  'data': [
                    {'usernane': 'john'},
                    {'usernane': 'albert'}]
                }
    """
    data = {}
    username = request.GET.get('username')
    if username is not None and username.strip() != '':
        users = User.objects.filter(is_active=True).filter(auth_user_search_q(username)).select_related('profile')
        if users:
            data.update({
                'status': 200,
                'data': [_mention_user_payload(u) for u in users],
            })
            return HttpResponse(
                json.dumps(data, cls=LazyEncoder),
                content_type='application/json')
        data.update({
            'status': 204,
            'error': _('No users registered as `%(username)s` '
                       'or user is unactived.') % {'username': username},
        })
    else:
        data.update({
            'status': 204,
            'error': _('Validation Failed for field `username`'),
        })
    return HttpResponse(
        json.dumps(data, cls=LazyEncoder),
        content_type='application/json')
