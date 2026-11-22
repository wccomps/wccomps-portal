from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("scoring", "0051_service_corrections")]

    operations = [
        migrations.AlterField(
            model_name="servicecorrection",
            name="service_credit",
            field=models.IntegerField(),
        ),
    ]
