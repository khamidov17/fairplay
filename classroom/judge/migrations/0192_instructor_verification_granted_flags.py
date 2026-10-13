# Track which privileges an instructor approval actually granted (safe revoke).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('judge', '0191_rename_teacher_to_instructor'),
    ]

    operations = [
        migrations.AddField(
            model_name='instructorverificationrequest',
            name='granted_class_admin',
            field=models.BooleanField(
                default=False,
                help_text='True if approval added the user to class.admins.',
                verbose_name='granted class admin',
            ),
        ),
        migrations.AddField(
            model_name='instructorverificationrequest',
            name='granted_staff',
            field=models.BooleanField(
                default=False,
                help_text='True if approval flipped user.is_staff from False to True.',
                verbose_name='granted staff',
            ),
        ),
    ]
