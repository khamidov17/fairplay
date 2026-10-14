# run: manage.py shell < gen_urls.py > urls.json
# Every route of dmoj/urls.py, with its arguments filled from the seeded objects.
import json
import re

from django.contrib import admin
from django.urls import NoReverseMatch, URLPattern, URLResolver, get_resolver, reverse
from django.utils import timezone

from judge.models import (BlogPost, Class, ClassMaterial, Comment, Contest, ContestParticipation, ContestProblem,
                          ContestSubmission, License, Organization, OrganizationRequest, Problem, Submission,
                          Ticket)
from judge.models import ContestTag


def walk(resolver, prefix=''):
    for p in resolver.url_patterns:
        if isinstance(p, URLResolver):
            yield from walk(p, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), p.name


SKIP = re.compile(r'^(admin/|media|\^media|i18n|impersonate/|login/|complete/|disconnect/|mailgun|500/|'
                  r'accounts/(logout|2fa/(disable|webauthn|scratchcode)|api/token|activate/|email/change/activate|'
                  r'\^password/reset/confirm))')
users = ['ab101', 'ab102', 'prof_li', 'ta_wang', 'admin']
now = timezone.now()
orgs = [(o.id, o.slug) for o in Organization.objects.all()]
classes = [(c.organization_id, c.organization.slug, c.id, c.slug) for c in Class.objects.all()]
contests = list(Contest.objects.values_list('key', flat=True))
problems = list(Problem.objects.values_list('code', flat=True))
subs = list(Submission.objects.values_list('id', 'problem__code', 'user__user__username'))
cps = list(ContestProblem.objects.values_list('contest__key', 'problem__code'))
parts = list(ContestParticipation.objects.values_list('contest__key', 'user__user__username'))
csubs = list(ContestSubmission.objects.values_list('participation__contest__key',
                                                   'participation__user__user__username',
                                                   'problem__problem__code'))


def combos(route, params):
    ps = set(params)
    if {'cpk', 'cslug', 'mid'} <= ps:
        return [dict(pk=c[0], slug=c[1], cpk=c[2], cslug=c[3], mid=m.id)
                for c in classes for m in ClassMaterial.objects.filter(class_obj_id=c[2])]
    if {'cpk', 'cslug'} <= ps:
        return [dict(pk=c[0], slug=c[1], cpk=c[2], cslug=c[3]) for c in classes]
    if route.startswith('organization/'):
        if 'rpk' in ps:
            return [dict(pk=r.organization_id, slug=r.organization.slug, rpk=r.id)
                    for r in OrganizationRequest.objects.all()]
        return [dict(pk=o[0], slug=o[1]) for o in orgs]
    if route.startswith('post/'):
        return [dict(id=b.id, slug=b.slug) for b in BlogPost.objects.all()]
    if route.startswith('comments/'):
        return [dict(id=c) for c in Comment.objects.values_list('id', flat=True)]
    if route.startswith('ticket/'):
        return [dict(pk=t) for t in Ticket.objects.values_list('id', flat=True)]
    if ps == {'year', 'month'}:
        return [dict(year=now.year, month=now.month)]
    if ps == {'name'}:
        return [dict(name=t) for t in ContestTag.objects.values_list('name', flat=True)]
    if ps == {'key'}:
        return [dict(key=k) for k in License.objects.values_list('key', flat=True)]
    if ps - {'page'} == {'contest', 'user', 'problem'}:
        base = [dict(contest=c, user=u, problem=p) for c, u, p in csubs]
    elif ps - {'page'} == {'contest', 'problem'}:
        base = [dict(contest=c, problem=p) for c, p in cps]
    elif ps - {'page'} == {'contest', 'user'}:
        base = [dict(contest=c, user=u) for c, u in parts]
    elif ps - {'page'} == {'problem', 'submission'}:
        base = [dict(problem=p, submission=s) for s, p, u in subs]
    elif ps - {'page'} == {'problem', 'user'}:
        base = [dict(problem=p, user=u) for s, p, u in subs]
    elif ps - {'page'} == {'contest'}:
        base = [dict(contest=c) for c in contests]
    elif ps - {'page'} == {'problem'}:
        base = [dict(problem=p) for p in problems]
    elif ps - {'page'} == {'user'}:
        base = [dict(user=u) for u in users]
    elif ps - {'page'} == {'submission'}:
        base = [dict(submission=s) for s, p, u in subs]
    elif ps == {'page'}:
        base = [{}]
    else:
        return None  # unknown parameter (token, draft, task id, file uuid ...): skipped
    if 'page' in ps:
        for b in base:
            b['page'] = 1
    # dedupe
    out, seen = [], set()
    for b in base:
        k = tuple(sorted(b.items()))
        if k not in seen:
            seen.add(k)
            out.append(b)
    return out


result, skipped = [], []
for route, name in walk(get_resolver()):
    if SKIP.match(route):
        continue
    if route.startswith('^contests/tag/'):
        route = route.lstrip('^').replace('(?P<name>[a-z-]+)', '<name>')
    if route.startswith('^') or '(?P' in route:
        skipped.append(route)
        continue
    params = re.findall(r'<(?:\w+:)?(\w+)>', route)
    if not params:
        result.append({'url': '/' + route, 'name': name})
        continue
    cs = combos(route, params)
    if not cs:
        skipped.append(route)
        continue
    for kw in cs:
        url = route
        for k, v in kw.items():
            url = re.sub(r'<(?:\w+:)?%s>' % k, str(v), url)
        result.append({'url': '/' + url, 'name': name})

# Admin: change list, add page and the first object's change page for every registered model.
for model, ma in admin.site._registry.items():
    meta = model._meta
    for view in ('changelist', 'add'):
        try:
            result.append({'url': reverse('admin:%s_%s_%s' % (meta.app_label, meta.model_name, view)),
                           'name': 'admin-' + view})
        except NoReverseMatch:
            pass
    obj = model._default_manager.order_by('pk').first()
    if obj is not None:
        result.append({'url': reverse('admin:%s_%s_change' % (meta.app_label, meta.model_name), args=(obj.pk,)),
                       'name': 'admin-change'})
result.append({'url': '/admin/', 'name': 'admin-index'})
seen = set()
final = [r for r in result if not (r['url'] in seen or seen.add(r['url']))]
print(json.dumps({'urls': final, 'skipped': skipped}))
