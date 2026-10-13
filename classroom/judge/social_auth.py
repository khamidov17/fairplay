import logging
import re
from operator import itemgetter
from urllib.parse import quote

from django import forms
from django.contrib.auth.models import User
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import reverse
from requests import HTTPError
from reversion import revisions
from social_core.backends.github import GithubOAuth2
from social_core.exceptions import InvalidEmail, SocialAuthBaseException
from social_core.pipeline.partial import partial
from social_django.middleware import SocialAuthExceptionMiddleware as OldSocialAuthExceptionMiddleware

from judge.forms import ProfileForm
from judge.models import Language, Profile
from judge.utils.sso_usernames import SSO_RESERVED_USERNAME_MESSAGE, is_sso_reserved_username

logger = logging.getLogger('judge.social_auth')


class GitHubSecureEmailOAuth2(GithubOAuth2):
    name = 'github-secure'

    def user_data(self, access_token, *args, **kwargs):
        data = self._user_data(access_token)
        try:
            emails = self._user_data(access_token, '/emails')
        except (HTTPError, ValueError, TypeError):
            emails = []

        emails = [(e.get('email'), e.get('primary'), 0) for e in emails if isinstance(e, dict) and e.get('verified')]
        emails.sort(key=itemgetter(1), reverse=True)
        emails = list(map(itemgetter(0), emails))

        if emails:
            data['email'] = emails[0]
        else:
            data['email'] = None

        return data


def slugify_username(username, renotword=re.compile(r'[^\w]')):
    return renotword.sub('', username.replace('-', '_'))


def associate_by_email(backend, details, user=None, *args, **kwargs):
    """``social_core``'s associate_by_email, but never into an SSO-only account (CA-1, CA-7).

    An account with no usable password and no social login was made for NetID sign-in (by an SSO
    login or a roster import). Its e-mail address may come from a roster upload, which nobody
    verified, so matching a Google/GitHub/Facebook e-mail against it would hand the account to
    whoever owns that address. Such accounts are only entered through NetID.
    """
    from social_core.pipeline.social_auth import associate_by_email as social_associate_by_email
    result = social_associate_by_email(backend, details, user, *args, **kwargs)
    found = (result or {}).get('user')
    if found is not None and not found.has_usable_password() and \
            not found.social_auth.exists():
        return None
    return result


def verify_email(backend, details, *args, **kwargs):
    if not details['email']:
        raise InvalidEmail(backend)


class UsernameForm(forms.Form):
    username = forms.RegexField(regex=r'^\w+$', max_length=30, label='Username',
                                error_messages={'invalid': 'A username must contain letters, numbers, or underscores.'})

    def clean_username(self):
        username = self.cleaned_data['username']
        # CA-7: same rule as local registration. The suggestion for ab123@gmail.com is ab123.
        if is_sso_reserved_username(username):
            raise forms.ValidationError(SSO_RESERVED_USERNAME_MESSAGE)
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError('Sorry, the username is taken.')
        return username


@partial
def choose_username(backend, user, username=None, *args, **kwargs):
    if not user:
        request = backend.strategy.request
        if request.POST:
            form = UsernameForm(request.POST)
            if form.is_valid():
                return {'username': form.cleaned_data['username']}
        else:
            form = UsernameForm(initial={'username': username})
        return render(request, 'registration/username_select.html', {
            'title': 'Choose a username', 'form': form,
        })


@partial
def make_profile(backend, user, response, is_new=False, *args, **kwargs):
    if is_new:
        if not hasattr(user, 'profile'):
            profile = Profile(user=user)
            profile.language = Language.get_default_language()
            logger.info('Info from %s: %s', backend.name, response)
            profile.save()
            form = ProfileForm(instance=profile, user=user)
        else:
            data = backend.strategy.request_data()
            logger.info(data)
            form = ProfileForm(data, instance=user.profile, user=user)
            if form.is_valid():
                with revisions.create_revision(atomic=True):
                    form.save()
                    revisions.set_user(user)
                    revisions.set_comment('Updated on registration')
                    return
        return render(backend.strategy.request, 'registration/profile_creation.html', {
            'title': 'Create your profile', 'form': form,
        })


class SocialAuthExceptionMiddleware(OldSocialAuthExceptionMiddleware):
    def process_exception(self, request, exception):
        if isinstance(exception, SocialAuthBaseException):
            return HttpResponseRedirect('%s?message=%s' % (reverse('social_auth_error'),
                                                           quote(self.get_message(request, exception))))
