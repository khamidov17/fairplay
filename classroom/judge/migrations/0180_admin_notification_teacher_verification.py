from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


SITE_ADMINISTRATORS_GROUP = 'Site Administrators'
TEACHERS_GROUP = 'Teachers'


def create_groups(apps, schema_editor):
    from django.db.models import Q

    Group = apps.get_model('auth', 'Group')
    Permission = apps.get_model('auth', 'Permission')
    User = apps.get_model(settings.AUTH_USER_MODEL)

    site_admins, _ = Group.objects.get_or_create(name=SITE_ADMINISTRATORS_GROUP)
    teachers, _ = Group.objects.get_or_create(name=TEACHERS_GROUP)

    # Prefer edit_all_organization; fall back to staff/superusers.
    perm = Permission.objects.filter(
        content_type__app_label='judge',
        codename='edit_all_organization',
    ).first()
    if perm:
        site_admins.permissions.add(perm)

    teacher_codenames = (
        'add_organization',
        'change_organization',
        'organization_admin',
        'add_class',
        'change_class',
        'delete_class',
        'create_private_problem',
        'edit_own_problem',
    )
    teacher_perms = Permission.objects.filter(
        content_type__app_label='judge',
        codename__in=teacher_codenames,
    )
    teachers.permissions.add(*teacher_perms)

    staff_ids = list(
        User.objects.filter(Q(is_superuser=True) | Q(is_staff=True))
        .values_list('id', flat=True)[:50],
    )
    if staff_ids:
        site_admins.user_set.add(*staff_ids)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('judge', '0179_organizationrequest_grants_organization'),
    ]

    operations = [
        migrations.CreateModel(
            name='TeacherVerificationRequest',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('reason', models.TextField(verbose_name='reason')),
                ('time', models.DateTimeField(auto_now_add=True, verbose_name='request time')),
                ('state', models.CharField(
                    choices=[('P', 'Pending'), ('A', 'Approved'), ('R', 'Rejected')],
                    default='P', max_length=1, verbose_name='state',
                )),
                ('reviewed_time', models.DateTimeField(blank=True, null=True, verbose_name='reviewed time')),
                ('reviewed_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='reviewed_teacher_verifications', to='judge.profile',
                    verbose_name='reviewed by',
                )),
                ('user', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='teacher_verification_requests', to='judge.profile',
                    verbose_name='user',
                )),
            ],
            options={
                'verbose_name': 'teacher verification request',
                'verbose_name_plural': 'teacher verification requests',
                'ordering': ('-time',),
            },
        ),
        migrations.CreateModel(
            name='AdminNotification',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(
                    choices=[
                        ('teacher_verify', 'Teacher verification'),
                        ('course_join', 'Course join request'),
                        ('class_join', 'Class join request'),
                    ],
                    db_index=True, max_length=32, verbose_name='kind',
                )),
                ('title', models.CharField(max_length=200, verbose_name='title')),
                ('body', models.TextField(blank=True, verbose_name='body')),
                ('link', models.CharField(blank=True, max_length=300, verbose_name='link')),
                ('created', models.DateTimeField(auto_now_add=True, db_index=True, verbose_name='created')),
                ('read_at', models.DateTimeField(blank=True, null=True, verbose_name='read at')),
                ('actor', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='sent_admin_notifications', to='judge.profile',
                    verbose_name='actor',
                )),
                ('organization_request', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='notifications', to='judge.organizationrequest',
                )),
                ('recipient', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='admin_notifications', to='judge.profile',
                    verbose_name='recipient',
                )),
                ('teacher_verification', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.CASCADE,
                    related_name='notifications', to='judge.teacherverificationrequest',
                )),
            ],
            options={
                'verbose_name': 'admin notification',
                'verbose_name_plural': 'admin notifications',
                'ordering': ('-created',),
            },
        ),
        migrations.AddIndex(
            model_name='adminnotification',
            index=models.Index(fields=['recipient', 'read_at', '-created'], name='judge_admin_recipi_7f2c1a_idx'),
        ),
        migrations.RunPython(create_groups, noop_reverse),
    ]
