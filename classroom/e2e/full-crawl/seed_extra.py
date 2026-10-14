# run: manage.py shell < seed_extra.py   (after seed_dev_data) -- extra objects so every detail route has data
from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from judge.models import (BlogPost, Comment, Contest, ContestParticipation, ContestProblem, ContestSubmission,
                          ContestTag, Language, Organization, OrganizationRequest, Problem, Profile, Submission,
                          SubmissionSource, Ticket, TicketMessage)

now = timezone.now()
P = {u: Profile.objects.get(user__username=u) for u in ('admin', 'prof_li', 'ta_wang', 'ab101', 'ab102', 'ab105')}
post = BlogPost.objects.create(title='Welcome to CS101', slug='welcome-cs101', visible=True,
                               publish_on=now - timedelta(days=1), content='Welcome! See **Homework 1**.',
                               summary='Course start.')
post.authors.add(P['admin'])
cs_sum = Problem.objects.get(code='cs_sum')
Comment.objects.create(author=P['ab101'], page='p:cs_sum', body='Is input on one line?')
Comment.objects.create(author=P['ab102'], page='b:%d' % post.id, body='Thanks!')
t = Ticket.objects.create(title='Sample input unclear', user=P['ab101'],
                          content_type=ContentType.objects.get_for_model(Problem), object_id=cs_sum.id)
TicketMessage.objects.create(ticket=t, user=P['ab101'], body='Is the input on one line?')
math = Organization.objects.get(slug='math201')
OrganizationRequest.objects.create(user=P['ab105'], organization=math, reason='I am taking this course.')
ContestTag.objects.create(name='midterm', color='#ff0000', description='Midterm exams')
Contest.objects.get(key='cs101_exam1').tags.add(ContestTag.objects.get(name='midterm'))
# Code answers: one practice submission by ab101, one homework submission by ab102.
py = Language.objects.get(key='PY3')
s = Submission.objects.create(user=P['ab101'], problem=cs_sum, language=py, status='D', result='AC', points=10,
                              case_points=10, case_total=10, time=0.01, memory=1024)
SubmissionSource.objects.create(submission=s, source='a, b = map(int, input().split())\nprint(a + b)\n')
hw = Contest.objects.get(key='cs101_hw1')
part = ContestParticipation.objects.create(contest=hw, user=P['ab102'], real_start=now - timedelta(hours=2))
s2 = Submission.objects.create(user=P['ab102'], problem=cs_sum, language=py, status='D', result='WA', points=0,
                               case_points=0, case_total=10, time=0.01, memory=1024, contest_object=hw)
SubmissionSource.objects.create(submission=s2, source='print(0)\n')
ContestSubmission.objects.create(submission=s2, problem=ContestProblem.objects.get(contest=hw, problem=cs_sum),
                                 participation=part, points=0)
part.recompute_results()
print('extra seed ok')
