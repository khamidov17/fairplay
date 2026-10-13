# Generated manually for course join-request revoke semantics

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0178_merge_short_answer_and_problem_import'),
    ]

    operations = [
        migrations.AddField(
            model_name='organizationrequest',
            name='grants_organization',
            field=models.BooleanField(
                default=True,
                help_text='If false, this request only adds class membership for an existing course member.',
                verbose_name='grants course membership',
            ),
        ),
    ]
