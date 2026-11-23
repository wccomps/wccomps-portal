from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("scoring", "0052_signed_service_corrections")]

    operations = [
        migrations.AddField(
            model_name="servicecorrectionbatch",
            name="full_void",
            field=models.BooleanField(default=False, db_default=False),
        ),
    ]
